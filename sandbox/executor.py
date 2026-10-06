"""
Sandbox executor (Section V.2 points 1, 2).

Responsibilities:
  1. Keep one namespace dict that lives across steps, pre-filled with
     - MCP tool wrappers (discovered from mcp_client.py)
     - final_answer()  (built into the sandbox, NOT an MCP tool)
  2. execute(code) -> str : run code, capture stdout (the LLM only sees what it print()s)
  3. run_repl() : interactive mode for `uv run sandbox`

  Re: point 2 -- "only sees what it print()s" is now literally true for the
  agent path, and `run_repl()` is the one deliberate exception. See
  _compile() for why that distinction has to be made explicitly.

STAGE 4 (current): code now runs in a persistent CHILD PROCESS instead of
being exec()'d directly inside the agent's own process. Why this changed:

  - Real timeout. The previous approach used `signal.alarm`, which only
    interrupts Python BETWEEN bytecode instructions. Code stuck in a tight
    C-extension loop (some numpy/requests internals, a C-level infinite
    loop) never yields back to the Python interpreter, so alarm never
    fires and the "timeout" never actually stops it. A subprocess can be
    SIGKILLed unconditionally from outside -- no cooperation from the
    stuck code required, so this is a real guarantee instead of a
    best-effort one.
  - Real isolation. A segfault, a C-level crash, or the OS OOM-killing the
    process now only takes down the *worker*, not the whole agent_mbpp /
    agent_swebench run. Before, a crash inside exec() could kill the
    entire agent process (or worse, corrupt its state without killing it).
  - Memory limit is now TWO layers instead of one:
      1. security.apply_memory_limit()/reset_memory_limit() (RLIMIT_AS),
         applied ONLY around the exec(code, namespace) call inside the
         worker's loop -- not for the worker's whole lifetime. Reliable on
         Linux, loosely enforced by the macOS kernel. IMPORTANT: this used
         to be applied once at worker startup and left on permanently,
         which caused a real bug on Linux (never showed up on macOS, since
         macOS doesn't enforce RLIMIT_AS anyway): a forked, fully-loaded
         CPython worker can already be using a good chunk of a tight limit
         (e.g. 128MB in tests) just from its own imports, so when
         result_queue.put() lazily starts its internal feeder thread after
         exec() finishes, the mmap for that thread's stack could fail with
         "RuntimeError: can't start new thread" -- nothing was actually
         leaking, the cap meant for the user's code was just still active
         for the worker's own plumbing. Now the cap is applied right before
         exec() and lifted right after, so it never constrains anything
         except the code it's meant to constrain.
      2. An RSS watchdog in the PARENT: execute()'s polling loop (already
         running every _POLL_INTERVAL_SECONDS to check for a result/crash)
         also shells out to `ps -o rss= -p <pid>` to read the worker's
         actual resident memory and kills it if it crosses
         config.max_memory_mb. `ps` works the same way on Linux and macOS,
         so this layer is the one that makes the limit actually hold on
         both platforms, independent of how the kernel treats RLIMIT_AS.

Design:
  - One worker process per Sandbox instance, started in __init__ and kept
    alive across calls (`multiprocessing`, fork start method).
  - Two queues: code goes parent -> child (`_code_queue`), results come
    child -> parent (`_result_queue`). A `_code_queue` item is either the
    `None` shutdown sentinel or a `(code, echo_last_expr)` tuple -- the
    worker has to test for the sentinel BEFORE it unpacks.
  - The worker keeps its OWN `namespace` dict alive across calls (it lives
    in the child's memory, not the parent's), so a variable/function
    defined in step N is still there in step N+1 -- same externally
    visible behavior as the old in-process version.
  - If a call times out, or the worker dies (crash/OOM-kill), that worker
    is replaced with a fresh one. The namespace from that in-flight call
    is lost -- but the step failed anyway, so from the LLM's point of view
    this is no different from any other failed step: it sees an error
    observation and tries again.

MCP tools and the worker process:
  MCP tools must NOT be called directly inside the worker. The MCP
  connection lives in a background thread of the PARENT process, and fork
  only copies the calling thread -- so in the child, a tool call would wait
  forever for a thread that doesn't exist (the call hangs until timeout).

  Instead, the worker gets small proxy functions with the same names. A
  proxy sends (tool_name, kwargs) to the parent over a Pipe and waits for
  the reply; the parent, while polling for the result in execute(), runs
  the real MCP call and sends the result back. So the connection is only
  ever used by the process that owns it, and MCP tools run outside the
  sandbox, as Section V.2 describes.

  A Pipe (not a Queue) is used on purpose: Connection.send() writes
  synchronously and never starts a background feeder thread, so a tool
  call made while the memory cap is active can't fail with
  "can't start new thread" (the Queue problem described above).

  Timeout independence: a tool call's own duration must not count
  against the sandbox's max_execution_time_seconds (Section V.2: MCP
  actions "are not subject to the sandbox timeout"). execute()'s
  deadline is pushed forward by however long _serve_tool_requests()
  just spent actually running a tool, so waiting on e.g. SWE-bench's
  run_tests (which can legitimately take far longer than a quick
  sandbox computation) doesn't eat into the code-execution budget.
  The tool call itself is still bounded -- by its own timeout (e.g.
  docker_exec's), not by this one.

Import restriction:
  The ImportGuard is installed only in the sandbox namespace's own
  __builtins__ (see _worker_main), NOT globally with install(). A global
  install would also apply to trusted code running in the worker, such as
  the tool proxies.
"""
from __future__ import annotations

