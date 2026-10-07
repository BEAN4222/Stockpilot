"""
Target portfolio and rebalance plan.

Targets: the followed manager's latest 13F (trader/sec13f.py), turned into
tickers, limited to stocks Alpaca can trade in fractions, renormalized to
100%, each capped at max_position_pct of equity (the excess stays in cash).

Plan: for every target, target $ = weight x investable equity (equity minus
the cash reserve), compared with what we hold:
  - held but no longer in the 13F          -> sell all
  - below target by more than the drift band -> buy the difference (in $)
  - above target by more than the drift band -> sell the difference (in shares)
Symbols with an open order are left alone until it fills or is cancelled.
Sells come first so their cash can pay for the buys.

The plan is a proposal: the AI reviews it and places the orders, within the
limits of trader/guard.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Dict, List, Optional

from trader.broker import AlpacaBroker, to_qty
from trader.config import TradingConfig
from trader.sec13f import Filing13F, SEC13FClient, map_cusips


@dataclass
class Target:
    symbol: str
    issuer: str
    weight_pct: float     # our target, after filtering and renormalizing
    filing_pct: float     # its weight in the 13F itself


def build_targets(filing: Filing13F, tickers: Dict[str, Optional[str]], assets: Dict[str, Optional[dict]],
                  config: TradingConfig) -> tuple:
    """(targets, skipped): skipped lists holdings left out, with the reason."""
    total = sum(h.value for h in filing.holdings) or 1.0
    values: Dict[str, float] = {}
    issuers: Dict[str, str] = {}
    skipped = []
    for h in filing.holdings:
        pct = h.value / total * 100
        symbol = tickers.get(h.cusip)
        asset = assets.get(symbol) if symbol else None
        if not symbol:
            reason = "no ticker found for CUSIP"
        elif not asset or not asset.get("tradable") or asset.get("status") != "active":
            reason = "not tradable at Alpaca"
        elif not asset.get("fractionable"):
            reason = "Alpaca does not allow fractional shares of it"
        elif pct < config.min_weight_pct:
            reason = f"weight below min_weight_pct ({config.min_weight_pct}%)"
        else:
            values[symbol] = values.get(symbol, 0.0) + h.value
            issuers[symbol] = h.issuer
            continue
        skipped.append({"cusip": h.cusip, "issuer": h.issuer, "symbol": symbol,
                        "filing_pct": round(pct, 2), "reason": reason})

    kept = sum(values.values()) or 1.0
    targets = [
        Target(symbol=s, issuer=issuers[s], weight_pct=v / kept * 100, filing_pct=v / total * 100)
        for s, v in sorted(values.items(), key=lambda kv: -kv[1])
    ]
    return targets, skipped


def _round_qty(qty: float) -> Decimal:
    """Rounded down to 6 decimals (never sell a hair more than intended)."""
    q = Decimal(str(qty)).quantize(Decimal("1e-6"), rounding=ROUND_DOWN)
    return to_qty(q) if q > 0 else Decimal(0)


def make_plan(targets: List[Target], positions: List[dict], open_orders: List[dict],
              equity: float, config: TradingConfig) -> dict:
    investable = equity * (1 - config.min_cash_reserve_pct / 100)
    cap = equity * config.max_position_pct / 100
    held = {p["symbol"]: p for p in positions if float(p["qty"]) > 0}
    busy = {o["symbol"] for o in open_orders}
    target_by_symbol = {t.symbol: t for t in targets}

    rows, sells, buys, waiting = [], [], [], []
    for symbol in sorted(set(target_by_symbol) | set(held)):
        t = target_by_symbol.get(symbol)
        p = held.get(symbol)
        current = float(p["market_value"]) if p else 0.0
        target_usd = min(t.weight_pct / 100 * investable, cap) if t else 0.0
        diff = target_usd - current
        rows.append({
            "symbol": symbol,
            "filing_pct": round(t.filing_pct, 2) if t else 0.0,
            "target_pct": round(t.weight_pct, 2) if t else 0.0,
            "target_usd": round(target_usd, 2),
            "current_usd": round(current, 2),
            "diff_usd": round(diff, 2),
        })
        if symbol in busy:
            waiting.append(symbol)
            continue
        band = max(config.min_trade_usd, target_usd * config.rebalance_drift_pct / 100)
        if t is None:
            sells.append({"action": "sell", "symbol": symbol, "sell_all": True,
                          "why": "no longer in the 13F"})
        elif diff <= -band and p:
            qty = _round_qty(-diff / float(p["current_price"]))
            if qty > 0:
                sells.append({"action": "sell", "symbol": symbol, "qty": str(qty),
                              "approx_usd": round(-diff, 2), "why": "above target"})
        elif diff >= band:
            # One entry per order, each within max_order_usd.
            left = round(diff, 2)
            while left >= config.min_trade_usd:
                amount = min(left, config.max_order_usd)
                buys.append({"action": "buy", "symbol": symbol, "notional_usd": amount,
                             "why": "below target" if p else "new position"})
                left = round(left - amount, 2)

    return {
        "equity": round(equity, 2),
        "investable_usd": round(investable, 2),
        "positions": sorted(rows, key=lambda r: -r["target_usd"]),
        "trades": sells + buys,
        "waiting_on_open_orders": waiting,
    }


class Planner:
    """Keeps the latest filing and targets, and makes plans from live account data."""

    def __init__(self, broker: AlpacaBroker, config: TradingConfig, cache_dir: str = "cache"):
        self.broker = broker
        self.config = config
        self.cache_dir = cache_dir
        self.sec = SEC13FClient(cache_dir)
        self.filing: Optional[Filing13F] = None
        self.targets: List[Target] = []
        self.skipped: List[dict] = []

    def refresh(self) -> None:
        """Re-read the latest 13F (cheap when unchanged: filings are cached)."""
        filing = self.sec.latest(self.config.follow_cik)
        cusips = [h.cusip for h in filing.holdings]
        tickers = map_cusips(cusips, self.cache_dir, self.config.ticker_overrides)
        assets = {s: self.broker.get_asset(s) for s in set(filter(None, tickers.values()))}
        self.filing = filing
        self.targets, self.skipped = build_targets(filing, tickers, assets, self.config)

    @property
    def target_symbols(self) -> set:
        return {t.symbol for t in self.targets}

    def plan(self) -> dict:
        if self.filing is None:
            self.refresh()
        account = self.broker.get_account()
        plan = make_plan(self.targets, self.broker.get_positions(), self.broker.get_open_orders(),
                         float(account["equity"]), self.config)
        f = self.filing
        return {
            "following": f"{self.config.follow_name} (CIK {f.cik})",
            "filing": {
                "report_period": f.report_date,
                "filed": f.filed_date,
                "accessions": f.accessions,
                "holdings_in_filing": len(f.holdings),
                "some_holdings_kept_confidential": f.confidential_omitted,
            },
            "skipped_holdings": self.skipped,
            **plan,
        }
