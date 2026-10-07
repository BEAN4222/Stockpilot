"""
One trading cycle:

    messages = [system prompt (rules + user's strategy), account + rebalance plan]
    repeat up to max_steps_per_cycle:
        response = llm.generate(messages, tools=TOOL_SCHEMAS)
        run every tool call, append the results
        stop when the AI calls finish(...) or answers without a tool call
        (an empty reply is not an answer: the AI is told so and continues)

Every cycle is appended to logs/<date>.jsonl: the full conversation, every
order attempt (accepted or rejected) and the AI's summary.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Optional

from trader.config import TradingConfig
from trader.guard import NEW_YORK
from trader.llm_provider import LLMProvider
from trader.tools import TOOL_SCHEMAS, ToolRunner

# After this many empty replies in a row the cycle stops with an error.
MAX_EMPTY_REPLIES = 3
EMPTY_REPLY_NOTICE = (
    "Your last reply was empty: no text and no tool call. Nothing was done. "
    "Continue the cycle from where you left off: call the tools you need, "
    "then finish(summary)."
)


def build_system_prompt(config: TradingConfig, strategy: str, live: bool, dry_run: bool) -> str:
    mode = "LIVE (real money)" if live else "PAPER (simulated money)"
    if dry_run:
        mode += ", DRY RUN (orders are only logged, never sent)"
    return f"""You are an autonomous US stock trading agent managing an Alpaca brokerage account.
Account mode: {mode}.

The account follows the portfolio of {config.follow_name}, as disclosed in its latest
SEC 13F filing. Code reads the filing and computes a rebalance plan (get_rebalance_plan):
target weights, current holdings, and proposed trades. Your job is to review that plan
and carry it out, applying the user's strategy and your own judgment.

Keep in mind:
- A 13F is filed up to 45 days after quarter end, so it can be months old. It shows only
  US long stock positions: not cash, not foreign stocks, and possibly not every holding
  (some can be kept confidential for a while).
- Holdings the code had to skip are listed in the plan with the reason.

How you work:
- Each cycle starts with the account state and the current plan. Use the tools for more
  data if needed, place the orders you decide on, then call finish(summary).
- Place sells before buys: their proceeds pay for the buys.
- Buy with notional_usd (a dollar amount; fractional shares are fine). Sell with qty or
  sell_all.
- You may deviate from the plan when the strategy or your judgment calls for it, and say
  why in the summary. Doing nothing is a valid decision.
- If an order comes back "rejected", read why and adapt; do not resend it unchanged.

Hard rules (enforced by the system; orders that break them are rejected):
- LONG ONLY. You can buy shares and sell shares you already hold. Short selling,
  margin, options and other derivatives are impossible.
- Day orders, market or limit. Dollar-amount orders are market buys only.
- Max ${config.max_order_usd:,.0f} per order.
- Max {config.max_position_pct}% of equity in any one symbol.
- Keep at least {config.min_cash_reserve_pct}% of equity in cash.
- Max {config.max_orders_per_day} orders per day.
- {"Only symbols in the target portfolio may be bought." if config.only_target_symbols else "Any US-listed stock may be bought."}

The user's strategy:
Follow it first. Where it is silent or unclear, use your own judgment, staying
consistent with its spirit and with prudent risk management.
<strategy>
{strategy.strip() or "(none given: follow the plan, favoring capital preservation)"}
</strategy>

Write the finish summary in the same language as the strategy."""


def build_snapshot(runner: ToolRunner, plan: dict) -> str:
    """The first user message: everything the AI needs to start, in one go."""
    clock = runner.broker.get_clock()
    now = datetime.now(NEW_YORK).strftime("%Y-%m-%d %H:%M %Z")
    sections = {
        "time_new_york": now,
        "market": {
            "is_open": clock.get("is_open"),
            "next_open": clock.get("next_open"),
            "next_close": clock.get("next_close"),
        },
        "account": runner.get_account(),
        "open_orders": runner.get_open_orders(),
        "rebalance_plan": plan,
    }
    return "New trading cycle. Current state:\n" + json.dumps(sections, indent=1, default=str)


def run_cycle(
    llm: LLMProvider,
    runner: ToolRunner,
    config: TradingConfig,
    system_prompt: str,
    plan: dict,
    log_extra: Optional[dict] = None,
) -> dict:
    started = time.perf_counter()
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": build_snapshot(runner, plan)},
    ]
    input_tokens = output_tokens = steps = empty_replies = 0
    summary, error = "", None

    try:
        for steps in range(1, config.max_steps_per_cycle + 1):
            response = llm.generate(messages, tools=TOOL_SCHEMAS)
            input_tokens += response.input_tokens
            output_tokens += response.output_tokens

            if not response.tool_calls and not response.text.strip():
                # Empty reply (some free models do this): it is not a decision.
                # Leave it out of the conversation (some providers reject empty
                # assistant messages) and tell the AI what happened.
                empty_replies += 1
                if empty_replies > MAX_EMPTY_REPLIES:
                    error = f"AI replied empty {empty_replies} times in a row"
                    break
                messages.append({"role": "user", "content": EMPTY_REPLY_NOTICE})
                continue
            empty_replies = 0
            messages.append(response.message)

            if not response.tool_calls:
                # Plain text answer: treat it as the end of the cycle.
                summary = response.text
                break
            for call in response.tool_calls:
                fn = call.get("function") or {}
                result = runner.run(fn.get("name", ""), fn.get("arguments") or "{}")
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": result})
            if runner.finished:
                summary = runner.summary
                break
        else:
            error = f"Reached max_steps_per_cycle ({config.max_steps_per_cycle}) without finish"
    except Exception as e:
        error = f"{type(e).__name__}: {e}"

    record = {
        "timestamp": datetime.now(NEW_YORK).isoformat(),
        **(log_extra or {}),
        "steps": steps,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "seconds": round(time.perf_counter() - started, 1),
        "orders": runner.orders,
        "summary": summary,
        "error": error,
        "messages": messages,
    }
    os.makedirs(config.log_dir, exist_ok=True)
    log_path = os.path.join(config.log_dir, datetime.now(NEW_YORK).strftime("%Y-%m-%d") + ".jsonl")
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    return record