import ast
import atexit
import io
import multiprocessing as mp
import os
import sys
import tempfile
import queue as queue_module
import time
import traceback
import weakref
import codeop
from multiprocessing.connection import Connection
from typing import Any, Callable, Dict, List, Optional

from common.models import SandboxConfig
from sandbox.security import (
    ImportGuard,
    apply_memory_limit,
    block_network,
    build_restricted_builtins,
    reset_memory_limit,
)

# How often the parent polls for a result while waiting on a call. Small
# enough that a dead worker (crash/OOM) is noticed well before the full
# timeout elapses, instead of always waiting out the whole budget.
_POLL_INTERVAL_SECONDS = 0.1

# The RSS check (layer 2 of the memory limit, see module docstring) shells
# out to `ps` -- cheap, but no need to do it on every single 0.1s poll tick.
# Checking every 2nd tick still catches a runaway allocation within ~0.2s.
_RSS_CHECK_EVERY_N_TICKS = 2

# Output printed before a timeout / memory kill / crash. The worker writes its
# stdout through to a file (up to a cap), so the parent can still read what was
# printed after the worker has been killed.
_OUTPUT_FILE_CAP_BYTES = 1_000_000
_PARTIAL_OUTPUT_MAX_CHARS = 2000

# Cap on the output of a normal (finished) call. Long output keeps its start
# and its end, so the LLM still sees how it began and how it ended.
_RESULT_OUTPUT_HEAD_CHARS = 8000
_RESULT_OUTPUT_TAIL_CHARS = 4000
_RESULT_OUTPUT_MAX_CHARS = _RESULT_OUTPUT_HEAD_CHARS + _RESULT_OUTPUT_TAIL_CHARS

# Every live Sandbox, so emergency_cleanup() can reach their workers and files.
_LIVE_SANDBOXES = weakref.WeakSet()


def _cap_output(text: str) -> str:
    if len(text) <= _RESULT_OUTPUT_MAX_CHARS:
        return text
    omitted = len(text) - _RESULT_OUTPUT_HEAD_CHARS - _RESULT_OUTPUT_TAIL_CHARS
    return (
        text[:_RESULT_OUTPUT_HEAD_CHARS]
        + f"\n[... {omitted} characters omitted ...]\n"
        + text[-_RESULT_OUTPUT_TAIL_CHARS:]
    )


class _TeeStream(io.TextIOBase):
    """stdout replacement inside the worker. Everything goes to the in-memory
    buffer (the normal result) and, up to a cap, is also flushed to a file."""

    def __init__(self, buffer, out_file, cap_bytes):
        self._buffer = buffer
        self._file = out_file
        self._cap = cap_bytes
        self._written = 0

    def writable(self):
        return True

    def write(self, s):
        self._buffer.write(s)
        if self._file is not None and self._written < self._cap:
            try:
                self._file.write(s)
                self._file.flush()
                self._written += len(s)
            except (OSError, ValueError):
                self._file = None
        return len(s)

    def flush(self):
        pass


