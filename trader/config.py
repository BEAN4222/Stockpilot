"""
Trading configuration (trading_config.json).

These are hard limits: they are enforced in code by trader/guard.py, so the
AI cannot talk its way past them. The strategy itself is free text in
strategy.md and only guides the AI.
"""
from __future__ import annotations

import json
from typing import Dict

from pydantic import BaseModel, Field

DEFAULT_TRADING_CONFIG = "trading_config.json"


class TradingConfig(BaseModel):
    # Whose 13F portfolio to follow (SEC CIK number).
    follow_name: str = "Berkshire Hathaway"
    follow_cik: str = "0001067983"
    # CUSIP -> ticker, for holdings OpenFIGI maps wrongly or not at all.
    ticker_overrides: Dict[str, str] = Field(default_factory=dict)
    # Leave out 13F holdings smaller than this (% of the filing); 0 keeps all.
    min_weight_pct: float = Field(0.0, ge=0)
    # true: only symbols in the current target portfolio may be bought.
    only_target_symbols: bool = True

    # Rebalancing: trade a symbol when it is off target by more than this
    # % of its target value, and never for less than min_trade_usd.
    rebalance_drift_pct: float = Field(10.0, ge=0)
    min_trade_usd: float = Field(1.0, ge=1)

    # Risk limits, all checked before an order is sent.
    max_order_usd: float = Field(2000.0, gt=0)
    max_position_pct: float = Field(30.0, gt=0, le=100)      # of equity, per symbol
    min_cash_reserve_pct: float = Field(2.0, ge=0, lt=100)   # of equity, never spent
    max_orders_per_day: int = Field(60, ge=0)
    # Share-quantity buys have no price cap at market: their cost is
    # estimated at the last price plus this margin.
    market_slippage_pct: float = Field(1.0, ge=0)

    # Scheduling and agent limits.
    cycle_minutes: int = Field(1440, ge=1)
    max_steps_per_cycle: int = Field(60, ge=1)

    # Alpaca market data feed: "iex" works with free accounts, "sip" needs a subscription.
    data_feed: str = "iex"
    strategy_file: str = "strategy.md"
    log_dir: str = "logs"
    cache_dir: str = "cache"


def load_trading_config(path: str = DEFAULT_TRADING_CONFIG) -> TradingConfig:
    with open(path, encoding="utf-8") as f:
        return TradingConfig(**json.load(f))
