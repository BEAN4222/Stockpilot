"""
Order gate: the only way the AI can trade.

Every buy/sell the AI asks for goes through OrderGuard, which checks the hard
limits from trading_config.json before calling the broker. A refused order
raises OrderRejected with a message the AI can read and react to.

Long-only is enforced in three layers:
  1. The AI only has `buy` and `sell` tools; there is no short/margin tool.
  2. OrderGuard.sell and AlpacaBroker.submit_order both refuse to sell more
     shares than are held (minus shares already in open sell orders). Sells
     are always by share quantity, so the check is exact.
  3. AlpacaBroker.ensure_long_only turns on the account's `no_shorting` mode.
Buys are paid from cash only (never margin buying power).
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional, Set
from zoneinfo import ZoneInfo

from trader.broker import (
    AlpacaBroker,
    OrderRejected,
    normalize_symbol,
    to_notional,
    to_qty,
)
from trader.config import TradingConfig

NEW_YORK = ZoneInfo("America/New_York")


class OrderGuard:
    def __init__(self, broker: AlpacaBroker, config: TradingConfig, dry_run: bool = False):
        self.broker = broker
        self.config = config
        self.dry_run = dry_run
        # Symbols that may be bought (the current target portfolio); set by
        # the caller each cycle. None means no restriction.
        self.allowed_symbols: Optional[Set[str]] = None
        # Orders "placed" in dry-run mode, so the daily limit still applies.
        self._dry_run_orders = 0

    # ---------------------------------------------------------------- checks
    @staticmethod
    def _check_order_type(order_type: str, limit_price: Optional[float]) -> None:
        if order_type not in ("market", "limit"):
            raise OrderRejected("order_type must be 'market' or 'limit'")
        if order_type == "limit" and (limit_price is None or limit_price <= 0):
            raise OrderRejected("limit orders need a positive limit_price")

    def _check_daily_limit(self) -> None:
        today = datetime.now(NEW_YORK).replace(hour=0, minute=0, second=0, microsecond=0)
        placed = len(self.broker.get_orders_since(today)) + self._dry_run_orders
        if placed >= self.config.max_orders_per_day:
            raise OrderRejected(
                f"Daily order limit reached ({self.config.max_orders_per_day} orders today)."
            )

    def _last_price(self, symbol: str) -> float:
        snap = self.broker.get_snapshots([symbol]).get(symbol) or {}
        quote = snap.get("latestQuote") or {}
        trade = snap.get("latestTrade") or {}
        # For a buy, the ask is what we would pay; fall back to the last trade.
        price = float(quote.get("ap") or 0) or float(trade.get("p") or 0)
        if price <= 0:
            raise OrderRejected(f"No current price available for {symbol}.")
        return price

    def _open_buy_cost(self) -> float:
        """Cash already committed to open buy orders."""
        total = 0.0
        for o in self.broker.get_open_orders():
            if o.get("side") != "buy":
                continue
            if o.get("notional") is not None:
                total += float(o["notional"])
                continue
            remaining = float(o.get("qty") or 0) - float(o.get("filled_qty") or 0)
            price = o.get("limit_price")
            if price is None:
                price = self._last_price(o["symbol"]) * (1 + self.config.market_slippage_pct / 100)
            total += remaining * float(price)
        return total

    # ---------------------------------------------------------------- orders
    def buy(self, symbol: str, qty=None, notional=None, order_type: str = "market",
            limit_price: Optional[float] = None) -> dict:
        """Buy `qty` shares (fractions allowed) or `notional` dollars (market only)."""
        symbol = normalize_symbol(symbol)
        self._check_order_type(order_type, limit_price)
        if (qty is None) == (notional is None):
            raise OrderRejected("give exactly one of qty (shares) or notional_usd (dollars)")
        if notional is not None and order_type != "market":
            raise OrderRejected("a dollar amount (notional_usd) only works with market orders")
        cfg = self.config

        if cfg.only_target_symbols and self.allowed_symbols is not None \
                and symbol not in self.allowed_symbols:
            raise OrderRejected(f"{symbol} is not in the target portfolio; it may not be bought.")
        asset = self.broker.get_asset(symbol)
        if not asset or not asset.get("tradable") or asset.get("status") != "active":
            raise OrderRejected(f"{symbol} is not a tradable asset.")

        if notional is not None:
            notional = to_notional(notional)
            cost = float(notional)
            fractional = True
        else:
            qty = to_qty(qty)
            fractional = qty != qty.to_integral_value()
            price = float(limit_price) if order_type == "limit" else \
                self._last_price(symbol) * (1 + cfg.market_slippage_pct / 100)
            cost = float(qty) * price
        if fractional and not asset.get("fractionable"):
            raise OrderRejected(f"{symbol} cannot be bought in fractions; use a whole qty.")
        self._check_daily_limit()

        if cost > cfg.max_order_usd:
            raise OrderRejected(
                f"Order ~${cost:,.2f} exceeds max_order_usd ${cfg.max_order_usd:,.2f}; split it."
            )

        account = self.broker.get_account()
        equity = float(account["equity"])
        cash = float(account["cash"])
        reserve = equity * cfg.min_cash_reserve_pct / 100
        spendable = cash - self._open_buy_cost() - reserve
        if cost > spendable:
            raise OrderRejected(
                f"Not enough cash: order ~${cost:,.2f}, spendable ${max(spendable, 0):,.2f} "
                f"(cash ${cash:,.2f}, keeping {cfg.min_cash_reserve_pct}% of equity in reserve, "
                "no margin)."
            )

        position = self.broker.get_position(symbol)
        held_value = float(position["market_value"]) if position else 0.0
        max_value = equity * cfg.max_position_pct / 100
        if held_value + cost > max_value:
            raise OrderRejected(
                f"Position in {symbol} would be ~${held_value + cost:,.2f}, over the "
                f"{cfg.max_position_pct}% of equity limit (${max_value:,.2f})."
            )

        return self._send(symbol, "buy", qty, notional, order_type, limit_price, est_cost=cost)

    def sell(self, symbol: str, qty=None, sell_all: bool = False, order_type: str = "market",
             limit_price: Optional[float] = None) -> dict:
        """Sell `qty` shares, or every sellable share with sell_all=True."""
        symbol = normalize_symbol(symbol)
        self._check_order_type(order_type, limit_price)
        if sell_all == (qty is not None):
            raise OrderRejected("give either qty or sell_all=true")
        # Long-only. AlpacaBroker.submit_order checks this again right before sending.
        sellable = self.broker.sellable_qty(symbol)
        if sell_all:
            if sellable <= 0:
                raise OrderRejected(f"Long-only: no sellable {symbol} shares held.")
            qty = sellable
        qty = to_qty(qty)
        if qty > sellable:
            raise OrderRejected(
                f"Long-only: cannot sell {qty} {symbol}, only {sellable} sellable shares held. "
                "Short selling is not allowed."
            )
        self._check_daily_limit()
        return self._send(symbol, "sell", qty, None, order_type, limit_price)

    def _send(self, symbol, side, qty: Optional[Decimal], notional: Optional[Decimal],
              order_type, limit_price, est_cost=None) -> dict:
        if self.dry_run:
            self._dry_run_orders += 1
            result = {"dry_run": True, "symbol": symbol, "side": side, "qty": qty,
                      "notional": notional, "type": order_type, "limit_price": limit_price}
        else:
            order = self.broker.submit_order(symbol, side, qty=qty, notional=notional,
                                             order_type=order_type, limit_price=limit_price)
            result = {k: order.get(k) for k in
                      ("id", "symbol", "side", "qty", "notional", "type", "limit_price", "status")}
        if est_cost is not None:
            result["estimated_cost"] = round(est_cost, 2)
        return result
