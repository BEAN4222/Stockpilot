"""
Thin Alpaca REST client (trading + market data).

`submit_order` is the only function in the program that sends an order, and
it refuses any sell larger than the shares we actually hold minus shares
already waiting in open sell orders. Selling without a position is how a
short is opened at Alpaca, so this makes shorting impossible from our side.
Sells always go by share quantity (never a dollar amount) so this check is
exact, fractions included. `ensure_long_only` additionally turns on
Alpaca's own long-only mode (`no_shorting`) for the account, so the broker
refuses shorts too (and Alpaca never shorts fractional orders anyway).
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Dict, List, Optional

import requests

PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"

SYMBOL_RE = re.compile(r"^[A-Z][A-Z.]{0,9}$")
TIMEFRAMES = {"1Min", "5Min", "15Min", "1Hour", "1Day", "1Week"}


class BrokerError(Exception):
    """The Alpaca API returned an error."""


class OrderRejected(Exception):
    """An order was refused by our own checks before reaching the broker."""


def normalize_symbol(symbol: str) -> str:
    s = str(symbol).strip().upper()
    if not SYMBOL_RE.match(s):
        raise OrderRejected(f"Invalid symbol: {symbol!r}")
    return s


def to_qty(qty) -> Decimal:
    """A positive share count, fractions allowed, cut to Alpaca's 9 decimals."""
    if isinstance(qty, bool) or not isinstance(qty, (int, float, str, Decimal)):
        raise OrderRejected("qty must be a positive number of shares")
    try:
        q = Decimal(str(qty)).quantize(Decimal("1e-9"), rounding=ROUND_DOWN)
    except InvalidOperation:
        raise OrderRejected(f"Invalid qty {qty!r}") from None
    if not q.is_finite() or q <= 0:
        raise OrderRejected("qty must be a positive number of shares")
    # Drop trailing zeros, without the exponent normalize() gives integers ("1E+1").
    q = q.normalize()
    return q.quantize(Decimal(1)) if q == q.to_integral_value() else q


def to_notional(amount) -> Decimal:
    """A dollar amount of at least $1 (Alpaca's minimum), cut to cents."""
    if isinstance(amount, bool) or not isinstance(amount, (int, float, str, Decimal)):
        raise OrderRejected("notional must be a dollar amount")
    try:
        n = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    except InvalidOperation:
        raise OrderRejected(f"Invalid notional {amount!r}") from None
    if not n.is_finite() or n < 1:
        raise OrderRejected("notional must be at least $1")
    return n


