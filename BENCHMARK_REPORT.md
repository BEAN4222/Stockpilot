# Benchmark Report

All numbers below come from the `solution.json` / `validate.txt` files under
[`benchmark/runs/`](benchmark/runs/) and were aggregated with
[`benchmark/aggregate.py`](benchmark/aggregate.py) into
[`benchmark/tables.md`](benchmark/tables.md).

## 1. Setup

### Models

Every model was used on a free tier only: the free (`:free`) models on
OpenRouter, plus Google's default Gemini Flash model on Google AI Studio.

| Run name | Model id | Provider | Endpoint |
|---|---|---|---|
| `qwen3.8-27b` | `qwen/qwen3.8-27b:free` | OpenRouter | `https://openrouter.ai/api/v1` |
| `nemotron-3-ultra` | `nvidia/nemotron-3-ultra-550b-a55b:free` | OpenRouter | `https://openrouter.ai/api/v1` |
| `north-mini-code` | `cohere/north-mini-code:free` | OpenRouter | `https://openrouter.ai/api/v1` |
| `laguna-s-2.1` | `poolside/laguna-s-2.1:free` | OpenRouter | `https://openrouter.ai/api/v1` |
| `gemini-3.6-flash` | `gemini-3.6-flash` | Google AI Studio | `https://generativelanguage.googleapis.com/v1beta/openai` |

### Tasks

| Task | Repository | Test runner used by the eval script | Issue |
|---|---|---|---|
| `django__django-11066` | django/django | Django's own `tests/runtests.py` (unittest) | `RenameContentType._rename()` saves the content type on the wrong database |
| `pydata__xarray-4629` | pydata/xarray | `pytest -rA` | `merge(combine_attrs='override')` references the first object's `attrs` instead of copying them |
| `sympy__sympy-18189` | sympy/sympy | sympy's own `bin/test` | `diophantine(..., permute=True)` returns incomplete results depending on `syms` order |

**Why these tasks.** We chose the three tasks for diversity rather than difficulty.
They come from three different repositories, and each repository uses a
different test runner: unittest through Django's runner, pytest, and sympy's
custom runner. Each runner prints a different output format, and each codebase
has a different layout. We wanted to see whether a model can handle a project
whose structure and test output look different from the last one, and not
just one familiar format.

### Procedure

- Each model ran the same agent code (`agent_swebench`) on all three tasks
  via [`benchmark/run.sh`](benchmark/run.sh). One process ran per task, with
  a fresh task container each time.
- Every run had the same hard limits: 30 iterations, 300k input tokens, 10k
  output tokens, and 900 s. Every request used `temperature = 0`.
- **Pass/Fail is the moulinette verdict** (`moulinette_eval validate swebench`,
  the `Overall:` line). It is not the agent's own `success` flag, which only
  means that `final_answer()` was called.
- We graded locally under rootless Podman instead of Docker. Moulinette's
  helper that copies the patch into the container failed there on file
  ownership (`lchown ... invalid argument`), so we patched that copy step
  locally. Grading logic, tests and patches were not modified.

## 2. Results Table

| Model | Task | Pass/Fail | Iterations | Input tokens | Output tokens | Wall-clock (s) |
|---|---|---|---|---|---|---|
| qwen3.8-27b | django__django-11066 | **PASS** | 4 | 13,604 | 198 | 39.1 |
| qwen3.8-27b | pydata__xarray-4629 | **PASS** | 4 | 13,349 | 212 | 45.2 |
| qwen3.8-27b | sympy__sympy-18189 | **PASS** | 4 | 13,081 | 237 | 53.3 |
| gemini-3.6-flash | django__django-11066 | **PASS** | 9 | 42,071 | 564 | 129.8 |
| gemini-3.6-flash | pydata__xarray-4629 | **PASS** | 10 | 65,893 | 451 | 72.0 |
| gemini-3.6-flash | sympy__sympy-18189 | FAIL ¹ | 3 | 13,011 | 2,213 | 52.4 |
| laguna-s-2.1 | django__django-11066 | **PASS** | 6 | 26,800 | 498 | 38.8 |
| laguna-s-2.1 | pydata__xarray-4629 | FAIL ¹ | 21 | 78,710 | 1,606 | 84.8 |
| laguna-s-2.1 | sympy__sympy-18189 | **PASS** ² | 1 | 2,711 | 242 | 35.7 |
| nemotron-3-ultra | django__django-11066 | FAIL | 30 | 137,129 | 959 | 82.5 |
| nemotron-3-ultra | pydata__xarray-4629 | **PASS** | 15 | 58,480 | 1,919 | 117.1 |
| nemotron-3-ultra | sympy__sympy-18189 | **PASS** | 13 | 44,401 | 946 | 114.7 |
| north-mini-code | django__django-11066 | **PASS** | 7 | 22,907 | 420 | 18.4 |
| north-mini-code | pydata__xarray-4629 | FAIL | 30 | 234,000 | 1,511 | 38.1 |
| north-mini-code | sympy__sympy-18189 | FAIL ¹ | 20 | 151,020 | 1,796 | 46.6 |

