# StockTrader

An automated US stock trader on Alpaca that follows **Berkshire Hathaway's latest 13F
portfolio** with fractional shares. Code reads the filing and builds a rebalance plan;
an AI reviews the plan using `strategy.md` (your strategy) plus its own judgment, and
places the orders. It is **long only**: short selling is blocked at every level.

> Rewritten from a coding-agent project (Agent Smith), keeping only the LLM-calling part.

## How it works

1. **Filing** (`trader/sec13f.py`): downloads Berkshire's latest quarterly 13F from SEC
   EDGAR. Amendments disclosed later (13F-HR/A "NEW HOLDINGS") are merged in; options and
   bonds are dropped. CUSIPs are mapped to tickers through OpenFIGI.
2. **Target weights** (`trader/portfolio.py`): holdings Alpaca cannot trade fractionally
   (and similar) are dropped and the rest is rescaled to 100%. Anything above the
   per-symbol weight cap stays in cash.
3. **Rebalance plan**: compares target amounts with current holdings.
   - Symbol no longer in the filing → sell all
   - More than `rebalance_drift_pct`% under or over target → buy/sell the difference
   - Buys are **dollar amounts** (e.g. AAPL $229.30), sells are **share quantities**
     (fractions included)
4. **AI review and execution** (`trader/agent.py`): the AI is only called when there is
   something to trade. It places orders based on the plan and the strategy, and every
   order goes through the limit checks in `OrderGuard`.
5. **Logging**: the full conversation and every order (rejected ones included) are written
   to `logs/<date>.jsonl`.

`--loop` repeats this every `cycle_minutes` (one day by default) during market hours. A new
filing is picked up automatically on the next cycle.

### Limits of 13F
- Filed up to 45 days after quarter end, so the portfolio you follow is 1.5 to 4.5 months old.
- Shows only long positions in US-listed stocks (no cash weight, no foreign stocks).
- Berkshire may delay disclosing some holdings (they appear later in an amendment).
- Greg Abel has been CEO since 2026, so the latest filings reflect his decisions.

## Short-selling protection (3 layers)

| Layer | Where | What |
|---|---|---|
| 1 | `trader/tools.py` | The AI only has `buy`/`sell`. There are no short, margin or option tools at all |
| 2 | `trader/guard.py`, `trader/broker.py` | A sell is rejected if its quantity > (shares held − shares in open sell orders). Sells are taken in shares only, so fractions compare exactly. `submit_order`, the only function that sends orders, checks again |
| 3 | Alpaca account setting | On startup the account's `no_shorting` (long-only mode) is turned on; if it cannot be confirmed, or a short position already exists, **the program refuses to run** |

Buys use **cash only**; margin buying power is never used.
Alpaca also never treats fractional orders as short sales.

## Setup

### 1. Install
```bash
cp .env.example .env     # fill in Alpaca keys, SEC contact email, LLM key
uv sync
```
- Alpaca keys: https://app.alpaca.markets (paper and live accounts have different keys)
- `SEC_USER_AGENT`: SEC policy requires a contact email, e.g. `"StockTrader you@example.com"`

### 2. Strategy — `strategy.md`
Written in plain language. The AI follows it **first** and uses its own judgment where it
is silent (e.g. "buy stocks that rose a lot since the filing in several steps").

### 3. Safety limits and rebalancing — `trading_config.json`
These are enforced in code, so the AI cannot ignore them.

| Key | Meaning | Default |
|---|---|---|
| `follow_name`, `follow_cik` | Manager to follow (SEC CIK number) | Berkshire, 0001067983 |
| `ticker_overrides` | Manual ticker for wrongly mapped holdings `{"CUSIP": "TICKER"}` | {} |
| `min_weight_pct` | Holdings below this weight (%) are skipped | 0 (include all) |
| `only_target_symbols` | If true, only symbols in the target portfolio can be bought | true |
| `rebalance_drift_pct` | Rebalance when off target by at least this % | 10 |
| `min_trade_usd` | Minimum order amount ($; Alpaca minimum is $1) | 1 |
| `max_order_usd` | Maximum per order ($). Larger buys are split automatically | 2000 |
| `max_position_pct` | Maximum weight of one symbol (% of equity) | 30 |
| `min_cash_reserve_pct` | Cash always kept (%) | 2 |
| `max_orders_per_day` | Maximum orders per day | 60 |
| `cycle_minutes` | `--loop` interval (minutes) | 1440 (one day) |
| `max_steps_per_cycle` | Maximum AI replies in one cycle | 60 |
| `data_feed` | Market data feed (`iex` free, `sip` paid) | iex |

Apple is above 20%, so if you set `max_position_pct` lower than that, Apple is bought only
up to the cap.

### 4. AI model — `models.json`
Picks a model on an OpenAI-compatible API (OpenRouter, Gemini, ...).
**The model must support function calling (tool calling).**

```bash
--provider gemini                             # another provider
--provider gemini --model-name <model>        # a specific model
--model-name <model> --provider-url <url>     # fully custom
```

## Running

```bash
uv run python -m trader --plan           # print the rebalance plan only (no AI, no orders) — check this first
uv run python -m trader --dry-run        # one cycle; the AI decides but orders are only logged
uv run python -m trader                  # one cycle on the paper account (market hours only)
uv run python -m trader --loop           # repeat daily on the paper account
uv run python -m trader --loop --live    # live account (real money; starts after a 10s wait)
```

The **paper account** is always the default; the live account is used only with `--live`.
Every holding in the account is treated as part of the strategy, so **symbols not in the
target portfolio go into the sell plan.** Do not use it on an account that holds stocks for
other purposes.

## Tests
```bash
uv run --extra dev pytest
```
Checks short-selling protection (fractions included), every limit, 13F parsing and
amendment handling, and the rebalance plan, all without network access.

## Layout

| File | Role |
|---|---|
| `trader/__main__.py` | CLI: print plan, single run, loop |
| `trader/agent.py` | System prompt, AI cycle loop, logging |
| `trader/tools.py` | Tools the AI can use and their execution, technical indicators |
| `trader/portfolio.py` | Target weights, rebalance plan |
| `trader/sec13f.py` | SEC 13F download, CUSIP→ticker mapping (cache: `cache/`) |
| `trader/guard.py` | Order gate: long only + risk limits |
| `trader/broker.py` | Alpaca REST client (the only place orders are sent) |
| `trader/config.py` | Loads `trading_config.json` |
| `trader/llm_provider.py` | OpenAI-compatible LLM calls, key rotation and retries |
| `trader/model_config.py` | Model selection from `models.json` |

## Disclaimer

This program is not investment advice. Filing delays and AI mistakes can cause losses.
Validate it thoroughly on a paper account before going live, and start small with real money.