class AlpacaBroker:
    def __init__(self, live: bool = False, data_feed: str = "iex"):
        key_id = os.environ.get("ALPACA_API_KEY_ID", "").strip()
        secret = os.environ.get("ALPACA_API_SECRET_KEY", "").strip()
        if not key_id or not secret:
            raise ValueError(
                "ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY are not set. Check your .env file."
            )
        self.live = live
        self.base_url = LIVE_URL if live else PAPER_URL
        self.data_feed = data_feed
        self._session = requests.Session()
        self._session.headers.update(
            {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret}
        )

    # ------------------------------------------------------------------ HTTP
    def _request(self, method: str, url: str, **kwargs):
        response = self._session.request(method, url, timeout=30, **kwargs)
        if response.status_code == 404:
            return None
        if not response.ok:
            raise BrokerError(f"HTTP {response.status_code} {method} {url}: {response.text[:300]}")
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()

    def _trading(self, method: str, path: str, **kwargs):
        return self._request(method, f"{self.base_url}{path}", **kwargs)

    def _data(self, path: str, params: dict):
        return self._request("GET", f"{DATA_URL}{path}", params={**params, "feed": self.data_feed})

    # --------------------------------------------------------------- account
    def get_account(self) -> dict:
        return self._trading("GET", "/v2/account")

    def get_clock(self) -> dict:
        return self._trading("GET", "/v2/clock")

    def get_positions(self) -> List[dict]:
        return self._trading("GET", "/v2/positions") or []

    def get_position(self, symbol: str) -> Optional[dict]:
        """None when there is no position in that symbol."""
        return self._trading("GET", f"/v2/positions/{normalize_symbol(symbol)}")

    def get_asset(self, symbol: str) -> Optional[dict]:
        return self._trading("GET", f"/v2/assets/{normalize_symbol(symbol)}")

    def get_open_orders(self, symbol: Optional[str] = None) -> List[dict]:
        params = {"status": "open", "limit": 500}
        if symbol:
            params["symbols"] = normalize_symbol(symbol)
        return self._trading("GET", "/v2/orders", params=params) or []

    def get_orders_since(self, after: datetime) -> List[dict]:
        params = {"status": "all", "after": after.isoformat(), "limit": 500}
        return self._trading("GET", "/v2/orders", params=params) or []

    def ensure_long_only(self) -> None:
        """Turn on Alpaca's long-only mode and refuse to run if it cannot be
        confirmed, or if the account already holds a short position."""
        config = self._trading("GET", "/v2/account/configurations") or {}
        if config.get("no_shorting") is not True:
            self._trading("PATCH", "/v2/account/configurations", json={"no_shorting": True})
            config = self._trading("GET", "/v2/account/configurations") or {}
        if config.get("no_shorting") is not True:
            raise RuntimeError("Could not enable no_shorting on the Alpaca account; refusing to trade.")
        shorts = [p["symbol"] for p in self.get_positions() if float(p.get("qty", 0)) < 0]
        if shorts:
            raise RuntimeError(f"Account holds short positions {shorts}; refusing to trade.")

    # ---------------------------------------------------------- market data
    def get_snapshots(self, symbols: List[str]) -> Dict[str, dict]:
        symbols = [normalize_symbol(s) for s in symbols]
        return self._data("/v2/stocks/snapshots", {"symbols": ",".join(symbols)}) or {}

    def get_bars(self, symbol: str, timeframe: str = "1Day", limit: int = 100) -> List[dict]:
        """The last `limit` bars, oldest first."""
        if timeframe not in TIMEFRAMES:
            raise ValueError(f"timeframe must be one of {sorted(TIMEFRAMES)}")
        limit = max(1, min(int(limit), 1000))
        # Look back far enough to cover weekends/holidays, then take the
        # newest `limit` bars.
        days = {"1Week": limit * 8, "1Day": limit * 2}.get(timeframe, 30)
        start = datetime.now(timezone.utc) - timedelta(days=days + 10)
        data = self._data(
            f"/v2/stocks/{normalize_symbol(symbol)}/bars",
            {
                "timeframe": timeframe,
                "start": start.isoformat(),
                "limit": limit,
                "sort": "desc",
                "adjustment": "split",
            },
        ) or {}
        return list(reversed(data.get("bars") or []))

    # --------------------------------------------------------------- orders
    def sellable_qty(self, symbol: str) -> Decimal:
        """Shares (fractional included) we hold that are not already reserved
        by open sell orders. 0 when there is no (long) position."""
        symbol = normalize_symbol(symbol)
        position = self.get_position(symbol)
        held = Decimal(str(position["qty"])) if position else Decimal(0)
        if held <= 0:
            return Decimal(0)
        reserved = Decimal(0)
        for o in self.get_open_orders(symbol):
            if o.get("side") != "sell":
                continue
            if o.get("qty") is None:
                # A dollar-amount sell placed by hand: its share count is not
                # known, so treat the whole position as reserved.
                return Decimal(0)
            reserved += Decimal(str(o["qty"])) - Decimal(str(o.get("filled_qty") or 0))
        return max(Decimal(0), held - reserved)

    def submit_order(
        self,
        symbol: str,
        side: str,
        qty=None,
        notional: Optional[float] = None,
        order_type: str = "market",
        limit_price: Optional[float] = None,
    ) -> dict:
        """Send one order: `qty` shares (fractions allowed) or, for market
        buys only, a dollar amount `notional`."""
        symbol = normalize_symbol(symbol)
        if side not in ("buy", "sell"):
            raise OrderRejected(f"Invalid side {side!r}")
        if order_type not in ("market", "limit"):
            raise OrderRejected("order_type must be 'market' or 'limit'")
        if order_type == "limit" and (limit_price is None or limit_price <= 0):
            raise OrderRejected("limit orders need a positive limit_price")
        if (qty is None) == (notional is None):
            raise OrderRejected("give exactly one of qty or notional")

        body = {"symbol": symbol, "side": side, "type": order_type, "time_in_force": "day"}
        if notional is not None:
            # A dollar amount cannot be checked against shares held, so sells
            # always go by quantity.
            if side != "buy" or order_type != "market":
                raise OrderRejected("notional (dollar) orders are only allowed for market buys")
            notional = to_notional(notional)
            body["notional"] = str(notional)
        else:
            qty = to_qty(qty)
            body["qty"] = str(qty)

        # LONG ONLY: never sell more than we hold. This check sits right before
        # the request so no code path can skip it.
        if side == "sell":
            sellable = self.sellable_qty(symbol)
            if qty > sellable:
                raise OrderRejected(
                    f"Long-only: cannot sell {qty} {symbol}, only {sellable} sellable shares held. "
                    "Selling more than you hold would open a short position."
                )

        if order_type == "limit":
            body["limit_price"] = str(round(float(limit_price), 2))
        return self._trading("POST", "/v2/orders", json=body)

    def cancel_order(self, order_id: str) -> None:
        if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", str(order_id)):
            raise OrderRejected(f"Invalid order id {order_id!r}")
        self._trading("DELETE", f"/v2/orders/{order_id}")
