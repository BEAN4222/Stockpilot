"""
The tools the AI can call, as OpenAI function-calling schemas, and the
dispatcher that runs them.

This list is the AI's whole reach: it can read the rebalance plan, the
account and market data, buy, sell (only what it holds, see trader/guard.py),
cancel its orders, and finish the cycle. Nothing else.
"""
from __future__ import annotations

import json
from typing import Callable, Dict, List, Optional

import requests

from trader.broker import AlpacaBroker, BrokerError, OrderRejected
from trader.guard import OrderGuard
from trader.portfolio import Planner


def _fn(name: str, description: str, properties: Optional[dict] = None,
        required: Optional[List[str]] = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties or {},
                "required": required or [],
            },
        },
    }


_COMMON_ORDER_PROPS = {
    "symbol": {"type": "string", "description": "Ticker, e.g. AAPL"},
    "order_type": {"type": "string", "enum": ["market", "limit"]},
    "limit_price": {"type": "number", "description": "Required for limit orders"},
    "reason": {"type": "string", "description": "Short reason for this trade, for the log"},
}
_BUY_PROPS = {
    **_COMMON_ORDER_PROPS,
    "notional_usd": {"type": "number",
                     "description": "Dollar amount to buy (market orders only, min $1). "
                                    "Use this OR qty."},
    "qty": {"type": "number", "description": "Number of shares, fractions allowed. Use this OR notional_usd."},
}
_SELL_PROPS = {
    **_COMMON_ORDER_PROPS,
    "qty": {"type": "number", "description": "Number of shares to sell, fractions allowed"},
    "sell_all": {"type": "boolean", "description": "true to sell the whole position instead of qty"},
}

TOOL_SCHEMAS = [
    _fn("get_rebalance_plan",
        "The followed manager's latest 13F portfolio, our target weights, current holdings, "
        "and the trades that would bring the account back to target. Recomputed on every call."),
    _fn("get_account", "Cash, equity and today's P/L of the account."),
    _fn("get_positions", "All positions currently held, with cost basis and unrealized P/L."),
    _fn("get_open_orders", "Orders that are submitted but not yet filled or cancelled."),
    _fn(
        "get_quotes",
        "Latest price, bid/ask, today's bar and previous close for one or more symbols.",
        {"symbols": {"type": "array", "items": {"type": "string"}}},
        ["symbols"],
    ),
    _fn(
        "get_bars",
        "Historical OHLCV bars, oldest first.",
        {
            "symbol": {"type": "string"},
            "timeframe": {"type": "string", "enum": ["5Min", "15Min", "1Hour", "1Day", "1Week"]},
            "limit": {"type": "integer", "description": "Number of bars, max 200"},
        },
        ["symbol"],
    ),
    _fn(
        "get_indicators",
        "Daily indicators computed from the last 200 daily bars: SMA 20/50/200, RSI 14, "
        "20-day high/low, 5/20-day returns, average volume.",
        {"symbol": {"type": "string"}},
        ["symbol"],
    ),
    _fn("buy", "Buy (long) by dollar amount or share quantity. Paid from cash only. "
        "Checked against the risk limits.", _BUY_PROPS, ["symbol", "order_type", "reason"]),
    _fn("sell", "Sell shares you currently hold, by quantity or sell_all. Can never sell more "
        "than you hold (no shorting).", _SELL_PROPS, ["symbol", "order_type", "reason"]),
    _fn("cancel_order", "Cancel an open order.",
        {"order_id": {"type": "string"}}, ["order_id"]),
    _fn(
        "finish",
        "End this cycle. Call it when you are done, including when you decide not to trade.",
        {"summary": {"type": "string", "description": "What you did and why"}},
        ["summary"],
    ),
]


# ------------------------------------------------------------- indicators
def _sma(values: List[float], n: int):
    return round(sum(values[-n:]) / n, 2) if len(values) >= n else None


def _rsi(closes: List[float], n: int = 14):
    if len(closes) <= n:
        return None
    gains, losses = [], []
    for prev, cur in zip(closes[:-1], closes[1:]):
        change = cur - prev
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    # Wilder's smoothing.
    avg_gain = sum(gains[:n]) / n
    avg_loss = sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):
        avg_gain = (avg_gain * (n - 1) + g) / n
        avg_loss = (avg_loss * (n - 1) + l) / n
    if avg_loss == 0:
        return 100.0
    return round(100 - 100 / (1 + avg_gain / avg_loss), 2)


def compute_indicators(bars: List[dict]) -> dict:
    closes = [float(b["c"]) for b in bars]
    if not closes:
        return {"error": "no bars"}
    last = closes[-1]

    def ret(n):
        return round((last / closes[-1 - n] - 1) * 100, 2) if len(closes) > n else None

    return {
        "last_close": last,
        "sma20": _sma(closes, 20),
        "sma50": _sma(closes, 50),
        "sma200": _sma(closes, 200),
        "rsi14": _rsi(closes),
        "high20": max(float(b["h"]) for b in bars[-20:]),
        "low20": min(float(b["l"]) for b in bars[-20:]),
        "return_5d_pct": ret(5),
        "return_20d_pct": ret(20),
        "avg_volume20": round(sum(float(b["v"]) for b in bars[-20:]) / len(bars[-20:])),
        "bars_used": len(bars),
    }