def _remove_file(path):
    try:
        os.remove(path)
    except OSError:
        pass


def emergency_cleanup() -> None:
    """Last-resort cleanup for paths that end with os._exit (the watchdog).

    os._exit skips atexit handlers and multiprocessing's own exit hook, so
    sandbox workers would be left running and their temp files left behind.
    Never raises.
    """
    for sb in list(_LIVE_SANDBOXES):
        try:
            if sb._worker.is_alive():
                sb._worker.kill()
        except Exception:
            pass
        try:
            _remove_file(sb._output_path)
        except Exception:
            pass


def _open_output_file(path):
    if not path:
        return None
    try:
        return open(path, "w", encoding="utf-8")
    except OSError:
        return None


def _get_worker_rss_mb(pid: int) -> Optional[float]:
    """Resident memory of the worker process, in MB, via `ps` (works the
    same way on Linux and macOS, unlike RLIMIT_AS -- see module docstring).
    Returns None if the process is already gone or `ps` itself fails.
    """
    import subprocess

    try:
        proc = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = proc.stdout.strip()
    if not output:
        return None  # pid not found -- process already exited
    try:
        return int(output) / 1024  # ps reports RSS in KB
    except ValueError:
        return None


def _compile(code: str, echo_last_expr: bool):
    """Compile `code` for the worker, in the mode the CALLER asked for.

    This used to always try "single" first and only fall back to "exec",
    which quietly made the observation depend on the *shape* of the code
    rather than on what it printed. "single" accepts exactly one TOP-LEVEL
    statement, and for every expression statement it compiles at module
    scope it emits PRINT_EXPR -- i.e. pushes that value through
    sys.displayhook, which writes to sys.stdout, which is the buffer we
    capture and hand back to the LLM. So:

        sorted(x)                    -> 1 top-level stmt -> "single" -> echoed
        x = [3,1,2]                  -> 2 top-level stmts -> "exec"  -> silent
        sorted(x)

        for q in queries:            -> 1 top-level stmt -> "single" -> EVERY
            search(q)                   iteration's return value echoed

    That last one is the one that actually bites: an LLM writing a plain
    loop over tool calls, with no print() anywhere, dumps N reprs into the
    observation and burns context for it -- while the same logic split
    across two statements stays silent. Nothing leaks to the real terminal
    (redirect_stdout catches displayhook fine); the damage is entirely in
    what the LLM ends up reading.

    So the mode is now the caller's decision:
      - echo_last_expr=False (agent path, the default): "exec" only.
        Expression values are discarded, and the docstring's promise that
        the LLM only sees what it print()s actually holds.
      - echo_last_expr=True (run_repl only): the old behavior, which for an
        interactive prompt is not a bug but fidelity -- CPython's own REPL
        echoes exactly this way, nested statements included.

    The "exec" fallback stays on the echo path: pasting several statements
    into the REPL at once gets past run_repl's codeop check (it raises
    SyntaxError there, which run_repl treats as "complete, let the worker
    report it"), so "single" can still legitimately fail here.
    """
    if not echo_last_expr:
        return compile(code, "<sandbox>", "exec")
    try:
        return compile(code, "<sandbox>", "single")
    except SyntaxError:
        pass  # more than one top-level statement -- not REPL-shaped
    # Deliberately outside the except block: by the time this runs the
    # handler has exited, so a genuine syntax error raised here doesn't
    # carry the (misleading) "single" error as its __context__ and
    # traceback.format_exc() below shows just the real one.
    return compile(code, "<sandbox>", "exec")


