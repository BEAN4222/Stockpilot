"""Long-only and risk-limit checks, against a fake Alpaca API (no network)."""
import json

import pytest

from trader.broker import AlpacaBroker, OrderRejected
from trader.config import TradingConfig
from trader.guard import OrderGuard
from trader.tools import ToolRunner, compute_indicators


class FakeBroker(AlpacaBroker):
    """AlpacaBroker with the HTTP layer replaced by in-memory state, so the
    real submit_order / sellable_qty logic is what gets tested."""

    def __init__(self, positions=None, open_orders=None, cash=10_000.0, equity=10_000.0,
                 price=100.0, no_shorting=True, not_fractionable=()):
        self.positions = {s: str(q) for s, q in (positions or {}).items()}   # symbol -> qty
        self.open_orders = open_orders or []
        self.cash, self.equity, self.price = cash, equity, price
        self.config = {"no_shorting": no_shorting}
        self.not_fractionable = set(not_fractionable)
        self.allow_patch = True
        self.sent = []

    def _position(self, symbol):
        qty = float(self.positions[symbol])
        return {"symbol": symbol, "qty": self.positions[symbol], "market_value": str(qty * self.price),
                "current_price": str(self.price), "avg_entry_price": str(self.price),
                "unrealized_pl": "0", "unrealized_plpc": "0"}

    def _trading(self, method, path, **kwargs):
        if path == "/v2/account":
            return {"cash": str(self.cash), "equity": str(self.equity), "last_equity": str(self.equity)}
        if path == "/v2/account/configurations":
            if method == "PATCH" and self.allow_patch:
                self.config.update(kwargs["json"])
            return dict(self.config)
        if path == "/v2/clock":
            return {"is_open": True, "next_open": "", "next_close": ""}
        if path == "/v2/positions":
            return [self._position(s) for s in self.positions]
        if path.startswith("/v2/positions/"):
            symbol = path.rsplit("/", 1)[1]
            return self._position(symbol) if symbol in self.positions else None
        if path.startswith("/v2/assets/"):
            symbol = path.rsplit("/", 1)[1]
            return {"tradable": True, "status": "active",
                    "fractionable": symbol not in self.not_fractionable}
        if path == "/v2/orders" and method == "GET":
            params = kwargs.get("params", {})
            if params.get("status") == "open":
                return [o for o in self.open_orders
                        if "symbols" not in params or o["symbol"] == params["symbols"]]
            return list(self.sent)
        if path == "/v2/orders" and method == "POST":
            self.sent.append(kwargs["json"])
            return {"id": "abc", "status": "accepted", **kwargs["json"]}
        raise AssertionError(f"unexpected call {method} {path}")

    def _data(self, path, params):
        return {s: {"latestQuote": {"ap": self.price}} for s in params["symbols"].split(",")}


def make(**kwargs):
    broker = FakeBroker(**kwargs)
    config = TradingConfig(max_order_usd=5_000, max_position_pct=50,
                           min_cash_reserve_pct=10, max_orders_per_day=5)
    guard = OrderGuard(broker, config)
    guard.allowed_symbols = {"AAPL", "MSFT", "BRK.B"}
    return broker, guard


# ------------------------------------------------------------ long only
def test_sell_without_position_is_rejected():
    broker, guard = make()
    with pytest.raises(OrderRejected, match="Long-only"):
        guard.sell("AAPL", qty=1)
    with pytest.raises(OrderRejected, match="Long-only"):
        guard.sell("AAPL", sell_all=True)
    assert broker.sent == []


def test_sell_more_than_held_is_rejected():
    broker, guard = make(positions={"AAPL": 10})
    with pytest.raises(OrderRejected, match="only 10 sellable"):
        guard.sell("AAPL", qty=10.000001)
    assert broker.sent == []


def test_fractional_position_cannot_be_oversold():
    broker, guard = make(positions={"AAPL": "0.123456789"})
    with pytest.raises(OrderRejected, match="Long-only"):
        guard.sell("AAPL", qty="0.12345679")
    guard.sell("AAPL", qty="0.123456789")
    assert broker.sent[0]["qty"] == "0.123456789"


def test_sell_all_sells_exactly_the_fractional_position():
    broker, guard = make(positions={"AAPL": "3.5"})
    guard.sell("AAPL", sell_all=True)
    assert broker.sent == [{"symbol": "AAPL", "side": "sell", "type": "market",
                            "time_in_force": "day", "qty": "3.5"}]