¹ Run aborted by an HTTP 429 from the provider (see section 3).
² Suspicious pass, see section 6.

**Per-model totals** (3 tasks each):

| Model | Passed | Iterations | Input tokens | Output tokens | Wall-clock (s) |
|---|---|---|---|---|---|
| **qwen3.8-27b** | **3/3** | **12** | **40,034** | **647** | 137.6 |
| gemini-3.6-flash | 2/3 | 22 | 120,975 | 3,228 | 254.2 |
| laguna-s-2.1 | 2/3 | 28 | 108,221 | 2,346 | 159.3 |
| nemotron-3-ultra | 2/3 | 58 | 240,010 | 3,824 | 314.3 |
| north-mini-code | 1/3 | 57 | 407,927 | 3,727 | **103.1** |

## 3. Provider Reliability

| Model | Provider | Avg response time (ms) | Requests | Retries | Runs without provider failure |
|---|---|---|---|---|---|
| qwen3.8-27b | OpenRouter | 2,983 | 12 | 0 | 3/3 |
| gemini-3.6-flash | Google AI Studio | 7,060 | 24 | 2 | 2/3 |
| laguna-s-2.1 | OpenRouter | 3,929 | 32 | 4 | 2/3 |
| nemotron-3-ultra | OpenRouter | 4,392 | 58 | 0 | 3/3 |
| north-mini-code | OpenRouter | 913 | 57 | 0 | 2/3 |

*Retries* are requests the agent's provider layer re-sent after a 429 or 5xx
response, rotating to the next API key each time. A run that was aborted
because a request still failed after all retries counts as a provider
failure.

The three provider failures:

| Run | Step | Error | Cause |
|---|---|---|---|
| gemini-3.6-flash / sympy | 4 | 429 `You exceeded your current quota` | Google AI Studio free quota exhausted |
| laguna-s-2.1 / xarray | 22 | 429 `Provider returned error` | Upstream provider rate limit (OpenRouter forwarded it) |
| north-mini-code / sympy | 21 | 429 `Rate limit exceeded: free-models-per-day` | OpenRouter free tier: 50 requests/day **per account, across all models** |

- **north-mini-code** has the fastest responses (0.9 s). **gemini-3.6-flash**
  has the slowest (7.1 s), but it is the only model on a separate quota.
- Rotating API keys does **not** help with OpenRouter's daily limit, because
  every key of an account shares the same 50-requests/day budget. The models
  that spend the most steps (nemotron 58, north 57) use up that budget for
  every other OpenRouter model too. A second *provider* (here Google AI
  Studio) is the only real mitigation, so Gemini is useful as a fallback.

## 4. Intermediary Metrics

We report two of the three suggested metrics: **exploration efficiency**
(the first step whose code names a file that appears in the final patch) and
**submission discipline** (the number of steps between the first step where
`run_tests()` reports passing tests and `final_answer`, where 0 is ideal). We
also report the first step at which tests pass, which these two metrics are
measured against. A `-` means that the event never happened.

