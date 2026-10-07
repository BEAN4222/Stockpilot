"""
Stock trader CLI.

    uv run python -m trader --plan          # print the rebalance plan, no AI, no orders
    uv run python -m trader --dry-run       # one cycle, orders only logged
    uv run python -m trader                 # one cycle on the paper account
    uv run python -m trader --loop          # every cycle_minutes while the market is open
    uv run python -m trader --loop --live   # real money

The AI is only called when the plan has trades to make, so a balanced
portfolio costs no LLM tokens. Model selection: models.json "default", or
--provider / --model-name / --provider-url (see trader/model_config.py).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime

from trader.agent import build_system_prompt, run_cycle
from trader.broker import AlpacaBroker
from trader.config import DEFAULT_TRADING_CONFIG, load_trading_config
from trader.env import load_env_file
from trader.guard import OrderGuard
from trader.llm_provider import LLMProvider
from trader.model_config import DEFAULT_MODELS_CONFIG, resolve_model
from trader.portfolio import Planner
from trader.tools import ToolRunner

# While the market is closed, check the clock again at least this often.
MAX_IDLE_SLEEP_SECONDS = 3600


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="trader")
    parser.add_argument("--plan", action="store_true",
                        help="only print the current rebalance plan (no AI, no orders)")
    parser.add_argument("--loop", action="store_true",
                        help="keep running a cycle every cycle_minutes during market hours")
    parser.add_argument("--live", action="store_true",
                        help="trade the LIVE account with real money (default: paper)")
    parser.add_argument("--dry-run", action="store_true",
                        help="never send orders, only log what would have been sent")
    parser.add_argument("--config", default=DEFAULT_TRADING_CONFIG)
    parser.add_argument("--model-name", default=None)
    parser.add_argument("--provider-url", default=None)
    parser.add_argument("--provider", default=None, help="provider name from models.json")
    parser.add_argument("--api-key-env", default=None)
    parser.add_argument("--model-config", default=DEFAULT_MODELS_CONFIG)
    return parser.parse_args(argv)


def _seconds_until(iso: str) -> float:
    return (datetime.fromisoformat(iso) - datetime.now().astimezone()).total_seconds()


def print_plan(plan: dict) -> None:
    f = plan["filing"]
    print(f"[plan] {plan['following']}: 13F for {f['report_period']}, filed {f['filed']}"
          + (" (some holdings kept confidential)" if f["some_holdings_kept_confidential"] else ""))
    print(f"[plan] equity ${plan['equity']:,.2f}, investable ${plan['investable_usd']:,.2f}")
    print(f"  {'symbol':8} {'13F%':>6} {'target%':>8} {'target$':>11} {'current$':>11}")
    for r in plan["positions"]:
        print(f"  {r['symbol']:8} {r['filing_pct']:6.2f} {r['target_pct']:8.2f} "
              f"{r['target_usd']:11,.2f} {r['current_usd']:11,.2f}")
    for s in plan["skipped_holdings"]:
        print(f"  skipped {s['symbol'] or s['cusip']} ({s['issuer']}, {s['filing_pct']}%): {s['reason']}")
    if plan["waiting_on_open_orders"]:
        print(f"  waiting on open orders: {', '.join(plan['waiting_on_open_orders'])}")
    print(f"[plan] {len(plan['trades'])} trades:")
    for t in plan["trades"]:
        print("  " + json.dumps(t))


def main(argv=None) -> None:
    args = parse_args(argv)
    load_env_file(".env")
    config = load_trading_config(args.config)

    broker = AlpacaBroker(live=args.live, data_feed=config.data_feed)
    planner = Planner(broker, config, config.cache_dir)
    if args.plan:
        planner.refresh()
        print_plan(planner.plan())
        return

    with open(config.strategy_file, encoding="utf-8") as f:
        strategy = f.read()
    model_name, provider_url, api_key_env = resolve_model(
        args.model_config, args.provider, args.model_name, args.provider_url, args.api_key_env
    )
    llm = LLMProvider(model_name, provider_url, api_key_env)
    # Refuses to continue unless the account is in long-only mode.
    broker.ensure_long_only()
    guard = OrderGuard(broker, config, dry_run=args.dry_run)
    system_prompt = build_system_prompt(config, strategy, args.live, args.dry_run)

    mode = "LIVE" if args.live else "paper"
    print(f"[trader] {mode} account, model={model_name}"
          + (", DRY RUN" if args.dry_run else ""), flush=True)
    if args.live:
        print("[trader] WARNING: trading with REAL money. Ctrl+C within 10s to abort.", flush=True)
        time.sleep(10)

    log_extra = {"mode": mode, "dry_run": args.dry_run, "model": model_name}

    def cycle() -> None:
        planner.refresh()
        guard.allowed_symbols = planner.target_symbols
        plan = planner.plan()
        if not plan["trades"]:
            print(f"[trader] portfolio is on target ({plan['filing']['report_period']} 13F), "
                  "nothing to do", flush=True)
            return
        print(f"[trader] {len(plan['trades'])} proposed trades, asking the AI", flush=True)
        # A fresh runner per cycle: its finished flag and order list are per cycle.
        runner = ToolRunner(broker, guard, planner)
        record = run_cycle(llm, runner, config, system_prompt, plan, log_extra)
        for order in record["orders"]:
            amount = f"${order['notional_usd']}" if order.get("notional_usd") is not None \
                else f"{order['qty']} sh"
            status = "REJECTED " + order["rejected"] if "rejected" in order else "ok"
            print(f"  {order['side']} {amount} {order['symbol']} ({order['order_type']}): {status}")
        print(f"[trader] cycle done in {record['seconds']}s, {record['steps']} steps. "
              f"{record['summary'] or ''}" + (f" ERROR: {record['error']}" if record["error"] else ""),
              flush=True)

    if not args.loop:
        if not broker.get_clock().get("is_open") and not args.dry_run:
            print("[trader] market is closed; nothing sent. Use --plan or --dry-run to look ahead.")
            return
        cycle()
        return

    while True:
        try:
            clock = broker.get_clock()
            if clock.get("is_open"):
                cycle()
                wait = config.cycle_minutes * 60
            else:
                wait = min(MAX_IDLE_SLEEP_SECONDS, max(60, _seconds_until(clock["next_open"]) + 60))
                print(f"[trader] market closed, next open {clock['next_open']}", flush=True)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            # A network blip must not kill a long-running loop.
            print(f"[trader] error: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            wait = 60
        time.sleep(wait)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except KeyboardInterrupt:
        print("\n[trader] stopped")