def test_sell_counts_shares_already_in_open_sell_orders():
    open_sell = {"symbol": "AAPL", "side": "sell", "qty": "6", "filled_qty": "0"}
    broker, guard = make(positions={"AAPL": 10}, open_orders=[open_sell])
    with pytest.raises(OrderRejected, match="only 4 sellable"):
        guard.sell("AAPL", qty=5)
    guard.sell("AAPL", qty=4)
    assert len(broker.sent) == 1


def test_open_dollar_amount_sell_reserves_the_whole_position():
    open_sell = {"symbol": "AAPL", "side": "sell", "qty": None, "notional": "50"}
    broker, guard = make(positions={"AAPL": 10}, open_orders=[open_sell])
    with pytest.raises(OrderRejected, match="Long-only"):
        guard.sell("AAPL", qty=1)


def test_broker_itself_refuses_short_even_without_guard():
    broker = FakeBroker(positions={"AAPL": 3})
    with pytest.raises(OrderRejected, match="Long-only"):
        broker.submit_order("AAPL", "sell", qty=4)
    with pytest.raises(OrderRejected, match="Long-only"):
        broker.submit_order("MSFT", "sell", qty="0.001")
    assert broker.sent == []


def test_broker_refuses_dollar_amount_sells():
    broker = FakeBroker(positions={"AAPL": 3})
    with pytest.raises(OrderRejected, match="only allowed for market buys"):
        broker.submit_order("AAPL", "sell", notional=10)
    assert broker.sent == []


@pytest.mark.parametrize("side", ["sell_short", "short", "SELL", ""])
def test_broker_refuses_unknown_sides(side):
    broker = FakeBroker(positions={"AAPL": 3})
    with pytest.raises(OrderRejected):
        broker.submit_order("AAPL", side, qty=1)
    assert broker.sent == []


@pytest.mark.parametrize("qty", [0, -5, "-0.1", True, "abc", "inf", "nan", "0.0000000001", None])
def test_invalid_quantities_are_rejected(qty):
    broker, guard = make(positions={"AAPL": 10})
    with pytest.raises(OrderRejected):
        guard.sell("AAPL", qty=qty)
    with pytest.raises(OrderRejected):
        broker.submit_order("AAPL", "sell", qty=qty)
    assert broker.sent == []


def test_dry_run_still_enforces_long_only():
    broker = FakeBroker()
    guard = OrderGuard(broker, TradingConfig(), dry_run=True)
    with pytest.raises(OrderRejected, match="Long-only"):
        guard.sell("AAPL", qty=1)


def test_ensure_long_only_enables_no_shorting():
    broker = FakeBroker(no_shorting=False)
    broker.ensure_long_only()
    assert broker.config["no_shorting"] is True


def test_ensure_long_only_aborts_if_setting_cannot_be_enabled():
    broker = FakeBroker(no_shorting=False)
    broker.allow_patch = False
    with pytest.raises(RuntimeError, match="no_shorting"):
        broker.ensure_long_only()


def test_ensure_long_only_aborts_on_existing_short_position():
    broker = FakeBroker(positions={"AAPL": -5})
    with pytest.raises(RuntimeError, match="short positions"):
        broker.ensure_long_only()


def test_tool_sell_rejection_is_reported_to_the_ai():
    broker, guard = make()
    runner = ToolRunner(broker, guard, planner=None)
    out = json.loads(runner.run("sell", json.dumps(
        {"symbol": "AAPL", "qty": 1, "order_type": "market", "reason": "test"})))
    assert "Long-only" in out["rejected"]
    assert broker.sent == []


# ------------------------------------------------------------ buys and limits
def test_dollar_amount_buy():
    broker, guard = make()
    guard.buy("AAPL", notional=123.456)
    assert broker.sent[0] == {"symbol": "AAPL", "side": "buy", "type": "market",
                              "time_in_force": "day", "notional": "123.45"}


def test_fractional_share_buy():
    broker, guard = make()
    guard.buy("BRK.B", qty=0.5)
    assert broker.sent[0]["qty"] == "0.5"