# ------------------------------------------------------------- dispatcher
class ToolRunner:
    def __init__(self, broker: AlpacaBroker, guard: OrderGuard, planner: Planner):
        self.broker = broker
        self.guard = guard
        self.planner = planner
        self.finished = False
        self.summary = ""
        # Every order attempt this cycle, accepted or not, for the log.
        self.orders: List[dict] = []
        self._handlers: Dict[str, Callable[..., object]] = {
            "get_rebalance_plan": lambda: self.planner.plan(),
            "get_account": self.get_account,
            "get_positions": self.get_positions,
            "get_open_orders": self.get_open_orders,
            "get_quotes": self.get_quotes,
            "get_bars": self.get_bars,
            "get_indicators": self.get_indicators,
            "buy": self.buy,
            "sell": self.sell,
            "cancel_order": self.cancel_order,
            "finish": self.finish,
        }

    def run(self, name: str, arguments: str) -> str:
        """Run one tool call and return its result as a JSON string.
        Never raises: errors go back to the AI as {"error": ...}."""
        handler = self._handlers.get(name)
        if handler is None:
            return json.dumps({"error": f"Unknown tool {name!r}"})
        try:
            kwargs = json.loads(arguments) if arguments else {}
            if not isinstance(kwargs, dict):
                raise ValueError("arguments must be a JSON object")
            result = handler(**kwargs)
        except OrderRejected as e:
            result = {"rejected": str(e)}
        except (BrokerError, requests.RequestException, TypeError, ValueError, KeyError) as e:
            result = {"error": f"{type(e).__name__}: {e}"}
        return json.dumps(result, default=str)

    def get_account(self) -> dict:
        a = self.broker.get_account()
        equity, last_equity = float(a["equity"]), float(a["last_equity"])
        return {
            "cash": float(a["cash"]),
            "equity": equity,
            "today_pl": round(equity - last_equity, 2),
            "today_pl_pct": round((equity / last_equity - 1) * 100, 2) if last_equity else None,
        }

    def get_positions(self) -> List[dict]:
        return [
            {
                "symbol": p["symbol"],
                "qty": float(p["qty"]),
                "avg_entry_price": float(p["avg_entry_price"]),
                "current_price": float(p["current_price"]),
                "market_value": float(p["market_value"]),
                "unrealized_pl": float(p["unrealized_pl"]),
                "unrealized_pl_pct": round(float(p["unrealized_plpc"]) * 100, 2),
            }
            for p in self.broker.get_positions()
        ]

    def get_open_orders(self) -> List[dict]:
        return [
            {k: o.get(k) for k in ("id", "symbol", "side", "type", "qty", "filled_qty",
                                   "limit_price", "status", "submitted_at")}
            for o in self.broker.get_open_orders()
        ]

    def get_quotes(self, symbols: List[str]) -> dict:
        out = {}
        for symbol, s in self.broker.get_snapshots(list(symbols)[:20]).items():
            trade, quote = s.get("latestTrade") or {}, s.get("latestQuote") or {}
            day, prev = s.get("dailyBar") or {}, s.get("prevDailyBar") or {}
            price, prev_close = trade.get("p"), prev.get("c")
            out[symbol] = {
                "price": price,
                "bid": quote.get("bp"),
                "ask": quote.get("ap"),
                "day_open": day.get("o"),
                "day_high": day.get("h"),
                "day_low": day.get("l"),
                "day_volume": day.get("v"),
                "prev_close": prev_close,
                "change_pct": round((price / prev_close - 1) * 100, 2) if price and prev_close else None,
            }
        return out

    def get_bars(self, symbol: str, timeframe: str = "1Day", limit: int = 60) -> List[dict]:
        bars = self.broker.get_bars(symbol, timeframe, min(int(limit), 200))
        return [{k: b[k] for k in ("t", "o", "h", "l", "c", "v")} for b in bars]

    def get_indicators(self, symbol: str) -> dict:
        return compute_indicators(self.broker.get_bars(symbol, "1Day", 200))

    def _record(self, record: dict, place: Callable[[], dict]) -> dict:
        try:
            result = place()
        except OrderRejected as e:
            self.orders.append({**record, "rejected": str(e)})
            raise
        self.orders.append({**record, "result": result})
        return result

    def buy(self, symbol: str, order_type: str = "market", notional_usd=None, qty=None,
            limit_price: Optional[float] = None, reason: str = "") -> dict:
        record = {"side": "buy", "symbol": symbol, "notional_usd": notional_usd, "qty": qty,
                  "order_type": order_type, "limit_price": limit_price, "reason": reason}
        return self._record(record, lambda: self.guard.buy(
            symbol, qty=qty, notional=notional_usd, order_type=order_type, limit_price=limit_price))

    def sell(self, symbol: str, order_type: str = "market", qty=None, sell_all: bool = False,
             limit_price: Optional[float] = None, reason: str = "") -> dict:
        record = {"side": "sell", "symbol": symbol, "qty": "all" if sell_all else qty,
                  "order_type": order_type, "limit_price": limit_price, "reason": reason}
        return self._record(record, lambda: self.guard.sell(
            symbol, qty=qty, sell_all=bool(sell_all), order_type=order_type, limit_price=limit_price))

    def cancel_order(self, order_id: str) -> dict:
        if self.guard.dry_run:
            return {"dry_run": True, "cancelled": order_id}
        self.broker.cancel_order(order_id)
        return {"cancel_requested": order_id}

    def finish(self, summary: str = "") -> dict:
        self.finished = True
        self.summary = summary
        return {"ok": True}
