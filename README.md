*This project has been created as part of the 42 curriculum by lcao, rxue, suna.*

# Agent Smith

## Description

Agent Smith is an agentic framework that tries to solve coding tasks on its
own. It is evaluated on two benchmarks:

- **MBPP**: short Python exercises ("write a function that..."). The answer is
  the source code of the function.
- **SWE-bench**: real GitHub issues from real open-source projects (for
  example sympy). The answer is a `git diff` that fixes the issue.

The agent works in a loop. At each step the LLM writes a short **Thought** and
a block of **Python code**. That code runs inside a restricted **sandbox**, and
whatever it prints is sent back to the LLM as an **Observation**. The LLM
reads it and decides the next step, until it calls `final_answer(...)`.

The LLM's code cannot reach the network, and can only open files inside a few
allowed directories. To work on the task, it goes through **tools** exposed by
an **MCP server** (Model Context Protocol). For SWE-bench, those tools reach
into the task's Docker container with `docker exec`.

## Instructions

### Requirements
- Python 3.10
- [uv](https://docs.astral.sh/uv/)
- Docker or Podman (for SWE-bench)
- An API key for at least one LLM provider

### Setup
```bash
cp .env.example .env
# fill in your real API key(s) in .env
uv sync
```

Several keys for the same provider can be stored in one variable, separated by
commas (`OPENROUTER_API_KEY=key1,key2`). When a request is rate limited (429)
or the server fails (5xx), the provider switches to the next key and retries.

### Choosing an LLM provider

Providers and models are configured in [`models.json`](models.json) at the
repository root. For each provider it lists the base URL, the **name** of the
`.env` variable holding its key(s) (never the key itself), and the models we
use with it:

```json
{
  "default": {"provider": "openrouter", "model": "qwen/qwen3.8-27b:free"},
  "providers": {
    "gemini": {
      "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
      "api_key_env": "GOOGLE_API_KEY",
      "models": ["gemini-3.6-flash"]
    },
    "openrouter": {
      "base_url": "https://openrouter.ai/api/v1",
      "api_key_env": "OPENROUTER_API_KEY",
      "models": ["qwen/qwen3.8-27b:free", "nvidia/nemotron-3-ultra-550b-a55b:free", "..."]
    }
  }
}
```

The default is `qwen/qwen3.8-27b:free` on OpenRouter, the best model in our
benchmark (see [BENCHMARK_REPORT.md](BENCHMARK_REPORT.md)). The `models` lists
are for reference: a model that is not listed is still used as given.

To add a provider with an OpenAI-compatible API, add an entry under
`providers` and put its key(s) in `.env` under the `api_key_env` name; no code
change is needed.

| Option | Meaning | Default |
|---|---|---|
| `--model-name` | Model identifier at that provider | the provider's first model, or `default` |
| `--provider` | Provider name from `models.json` | `default.provider` |
| `--provider-url` | Base URL of the API; overrides `--provider` | (from `models.json`) |
| `--api-key-env` | Name of the variable in `.env` that holds the key | see below |
| `--model-config` | Path of the models config file | `models.json` |

Ways to choose the model:
```bash
                                                           # nothing at all: models.json "default" (qwen on OpenRouter)
--model-name nvidia/nemotron-3-ultra-550b-a55b:free        # another model on the default provider
--provider gemini                                          # another provider, its first listed model
--provider gemini --model-name gemini-3.6-flash            # another provider, a specific model
--model-name "<model>" --provider-url "<url>"              # explicit, works without models.json
```
With `--provider-url`, the key variable is `--api-key-env` if given; otherwise
the `api_key_env` of the provider in `models.json` with the same base URL;
otherwise `OPENROUTER_API_KEY`. The resolution rules live in
`common/model_config.py`.

Every provider is called through its OpenAI-compatible
`<base_url>/chat/completions` endpoint by `common/llm_provider.py`, including
Gemini (`.../v1beta/openai`). The only provider-specific code there is the
parameter that turns reasoning down (`reasoning` for OpenRouter,
`reasoning_effort` for URLs containing `googleapis.com`).

### Running the sandbox interactively
```bash
uv run sandbox                                                   # default config, no tools
uv run sandbox sandbox_template.json                             # custom config
uv run sandbox --mcp-stdio "python mcp_tools_mbpp.py" sandbox_template.json
uv run sandbox --mcp-stdio "python mcp_tools_swebench.py"        # see below
uv run sandbox --mcp-server http://127.0.0.1:8000/mcp            # server already running
```

For `mcp_tools_swebench.py`, the server needs to know which task to serve. If
no container is given, it reads `cache/swebench_task.json`, starts its own
container for that task, and removes it when it exits. Tools must be called
with keyword arguments, for example:
```python
>>> print(search_code(pattern="permute", file_pattern="*.py"))
```

### Running the MBPP agent
```bash
cd moulinette
uv run moulinette_eval dump mbpp --output ../cache/mbpp_task.json
cd ..
uv run python -m agent_mbpp --task-file cache/mbpp_task.json \
    --output cache/mbpp_solution.json
# default model from models.json; add e.g. --provider gemini, or
# --model-name "<model>" --provider-url "<url>" (see "Choosing an LLM provider")
```

### Running the SWE-bench agent
```bash
cd moulinette
uv run moulinette_eval dump swebench --task-id sympy__sympy-14711 --output ../cache/swebench_task.json
cd ..
uv run python -m agent_swebench --task-file cache/swebench_task.json \
    --output cache/swebench_solution.json
# same model options as the MBPP agent
cd moulinette
uv run moulinette_eval validate swebench ../cache/swebench_task.json ../cache/swebench_solution.json
```

Leave off `--task-id` to dump a random instance instead of a fixed one.

`agent_swebench` pulls the task's image, starts a container named
`agent-smith-<instance_id>`, runs the agent, writes the result file, and then
always stops and removes the container, whether the run succeeded or not.

### Running the tests
```bash
uv run --extra dev pytest tests/
```

### Exploring a SWE-bench container by hand

It can help to poke around inside a task's container manually. This is the
same thing the SWE-bench tools do through `docker exec`, just typed by hand.

The image name comes from the `docker_image` field of the dumped task JSON and
is different for each `instance_id`. The example below uses
`sympy__sympy-14711`. With Podman, prefix the image with `docker.io/`.

```bash
# 1. pull the instance's image (first pull can take a few minutes)
docker pull swebench/sweb.eval.x86_64.sympy_1776_sympy-14711:latest

# 2. start it detached; the trailing /bin/bash keeps it alive
docker run -dit --name sympy-14711 \
    swebench/sweb.eval.x86_64.sympy_1776_sympy-14711:latest \
    /bin/bash

# 3. confirm it is up (should show sympy-14711, status Up)
docker ps

# 4. step inside; the prompt changes to something like root@<id>:/#
docker exec -it sympy-14711 /bin/bash

# --- now inside the container ---
cd /testbed
git log -1        # one synthetic "SWE-bench" commit, not the real history
git status
source /opt/miniconda3/bin/activate
conda activate testbed
exit
# --- back on the host ---

# 5. clean up; an unremoved container just sits there
docker stop sympy-14711
docker rm sympy-14711
```

## System Architecture

```
                         your machine (host)
 ┌────────────────────────────────────────────────────────────────────┐
 │                                                                    │
 │  agent_mbpp / agent_swebench  (entry points, __main__.py)          │
 │        │                                                           │
 │        ▼                                                           │
 │  AgentLoop  (common/agent_loop.py)                                 │
 │        │  messages ──► LLMProvider (common/llm_provider.py) ──► LLM API
 │        │  ◄── reply text                                           │
 │        ▼                                                           │
 │  Code extraction  (common/code_extraction.py)                      │
 │        │  Python code string                                       │
 │        ▼                                                           │
 │  Sandbox  (sandbox/executor.py, sandbox/security.py)               │
 │   └─ worker process runs the code, returns what was printed        │
 │        │  tool call (e.g. search_code(...))                        │
 │        ▼                                                           │
 │  MCP client  (sandbox/mcp_client.py)                               │
 │        │  MCP protocol (stdio or HTTP)                             │
 │        ▼                                                           │
 │  MCP server  (mcp_tools_mbpp.py  or  mcp_tools_swebench.py)        │
 │        │                                                           │
 └────────┼───────────────────────────────────────────────────────────┘
          │  docker exec  (SWE-bench only)
          ▼
 ┌──────────────────────────────────────┐
 │  Docker container for the task       │
 │  repository checked out in /testbed  │
 └──────────────────────────────────────┘
```

| Component | File(s) | Role |
|---|---|---|
| Entry points | `agent_mbpp/__main__.py`, `agent_swebench/__main__.py` | Read the task, start the MCP server (and the container for SWE-bench), run the loop, write `SolutionOutput` JSON |
| Agent loop | `common/agent_loop.py` | Thought → Code → Observation cycle, limits, system prompt |
| LLM provider | `common/llm_provider.py` | One API call per step, key rotation and retries |
| Code extraction | `common/code_extraction.py` | Pulls runnable code out of the LLM's reply |
| Sandbox | `sandbox/executor.py`, `sandbox/security.py` | Runs the LLM's code with restrictions |
| Sandbox manual | `sandbox/manual.py` | Turns the server's tool list into text for the system prompt |
| MCP client | `sandbox/mcp_client.py` | Connects to any MCP server, lists its tools, calls them |
| MCP servers | `mcp_tools_mbpp.py`, `mcp_tools_swebench.py`, `swebench_tools/` | The tools themselves |
| Container lifecycle | `common/docker_env.py` | Pull image, start container, clean up |
| Data models | `common/models.py` | Pydantic models whose fields are fixed by the subject |

The MCP server runs **on the host**, not inside the container. Only the
commands the tools send (`grep`, `git diff`, the test script, ...) run inside
the container. This keeps the official SWE-bench images unchanged: nothing
has to be installed in them.

## Agent Loop Explanation

`AgentLoop.run()` starts with two messages: the system prompt and the task.
Then, for each step:

1. **Ask the LLM.** The whole message list is sent to the provider, with
   `<end_code>` as a stop sequence so the model stops right after its code.
2. **Extract the code.** `extract_python_code_block()` reads the reply. The
   main format is a ```` ```python ```` block, but XML tool calls, JSON/Hermes
   tool calls and ReAct `Action:` lines are also converted into Python calls.
   If nothing is found, the observation tells the LLM which format to use.
3. **Run it.** The sandbox executes the code. The observation is everything
   the code printed. If nothing was printed, the LLM is reminded to use
   `print()`.
4. **Check for an answer.** If the code called `final_answer(...)`, the loop
   ends with `success=True`.
5. **Feed back.** The LLM's reply and `Observation:\n<output>` are appended to
   the messages, and the next step begins.

**Limits.** Each entry point sets its own limits:

| Limit | MBPP | SWE-bench |
|---|---|---|
| Max iterations | 10 | 30 |
| Max input tokens (cumulative) | 6,000 | 300,000 |
| Max output tokens (cumulative) | 1,500 | 10,000 |
| Timeout | 120 s | 900 s |

Token limits are summed over all steps. The timeout is checked before each new
LLM call. When a limit is reached, the run stops with `success=False` and an
`error` explaining which limit was hit. Every step is recorded as a
`StepMetrics` entry (tokens, time, model, raw LLM output, sandbox input and
output, retries).

**Reasoning tokens.** Reasoning tokens count toward the output limit, so the
provider asks models to think as little as the API allows
(`reasoning.enabled=false` on OpenRouter, `thinkingLevel: "low"` on Gemini),
and Gemini's thinking tokens are added to the reported output count.

**System prompt.** `build_system_prompt()` combines:
- the response format (Thought, code block, `<end_code>`) and general rules
  (only printed output is visible, variables persist, `exec`/`eval` are not
  available, tools take keyword arguments);
- the **sandbox manual**, generated at runtime from the tools the MCP server
  reports, so a different server automatically produces a different manual;
- benchmark-specific instructions with one worked example. For SWE-bench, the
  prompt stresses that the repository is only reachable through the tools,
  and that the final answer must be exactly what `get_patch()` returned.

## Sandbox Design

The sandbox runs code written by the LLM, so it treats that code as untrusted.

**Process isolation.** Code runs in a separate, long-lived **worker process**
(`multiprocessing`, fork). The parent keeps the agent running even if the
worker crashes, segfaults or is killed for using too much memory: the failure
simply becomes an error observation. Variables defined in one step stay
available in the next, because the worker keeps its own namespace between
calls. If a worker has to be replaced, that namespace is lost.

**The four guards** (`sandbox/security.py`, tested in
`tests/test_sandbox_security.py`):

| Guard | How it works |
|---|---|
| Import allowlist | `ImportGuard` replaces `__import__` in the sandbox's builtins. Only modules listed in `authorized_imports` (exact names or `package.*`) can be imported. Network modules such as `socket` are therefore blocked. |
| Path allowlist | `open` is replaced by a version that resolves the path with `os.path.realpath` first (so `..` and symlinks cannot escape) and only allows paths inside `allowed_directories`. |
| Timeout | The parent waits for the result and **kills the worker** after `max_execution_time_seconds`. Unlike `signal.alarm`, this also stops code stuck inside C code. |
| Memory limit | Two layers: `RLIMIT_AS` is lowered only around the `exec()` call (reliable on Linux, loosely enforced on macOS), and the parent checks the worker's real memory with `ps` and kills it if it goes over `max_memory_mb` (works on both systems). |

In addition, `eval`, `exec`, `compile`, `input` and `breakpoint` are removed
from the sandbox's builtins, so the LLM's code cannot use them to get around
the guards.

**Default configuration** (`common/models.py`, also in
`sandbox_template.json`): a small set of standard modules (`math`,
`collections`, `itertools`, `re`, `json`, ...), directories `/testbed` and
`/tmp/agent`, 30 s and 512 MB per execution.

**Tools and the worker.** MCP tools are not called inside the worker. The MCP
connection lives in a background thread of the parent, and `fork` does not
copy threads. So the worker gets small proxy functions with the same names: a
proxy sends `(tool_name, kwargs)` to the parent through a `Pipe`, the parent
makes the real MCP call, and sends the result back. As a result, tools run
outside the sandbox's restrictions, which is intended: they are trusted code.

**`final_answer`** is built into the sandbox, not an MCP tool. Calling it
records the answer and tells the loop to stop.

## Tool Implementation Details

### Transports

Both MCP servers use `FastMCP` and support two transports:

- **stdio** (default): the client starts the server as a subprocess, for
  example `--mcp-stdio "python mcp_tools_swebench.py"`. This is what the
  agents use.
- **HTTP** (streamable HTTP): start the server yourself and connect by URL.
  ```bash
  python mcp_tools_swebench.py --http --port 8000 --task-file cache/swebench_task.json
  uv run sandbox --mcp-server http://127.0.0.1:8000/mcp
  ```

The MCP client is generic: it asks the server for its tool list
(`ListToolsRequest`) and wraps each tool as a normal Python function, so it
works with any MCP server, not only ours. Because the MCP SDK is async and the
sandbox is not, the client runs an event loop in a background thread and
blocks until each call returns.

### MBPP: `run_tests(code)`

`mcp_tools_mbpp.py` exposes a single tool. It builds a script from the task's
`test_imports`, the candidate code and a small harness that runs each test
separately, then runs it in a **separate Python process** with a 10 s timeout,
so broken or malicious code cannot affect the server. It returns one
PASS/FAIL/ERROR line per test and a summary such as
`[run_tests] 3/3 tests passed`.

### SWE-bench: the 9 tools

`mcp_tools_swebench.py` only registers the tools. The code is split by theme:

| File | Tools |
|---|---|
| `swebench_tools/fs_tools.py` | `read_file`, `edit_file`, `list_files` |
| `swebench_tools/search_tools.py` | `search_code`, `search_function_or_class_definition_in_code`, `find_references` |
| `swebench_tools/exec_tools.py` | `run_tests`, `get_patch`, `run_command` |
| `swebench_tools/docker_bridge.py` | shared helpers used by all tools |

**Shared rules** (`docker_bridge.py`):

- **Tools never raise.** Each tool is wrapped in `never_raise()`. Any
  exception comes back to the LLM as `"[error] <type>: <message>"` instead of
  crashing the server.
- **Everything goes through `docker_exec()`**, which runs
  `docker exec -w /testbed <container> bash -lc <command>`. It adds `-i` only
  when input is sent on stdin; without `-i`, piped input never reaches the
  container.
- **Paths:** relative paths are resolved against `/testbed`, and tools
  output absolute paths, so the LLM can pass a path from one tool's output
  straight into another.
- **Output size:** long output is cut to 20,000 characters (the first
  10,000 and the last 5,000 are kept, since errors are usually at the end).
  `get_patch()` is the only exception.
- **Noise:** `clean_stderr()` removes the container runtime's own messages
  (such as Podman's `Emulate Docker CLI...` line) from error output.
- **Safe arguments:** values supplied by the LLM (paths, patterns, names) are
  quoted with `shlex.quote()` before being put into a shell command.

**File system tools**

| Tool | What it does |
|---|---|
| `read_file(filepath, start_line, end_line)` | Returns the requested lines as `<line_number>: <content>`. Gives a clear error if the range is invalid or past the end of the file. |
| `edit_file(filepath, old_str, new_str)` | Replaces `old_str` with `new_str`. `old_str` must appear **exactly once**; otherwise nothing changes and the error says whether it was missing or found several times. Returns the edited lines with some context, and adds a warning if a `.py` file no longer compiles (the edit is still applied, so the LLM has to fix it). |
| `list_files(directory, pattern)` | Lists files matching a glob such as `*.py`, recursively, skipping `.git`. One absolute path per line, sorted. |

**Search tools** (all return `/testbed/path/to/file.py:<line> <content>`)

| Tool | What it does |
|---|---|
| `search_code(pattern, file_pattern)` | Text search with `grep -rnI --include=<glob>`. Shows at most 100 matches and asks the LLM to narrow the search if there are more. Exit code 1 from grep means "no match", not an error. |
| `search_function_or_class_definition_in_code(name)` | Finds where a function or class is **defined**. A short script is sent to the container's Python on stdin; it parses every `.py` file with the `ast` module and reports only real `def`/`async def`/`class` nodes with that name. Unlike a grep for `def name`, this ignores calls, comments, strings and similar names, and also finds methods inside classes. |
| `find_references(name, filepath, line)` | Finds where a name is **used**, with a whole-word grep (`-w`), and leaves out the definition line given by `filepath`/`line`. Known limitation: being text-based, it also lists comments and strings that mention the name. |

**Execution tools**

| Tool | What it does |
|---|---|
| `run_tests()` | Runs the task's `eval_script`. The script is base64-encoded on the host and decoded in the container (it contains quotes, heredocs and patch text that would not survive being pasted into a command), written to `/tmp` (a file in `/testbed` would end up in the diff), and run with a 300 s timeout. Returns a summary instead of the full log: passed/failed/error counts, names of failing tests, and the last 25 lines. Understands both pytest output and sympy's own `bin/test` output. |
| `get_patch()` | Returns `git -c core.fileMode=false diff`. This is the agent's answer, so it is **never truncated** (a cut diff cannot be applied). `core.fileMode=false` leaves permission-only changes out. |
| `run_command(command, workdir)` | Runs any shell command in the container. Returns `exit_code`, `stdout` and `stderr` separately, since a failing command can still print useful information. |

**Example** (sympy__sympy-18189, before any fix):
```text
>>> print(run_tests())
[run_tests] 42 passed, 1 failed, 0 errors
Failing tests:
  sympy/solvers/tests/test_diophantine.py:test_diophantine
...
```

**Which container?** The server finds its container and task in this order:
`--container`/`--task-file` flags, then the `SWEBENCH_CONTAINER_NAME` /
`SWEBENCH_TASK_FILE` environment variables (set by `agent_swebench`), then
`cache/swebench_task.json`. When it is not given a container, it starts its
own and removes it on exit, including when its parent process disappears.

**Images.** SWE-bench images are built for x86_64, so containers are started
with `--platform linux/amd64` (slower on Apple Silicon, but closer to the
grading machine). Image names without a registry get a `docker.io/` prefix so
that Podman can find them.

## Benchmark Results and Analysis

See [BENCHMARK_REPORT.md](./BENCHMARK_REPORT.md) for the full comparison.

## Resources

- Model Context Protocol documentation: https://modelcontextprotocol.io/
- MCP Python SDK: https://github.com/modelcontextprotocol/python-sdk
- SWE-bench: https://www.swebench.com/
- MBPP dataset: https://github.com/google-research/google-research/tree/master/mbpp
- Python `ast` module: https://docs.python.org/3/library/ast.html
- Python `multiprocessing` and `resource` modules: https://docs.python.org/3/library/multiprocessing.html, https://docs.python.org/3/library/resource.html
- Gemini API reference: https://ai.google.dev/api
- OpenRouter API documentation: https://openrouter.ai/docs
- smolagents (CodeAgent idea, for inspiration only, not used as a dependency): https://github.com/huggingface/smolagents

### How AI was used

AI assistants were used as helpers; all code was reviewed, tested and
understood by the team before being kept.

- **Explaining concepts:** MCP, Docker, the `ast` module, `multiprocessing`,
  shell quoting, and the existing code, explained step by step.
- **SWE-bench tools:** design discussions (AST instead of grep for
  definitions, base64 for the eval script, writing it to `/tmp`), debugging
  (the missing `-i` flag in `docker exec`), and translating code comments to
  English. Every tool was then tested by hand in the sandbox on a real task.
- **LLM provider:** help adding native Gemini API support to
  `common/llm_provider.py`, tested with real API calls.
- **Documentation:** first draft of this README, written from the code and
  then reviewed by the team.