def test_buy_needs_exactly_one_of_qty_or_notional():
    broker, guard = make()
    with pytest.raises(OrderRejected, match="exactly one"):
        guard.buy("AAPL")
    with pytest.raises(OrderRejected, match="exactly one"):
        guard.buy("AAPL", qty=1, notional=100)


def test_dollar_amount_must_be_market_and_at_least_one_dollar():
    broker, guard = make()
    with pytest.raises(OrderRejected, match="market"):
        guard.buy("AAPL", notional=100, order_type="limit", limit_price=99)
    with pytest.raises(OrderRejected, match=r"\$1"):
        guard.buy("AAPL", notional=0.5)


def test_non_fractionable_stock_needs_whole_shares():
    broker, guard = make(not_fractionable={"MSFT"})
    with pytest.raises(OrderRejected, match="fractions"):
        guard.buy("MSFT", notional=50)
    with pytest.raises(OrderRejected, match="fractions"):
        guard.buy("MSFT", qty=0.5)
    guard.buy("MSFT", qty=2)


def test_buy_outside_target_portfolio_is_rejected():
    broker, guard = make()
    with pytest.raises(OrderRejected, match="target portfolio"):
        guard.buy("TSLA", notional=10)


def test_buy_over_max_order_is_rejected():
    broker, guard = make()
    with pytest.raises(OrderRejected, match="max_order_usd"):
        guard.buy("AAPL", notional=5_001)


def test_buy_never_uses_more_than_cash_minus_reserve():
    broker, guard = make(cash=1_500, equity=10_000)   # reserve $1,000 -> $500 spendable
    with pytest.raises(OrderRejected, match="Not enough cash"):
        guard.buy("AAPL", notional=501)
    guard.buy("AAPL", notional=500)


def test_open_dollar_buys_count_against_cash():
    open_buy = {"symbol": "MSFT", "side": "buy", "qty": None, "notional": "400"}
    broker, guard = make(cash=1_500, equity=10_000, open_orders=[open_buy])
    with pytest.raises(OrderRejected, match="Not enough cash"):
        guard.buy("AAPL", notional=200)


def test_buy_over_position_limit_is_rejected():
    broker, guard = make(positions={"AAPL": 45})       # $4,500 of a $5,000 cap
    with pytest.raises(OrderRejected, match="% of equity"):
        guard.buy("AAPL", notional=600)


def test_daily_order_limit():
    broker, guard = make(positions={"AAPL": 100})
    for _ in range(5):
        guard.sell("AAPL", qty=1)
    with pytest.raises(OrderRejected, match="Daily order limit"):
        guard.sell("AAPL", qty=1)


# ------------------------------------------------------------ full cycle
class FakeLLM:
    base_url, model_name = "fake", "fake"

    def __init__(self, calls):
        self.turns = [[{"id": f"c{i}", "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)}}]
                      for i, (name, args) in enumerate(calls)]

    def generate(self, messages, tools=None):
        from trader.llm_provider import LLMResponse
        calls = self.turns.pop(0)
        return LLMResponse(text="", message={"role": "assistant", "tool_calls": calls},
                           tool_calls=calls)


def test_full_cycle_short_attempt_is_blocked_and_logged(tmp_path):
    from trader.agent import run_cycle
    broker, guard = make()
    guard.config.log_dir = str(tmp_path)
    llm = FakeLLM([
        ("sell", {"symbol": "AAPL", "qty": 5, "order_type": "market", "reason": "short it"}),
        ("buy", {"symbol": "AAPL", "notional_usd": 250, "order_type": "market", "reason": "plan"}),
        ("finish", {"summary": "done"}),
    ])
    record = run_cycle(llm, ToolRunner(broker, guard, planner=None), guard.config, "system",
                       plan={"trades": []})
    assert record["error"] is None and record["summary"] == "done"
    assert "Long-only" in record["orders"][0]["rejected"]
    assert broker.sent == [{"symbol": "AAPL", "side": "buy", "type": "market",
                            "time_in_force": "day", "notional": "250.00"}]
    assert len(list(tmp_path.glob("*.jsonl"))) == 1


def test_indicators():
    bars = [{"o": c, "h": c + 1, "l": c - 1, "c": c, "v": 1000} for c in range(1, 61)]
    ind = compute_indicators([{k: float(v) for k, v in b.items()} for b in bars])
    assert ind["sma20"] == 50.5
    assert ind["rsi14"] == 100.0
    assert ind["high20"] == 61.0