def _worker_main(
    code_queue: "mp.Queue",
    result_queue: "mp.Queue",
    config: SandboxConfig,
    tool_names: List[str],
    tool_conn: "Connection",
    output_path: Optional[str] = None,
) -> None:
    """Entry point of the child process. Runs until it receives `None`
    (shutdown sentinel) or is killed by the parent. Everything in here
    happens in the child's own memory -- it never touches parent state.
    """
    import contextlib
    import io
    import signal

    from sandbox.code_guard import check_code, harden_builtins, scrub_environment

    signal.signal(signal.SIGINT, signal.SIG_IGN)
    scrub_environment()

    # NOTE: the memory cap is intentionally NOT applied here. It's scoped
    # to just the exec() call below instead -- see the module docstring's
    # "Memory limit" section for why applying it for the worker's whole
    # lifetime caused a real bug on Linux.
    namespace: Dict[str, Any] = {}
    final_answer_state: Dict[str, Any] = {"called": False, "value": None}

    def _final_answer(answer: str) -> None:
        """Built into the sandbox, not an MCP tool (Section V.2)."""
        final_answer_state["value"] = str(answer)
        final_answer_state["called"] = True

    def _make_tool_proxy(tool_name: str) -> Callable:
        """A stand-in for an MCP tool: forwards the call to the parent
        process (which owns the MCP connection) and returns its reply."""

        def proxy(**kwargs):
            tool_conn.send((tool_name, kwargs))
            status, value = tool_conn.recv()
            if status == "error":
                raise RuntimeError(value)
            return value

        proxy.__name__ = tool_name
        return proxy

    namespace["final_answer"] = _final_answer
    for tool_name in tool_names:
        namespace[tool_name] = _make_tool_proxy(tool_name)

    block_network()
    import_guard = ImportGuard(config.authorized_imports)
    restricted_builtins = build_restricted_builtins(config.allowed_directories)
    restricted_builtins["__import__"] = import_guard.guarded_import
    harden_builtins(restricted_builtins)
    namespace["__builtins__"] = restricted_builtins

    while True:
        try:
            request = code_queue.get()
        except (KeyboardInterrupt, EOFError):
            break
        if request is None:  # shutdown sentinel -- test BEFORE unpacking
            break
        code, echo_last_expr = request

        stdout_buffer = io.StringIO()
        out_file = _open_output_file(output_path)
        tee = _TeeStream(stdout_buffer, out_file, _OUTPUT_FILE_CAP_BYTES)
        error: Optional[str] = None
        apply_memory_limit(config.max_memory_mb)
        try:
            check_code(code)
            compiled = _compile(code, echo_last_expr)
            with contextlib.redirect_stdout(tee):
                exec(compiled, namespace)
        except (KeyboardInterrupt, SystemExit):
            raise
        except MemoryError:
            error = f"[MEMORY LIMIT] Execution exceeded {config.max_memory_mb}MB and was stopped"
        except Exception:
            # Keep the partial output, then show the error so the LLM can fix its code.
            error = traceback.format_exc(limit=-1)
        finally:
            # Lift the memory cap before touching result_queue.put() below,
            # which may need to start its own background thread the first
            # time it's called -- that shouldn't be squeezed by a limit
            # meant only for the code that just ran.
            reset_memory_limit()
            if out_file is not None:
                out_file.close()

        result_queue.put(
            {
                "output": stdout_buffer.getvalue(),
                "error": error,
                "final_answer_called": final_answer_state["called"],
                "final_answer_value": final_answer_state["value"],
            }
        )
        final_answer_state["called"] = False  # reported; reset for the next call