| Model | Task | First step touching a patched file | First step tests pass | Extra steps before `final_answer` |
|---|---|---|---|---|
| qwen3.8-27b | django | 1 | 3 | 0 |
| qwen3.8-27b | xarray | 1 | 3 | 0 |
| qwen3.8-27b | sympy | 1 | 3 | 0 |
| gemini-3.6-flash | django | 3 | 7 | 1 |
| gemini-3.6-flash | xarray | 3 | 8 | 1 |
| gemini-3.6-flash | sympy | - | - | - |
| laguna-s-2.1 | django | 1 | 5 | 0 |
| laguna-s-2.1 | xarray | - | - | - |
| laguna-s-2.1 | sympy | 1 | 1 | 0 |
| nemotron-3-ultra | django | - | 30 | - |
| nemotron-3-ultra | xarray | 2 | 12 | 2 |
| nemotron-3-ultra | sympy | 1 | 8 | 4 |
| north-mini-code | django | 3 | 6 | 0 |
| north-mini-code | xarray | - | 7 | - |
| north-mini-code | sympy | - | - | - |

We also counted **steps without executable code**. In these steps the
model's reply contained no code block that our extractor could run, either
because the reply was empty or because it was in a tool-call format we do not
parse. This turned out to be the main reason for failure:

| Model | django | xarray | sympy | Main reason |
|---|---|---|---|---|
| qwen3.8-27b | 0/4 | 0/4 | 0/4 | - |
| gemini-3.6-flash | 1/9 | 1/10 | 0/3 | one empty first reply |
| laguna-s-2.1 | 0/6 | **21/21** | 0/1 | emits its own `<tool_call>read_file<arg_key>…</arg_key>` syntax |
| nemotron-3-ultra | **14/30** | 2/15 | 4/13 | empty replies |
| north-mini-code | 1/7 | 2/30 | **15/20** | emits JSON `{"tool_calls": [...]}` objects |

What the metrics show:

- **qwen3.8-27b** has the same ideal trace on all three tasks:
  `read_file → edit_file → run_tests → get_patch + final_answer`. It touches
  the right file at step 1, tests pass at step 3, and it submits with no
  extra steps.
- **gemini-3.6-flash** explores first (`search_code`, then `read_file`) and
  reaches the right file at step 3. It then submits one step after the tests
  pass, so its discipline is good.
- **nemotron-3-ultra** wastes steps on both sides. Half of its django steps
  were empty replies, and it kept working for 2 and 4 steps after the tests
  already passed.
- In two failed runs, `run_tests()` reported passing tests but no patch was
  ever submitted:
  - **nemotron / django**: tests passed at step 30, the last allowed
    iteration.
  - **north / xarray**: tests passed at step 7, after which the model
    called `search_code` 23 more times until it hit the iteration limit.

  We count these as submission-discipline failures, not reasoning failures.
- **Caveat:** "tests pass" is detected from the text that `run_tests()`
  prints, which differs by runner. It can also be triggered by tests that
  passed before the fix. The column is therefore an approximation, and the
  moulinette verdict is authoritative.

## 5. Ablation Study

**Change:** we removed the worked SWE-bench example from the system prompt.
This is the 43-line `Vector.__add__` trace in `build_system_prompt()` in
`common/agent_loop.py`, which shows `search → read → edit + run_tests →
get_patch + final_answer`. The exact diff is in
[`benchmark/runs/qwen3.8-27b-no-example/change.diff`](benchmark/runs/qwen3.8-27b-no-example/change.diff).
Everything else stayed the same: model (`qwen3.8-27b`), tasks, limits,
`temperature = 0` and tools. We reverted the change after the run.

| Task | With example (baseline) | Without example |
|---|---|---|
| django__django-11066 | **PASS**, 4 iterations, 13,604 in / 198 out | **FAIL**, 30 iterations (limit), 93,463 in / 770 out |
| pydata__xarray-4629 | **PASS**, 4 iterations, 13,349 in / 212 out | aborted at step 5 by OpenRouter's daily limit (429), no patch |
| sympy__sympy-18189 | **PASS**, 4 iterations, 13,081 in / 237 out | aborted at step 1 by OpenRouter's daily limit (429) |

Only django is a complete comparison. For the other two tasks the ablation
runs hit the shared 50-requests/day OpenRouter limit, so their results are
inconclusive. The partial xarray trace still shows the same failure pattern
as django.

**What went wrong without the example (django):**

- The model called tools as bare expressions, e.g.
  `read_file(filepath=..., start_line=1, end_line=40)`, without `print()`.
  The sandbox only returns what is printed, so the observation was
  `(no output, use print())`.
- At step 2 it tried `open(...)` on a repository path from the host sandbox,
  which the path guard blocked (`PermissionError`).
