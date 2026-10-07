"""13F parsing, amendments, targets and the rebalance plan (no network)."""
from trader.config import TradingConfig
from trader.portfolio import Target, build_targets, make_plan
from trader.sec13f import Filing13F, Holding, combine, parse_cover, parse_info_table

INFO_TABLE = """<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><titleOfClass>COM</titleOfClass>
    <cusip>037833100</cusip><value>600</value>
    <shrsOrPrnAmt><sshPrnamt>6</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt></infoTable>
  <infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><titleOfClass>COM</titleOfClass>
    <cusip>037833100</cusip><value>200</value>
    <shrsOrPrnAmt><sshPrnamt>2</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt></infoTable>
  <infoTable><nameOfIssuer>COCA COLA CO</nameOfIssuer><titleOfClass>COM</titleOfClass>
    <cusip>191216100</cusip><value>200</value>
    <shrsOrPrnAmt><sshPrnamt>4</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt></infoTable>
  <infoTable><nameOfIssuer>SOME CORP</nameOfIssuer><titleOfClass>PUT</titleOfClass>
    <cusip>999999999</cusip><value>500</value>
    <shrsOrPrnAmt><sshPrnamt>5</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
    <putCall>Put</putCall></infoTable>
  <infoTable><nameOfIssuer>BOND CO</nameOfIssuer><titleOfClass>NOTE</titleOfClass>
    <cusip>888888888</cusip><value>500</value>
    <shrsOrPrnAmt><sshPrnamt>5</sshPrnamt><sshPrnamtType>PRN</sshPrnamtType></shrsOrPrnAmt></infoTable>
</informationTable>"""

COVER_AMENDMENT = """<edgarSubmission xmlns="http://www.sec.gov/edgar/thirteenffiler"><formData>
  <coverPage><isAmendment>true</isAmendment>
    <amendmentInfo><amendmentType>NEW HOLDINGS</amendmentType></amendmentInfo></coverPage>
  <summaryPage><isConfidentialOmitted>false</isConfidentialOmitted></summaryPage>
</formData></edgarSubmission>"""


def h(cusip, value, issuer="X"):
    return Holding(cusip=cusip, issuer=issuer, title="COM", shares=1, value=value)


def test_info_table_keeps_shares_only():
    rows = parse_info_table(INFO_TABLE)
    assert {r.cusip for r in rows} == {"037833100", "191216100"}   # no put, no bond


def test_rows_are_summed_per_cusip():
    holdings = combine([{"amendment_type": "", "holdings": parse_info_table(INFO_TABLE)}])
    assert [(x.cusip, x.value, x.shares) for x in holdings] == [
        ("037833100", 800, 8), ("191216100", 200, 4)]


def test_amendment_cover_is_parsed():
    assert parse_cover(COVER_AMENDMENT) == {"amendment_type": "NEW HOLDINGS",
                                            "confidential_omitted": False}


def test_new_holdings_amendment_adds_and_restatement_replaces():
    original = {"amendment_type": "", "holdings": [h("A", 100)]}
    new = {"amendment_type": "NEW HOLDINGS", "holdings": [h("B", 50)]}
    restated = {"amendment_type": "RESTATEMENT", "holdings": [h("C", 10)]}
    assert [x.cusip for x in combine([original, new])] == ["A", "B"]
    assert [x.cusip for x in combine([original, new, restated])] == ["C"]


def filing(*holdings):
    return Filing13F(cik="1", report_date="2026-06-30", filed_date="2026-08-14",
                     accessions=["x"], confidential_omitted=False, holdings=list(holdings))


ASSET = {"tradable": True, "status": "active", "fractionable": True}


def test_targets_skip_unmapped_and_untradable_and_renormalize():
    f = filing(h("A", 50, "Apple"), h("K", 30, "Coke"), h("U", 10, "Unknown"), h("N", 10, "Nope"))
    tickers = {"A": "AAPL", "K": "KO", "U": None, "N": "NOPE"}
    assets = {"AAPL": ASSET, "KO": ASSET, "NOPE": {**ASSET, "fractionable": False}}
    targets, skipped = build_targets(f, tickers, assets, TradingConfig())
    assert [(t.symbol, round(t.weight_pct, 2), round(t.filing_pct, 2)) for t in targets] == [
        ("AAPL", 62.5, 50.0), ("KO", 37.5, 30.0)]
    assert {s["cusip"] for s in skipped} == {"U", "N"}


def pos(symbol, qty, price):
    return {"symbol": symbol, "qty": str(qty), "market_value": str(qty * price),
            "current_price": str(price)}


def config(**kw):
    base = dict(min_cash_reserve_pct=0, max_position_pct=100, max_order_usd=10_000,
                rebalance_drift_pct=10, min_trade_usd=1)
    return TradingConfig(**{**base, **kw})


def test_plan_from_empty_account_buys_everything():
    targets = [Target("AAPL", "Apple", 60, 60), Target("KO", "Coke", 40, 40)]
    plan = make_plan(targets, [], [], 1000, config())
    assert [(t["symbol"], t["notional_usd"]) for t in plan["trades"]] == [("AAPL", 600), ("KO", 400)]


def test_plan_sells_dropped_stocks_first_and_trims_overweight():
    targets = [Target("AAPL", "Apple", 50, 50), Target("KO", "Coke", 50, 50)]
    positions = [pos("AAPL", 8, 100), pos("V", 2, 100)]          # AAPL $800, V $200 (dropped)
    plan = make_plan(targets, positions, [], 1000, config())
    trades = [(t["action"], t["symbol"], t.get("sell_all"), t.get("qty"), t.get("notional_usd"))
              for t in plan["trades"]]
    assert trades == [("sell", "AAPL", None, "3", None),         # $800 -> $500
                      ("sell", "V", True, None, None),
                      ("buy", "KO", None, None, 500)]


def test_plan_ignores_small_drift_and_symbols_with_open_orders():
    targets = [Target("AAPL", "Apple", 50, 50), Target("KO", "Coke", 50, 50)]
    positions = [pos("AAPL", 5.2, 100)]                          # $520 vs $500: within 10%
    plan = make_plan(targets, positions, [{"symbol": "KO"}], 1000, config())
    assert plan["trades"] == []
    assert plan["waiting_on_open_orders"] == ["KO"]


def test_plan_respects_cash_reserve_position_cap_and_splits_big_buys():
    targets = [Target("AAPL", "Apple", 100, 100)]
    plan = make_plan(targets, [], [], 10_000,
                     config(min_cash_reserve_pct=10, max_position_pct=30, max_order_usd=1_000))
    assert [t["notional_usd"] for t in plan["trades"]] == [1000, 1000, 1000]   # capped at $3,000