class Sandbox:
    def __init__(self, config: SandboxConfig, mcp_tools: Optional[Dict[str, Callable]] = None):
        self.config = config
        self.mcp_tools = mcp_tools or {}
        self.final_answer_value: Optional[str] = None
        self.final_answer_called: bool = False
        # fork (not spawn): the child inherits everything already imported
        # in the parent for free and doesn't need `mcp_tools` to be
        # picklable. See the fork caveat in the module docstring re: what
        # mcp_tools is (and isn't) safe to close over.
        self._ctx = mp.get_context("fork")
        self._code_queue: "mp.Queue" = self._ctx.Queue()
        self._result_queue: "mp.Queue" = self._ctx.Queue()
        # File the worker writes its stdout through to; read back if the worker
        # has to be killed (timeout, memory limit, crash).
        fd, self._output_path = tempfile.mkstemp(prefix="sandbox_out_", suffix=".txt")
        os.close(fd)
        atexit.register(_remove_file, self._output_path)
        # Pipe for MCP tool calls: the worker sends requests, we answer them.
        self._tool_conn, self._worker_tool_conn = self._ctx.Pipe()
        self._worker: mp.process.BaseProcess = self._spawn_worker()
        _LIVE_SANDBOXES.add(self)

    def _spawn_worker(self) -> mp.process.BaseProcess:
        worker = self._ctx.Process(
            target=_worker_main,
            args=(
                self._code_queue,
                self._result_queue,
                self.config,
                list(self.mcp_tools),
                self._worker_tool_conn,
                self._output_path,
            ),
            daemon=True,
        )
        worker.start()
        return worker

    def _restart_worker(self) -> None:
        if self._worker.is_alive():
            self._worker.kill()  # SIGKILL -- no cooperation from the child needed
            self._worker.join()
        # Old queues may hold a stale/half-written result from the dead
        # worker; start clean so the next call can't accidentally read it.
        self._code_queue = self._ctx.Queue()
        self._result_queue = self._ctx.Queue()
        self._tool_conn, self._worker_tool_conn = self._ctx.Pipe()
        self._worker = self._spawn_worker()

    def _read_partial_output(self) -> str:
        """What the worker printed before it was killed. Call it after the
        worker is dead, so the file is final. Long output keeps its start and end."""
        try:
            with open(self._output_path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            return ""
        limit = _PARTIAL_OUTPUT_MAX_CHARS
        if len(text) > limit:
            half = limit // 2
            omitted = len(text) - limit
            text = text[:half] + f"\n[... {omitted} characters omitted ...]\n" + text[-half:]
        return text

    def _with_partial(self, message: str) -> str:
        """Put the output printed before the stop in front of the stop message."""
        partial = self._read_partial_output()
        if not partial:
            return message
        if not partial.endswith("\n"):
            partial += "\n"
        return f"{partial}{message}\n(The output above was printed before the execution was stopped.)"

    def _serve_tool_requests(self) -> float:
        """Answer any MCP tool calls the worker is waiting on, using the
        real tool functions (and MCP connection) owned by this process.

        Returns how many seconds were actually spent running a real
        tool -- the caller (execute()) adds this back onto its own
        deadline, since MCP tool time must NOT count against the
        sandbox's own execution budget (see the module docstring's
        "Timeout independence" section). Without this, a
        slow-but-legitimate tool call would be mistaken for the
        sandboxed CODE running too long, and the worker would be
        killed and reported as a "[TIMEOUT]" even though nothing was
        actually stuck."""
        spent = 0.0
        while self._tool_conn.poll():
            try:
                tool_name, kwargs = self._tool_conn.recv()
            except (EOFError, OSError):
                return spent  # worker died mid-request; execute() will notice
            start = time.monotonic()
            try:
                reply = ("ok", self.mcp_tools[tool_name](**kwargs))
            except Exception as e:
                reply = ("error", f"Tool '{tool_name}' failed: {type(e).__name__}: {e}")
            spent += time.monotonic() - start
            try:
                self._tool_conn.send(reply)
            except (BrokenPipeError, OSError):
                return spent
        return spent

    def execute(self, code: str, echo_last_expr: bool = False) -> str:
        """Run `code` in the persistent worker and return what it printed
        (or the error/timeout/crash message).

        `echo_last_expr` defaults to False on purpose: the agent path
        (common/agent_loop.py) calls this with just the code, so the safe,
        deterministic behavior is what it gets without having to ask. Only
        run_repl() opts in. See _compile() for what it changes.

        Note it rides along with each call rather than being worker state --
        that's what keeps _restart_worker() free of anything to re-init.
        """
        # Start from an empty file, so a kill below cannot return output
        # left over from an earlier call.
        try:
            open(self._output_path, "w").close()
        except OSError:
            pass
        self._code_queue.put((code, echo_last_expr))

        deadline = time.monotonic() + self.config.max_execution_time_seconds
        result: Optional[dict] = None
        over_memory_limit = False
        tick = 0
        while True:
            # Tool time doesn't count against the sandbox's own
            # deadline -- push it forward by however long that tool
            # call just took (see _serve_tool_requests()'s docstring).
            deadline += self._serve_tool_requests()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                result = self._result_queue.get(timeout=min(remaining, _POLL_INTERVAL_SECONDS))
                break
            except queue_module.Empty:
                if not self._worker.is_alive():
                    break  # crashed mid-call -- don't wait out the full timeout
                tick += 1
                if tick % _RSS_CHECK_EVERY_N_TICKS == 0:
                    rss_mb = _get_worker_rss_mb(self._worker.pid)
                    if rss_mb is not None and rss_mb > self.config.max_memory_mb:
                        over_memory_limit = True
                        break  # don't wait for the timeout -- kill it now

        if over_memory_limit:
            self._restart_worker()
            return self._with_partial(
                f"[MEMORY LIMIT] Execution exceeded {self.config.max_memory_mb}MB and was stopped"
            )

        if result is None:
            if self._worker.is_alive():
                self._restart_worker()
                return self._with_partial(
                    f"[TIMEOUT] Execution exceeded {self.config.max_execution_time_seconds}s and was terminated"
                )
            else:
                self._restart_worker()
                return self._with_partial(
                    "[CRASHED] Worker process died while running this code (likely OOM or a segfault)"
                )

        if result["final_answer_called"]:
            self.final_answer_value = result["final_answer_value"]
            self.final_answer_called = True

        output = _cap_output(result["output"])
        if result["error"]:
            return output + result["error"]
        return output

    def run_repl(self) -> None:
        """Read code, execute it, print result. `exit` or EOF quits.

        Piped input (`cat file.py | uv run sandbox`) is a whole file, not
        typed lines: it goes to _run_piped() instead of the line loop.
        """
        if not sys.stdin.isatty():
            self._run_piped(sys.stdin.read())
            return
        print("Agent Smith sandbox. Type 'exit' to quit.")
        buffer : List[str] = []
        while True:
            try:
                line = input("... " if buffer else ">>> ")
            except EOFError:
                print()
                break
            except KeyboardInterrupt:
                print("\nKeyboardInterrupt")
                buffer.clear()
                continue
            if not buffer:
                if line.strip() == "exit":
                    break
                if not line.strip():
                    continue
            buffer.append(line)
            source = "\n".join(buffer)
            try:
                if codeop.compile_command(source, "<sandbox>","single") is None:
                    continue
            except SyntaxError:
                pass
            buffer.clear()

            try:
                # The one caller that wants expression values echoed back --
                # that's what an interactive prompt is for.
                output = self.execute(source, echo_last_expr=True)
            except KeyboardInterrupt:
                print("\nKeyboardInterrupt")
                continue
            if output:
                print(output, end = "" if output.endswith("\n") else "\n")
            if self.final_answer_called:
                print(f"[final_answer] {self.final_answer_value!r}")
                self.final_answer_called = False

    def _run_piped(self, source: str) -> None:
        """Run a whole piped file one top-level statement at a time.

        The line loop in run_repl() follows interactive rules, which break
        on a file: a blank line inside a block ends that block (the rest of
        it becomes "unexpected indent"), and a block still open at EOF is
        never run. Splitting with ast uses the file's real structure instead.

        One statement per execute() call, like the REPL: if one statement
        times out (and the worker is restarted), the ones after it still run.
        """
        lines = source.splitlines()
        try:
            tree = ast.parse(source)
        except SyntaxError:
            chunks = [source]  # run it anyway, so the worker reports the error
        else:
            chunks = []
            for node in tree.body:
                # A decorated def starts at its first decorator, not at `def`.
                decorators = getattr(node, "decorator_list", [])
                start = min([node.lineno] + [d.lineno for d in decorators])
                chunks.append("\n".join(lines[start - 1:node.end_lineno]))

        for chunk in chunks:
            output = self.execute(chunk, echo_last_expr=True)
            if output:
                print(output, end="" if output.endswith("\n") else "\n")
            if self.final_answer_called:
                print(f"[final_answer] {self.final_answer_value!r}")
                self.final_answer_called = False

    def shutdown(self) -> None:
        """Call when done with this Sandbox so the worker process doesn't
        leak. `daemon=True` means it would get cleaned up at interpreter
        exit either way, but don't rely on that for a long-running agent
        loop that creates many Sandboxes (one per task)."""
        if self._worker.is_alive():
            try:
                self._code_queue.put(None)
                self._worker.join(timeout=2)
            except Exception:
                pass
            if self._worker.is_alive():
                self._worker.kill()
                self._worker.join()
        try:
            os.remove(self._output_path)
        except (OSError, AttributeError):
            pass

    def __del__(self) -> None:
        try:
            self.shutdown()
        except Exception:
            pass