- After that it called `list_files` three times and `run_command` 24 times,
  all without `print()`. **27 of 30 steps returned no information**, and the
  model never adapted, even though the observation explicitly says
  `use print()`.
- On xarray it made the same mistake in steps 1, 2 and 4, and the only
  useful step (3) was the one where it happened to print.

**Interpretation:** the worked example does not mainly teach a
problem-solving strategy, because qwen found the right files without it. It
teaches the **I/O convention of the agent**: tool results are visible only
through `print(tool(...))`. Removing those 43 lines turned a 3/3 model into
one that could not see any tool output. This also suggests a cheap change
that makes the agent robust independently of the prompt: echo the value of a
bare last expression back as the observation. The sandbox already has an
`echo_last_expr` option, but the agent loop does not use it.

## 6. Conclusions

**Selected for the final pipeline: `qwen/qwen3.8-27b:free`, with
`gemini-3.6-flash` as the provider fallback.**

**qwen3.8-27b is clearly the best model on every axis we measured:**

- It is the only model that passed all three tasks.
- It used the fewest iterations (4 per task, the minimum possible trace).
- It used the fewest tokens: about 13k input per task, 3× less than the
  next-best model, and 6–10× less than nemotron and north.
- It never produced a step without executable code.
- It had zero retries and no provider failures.
- It submitted with zero extra steps every time.

Its response time (3.0 s average) is mid-range, but because it needs so
few steps its total wall-clock time is also among the lowest. With 4
requests per task it is also the cheapest model to run under OpenRouter's
50-requests/day free limit.

Two caveats about qwen:

1. **Possible prior knowledge.** qwen opened the exact file to patch at
   step 1 on all three tasks, without searching. For django and xarray the
   file path appears in the issue text. For sympy it does not
   (`sympy/solvers/diophantine.py`). SWE-bench Verified is public, so part
   of this efficiency may come from memorized fixes rather than exploration.
2. **Prompt sensitivity.** The ablation shows that qwen depends on the
   worked example to follow the `print(tool(...))` convention. The example
   must stay in the prompt, or the sandbox should echo bare expressions.

**gemini-3.6-flash as fallback.** It passed both tasks that were not cut by
its quota, it explores sensibly, and it submits promptly (1 extra step). Its
format adherence is also good. Most importantly, it uses a **different
provider and quota** from OpenRouter. Because OpenRouter's daily limit is
shared across all models of one account, switching to another OpenRouter
model does not help once that limit is reached. Switching providers does.
Its drawbacks are the slowest responses (7.1 s average) and a small free
quota.

**Disregarded:**

- **north-mini-code (1/3).** It has the highest token use (408k total, 234k
  on xarray alone) despite the fastest responses. On xarray it looped on
  `search_code` 23 times after tests had already passed. On sympy it
  switched to a JSON tool-call format that the agent does not parse (15 of
  20 steps had no executable code), until OpenRouter's daily limit ended the
  run.
- **nemotron-3-ultra (2/3).** It passes, but at about 6× qwen's cost: 240k
  input tokens, 58 iterations and 314 s, the slowest model in total. It
  often returns empty replies (14 of 30 steps on django), and it keeps
  working after the tests pass (2 and 4 extra steps). On django the tests
  passed only at step 30, so it ran out of iterations before it could
  submit.
- **laguna-s-2.1 (2/3, not reliable).** On xarray it used its own
  `<tool_call>…<arg_key>` syntax for all 21 steps, so nothing was executed.
  Its 1-step sympy pass is not credible evidence. In a single code block it
  edited the file before reading it, and it called `final_answer` before it
  could see the `run_tests()` result. The edit was correct, which points to
  a memorized fix rather than real problem solving.

**What the data says about the agent itself:**

- The main failure mode was not reasoning. It was **format adherence**:
  empty replies or tool-call syntaxes we do not parse (laguna, north,
  nemotron).
- Two failed runs had passing tests but never submitted. Submitting
  `get_patch()` automatically when the iteration limit is reached after
  tests have passed would have turned those runs into submissions.
- Free-tier limits shaped the results as much as model quality did (5 of 18
  runs, ablation included, were cut by a 429). Provider fallback is therefore a requirement in
  practice, not an optional extra.
