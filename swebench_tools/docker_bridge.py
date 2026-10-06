"""
Shared infrastructure for the SWE-bench MCP tools (swebench_tools/*).

Every tool in fs_tools.py / search_tools.py / exec_tools.py needs two
things: which container to `docker exec` into, and which task it's serving
(eval_script, repo, instance_id, ...). This module resolves both and
provides docker_exec(), the single primitive every tool is built on top of,
plus the shared conventions the whole swebench_tools package follows:

  - Tools never raise. mcp_tools_swebench.py wraps every registered tool in
    never_raise() below, so any exception (a bug, a misconfigured
    container/task from get_container()/get_task(), ...) comes back to the
    LLM as "[error] <reason>" instead of crashing the MCP server.
  - Paths: a relative path is resolved against /testbed (see to_abs());
    whatever a tool outputs should use absolute paths.
  - Long output goes through truncate() before being returned -- except
    get_patch(), which must return the complete, unmodified diff (a
    truncated patch is a broken patch).

Settings (which container, which task) are resolved in this priority order:
  1. --container / --task-file command-line flags, if mcp_tools_swebench.py
     was launched with them directly. Only useful for manual testing.
  2. SWEBENCH_CONTAINER_NAME / SWEBENCH_TASK_FILE environment variables.
     This is the real path: agent_swebench/__main__.py sets these before
     spawning mcp_tools_swebench.py, and the sandbox CLI's own
     `--mcp-stdio "python mcp_tools_swebench.py"` invocation never passes
     any command-line flags at all.
  3. DEFAULT_CACHE_TASK_FILE (cache/swebench_task.json, moulinette's default
     dump path) plus a container name derived from that task's instance_id
     via common.docker_env.container_name() -- lets you test the tools in
     this package standalone against a container you started by hand,
     without running the full agent_swebench pipeline first.

Local testbed mode: if none of the above is set but TESTBED_PATH is, commands
run on this machine in that directory instead of in a container (see
_local_exec()). This is how exams/exam_sandbox.sh tests the tools, with a
small local repo and no Docker. There is no isolation in this mode.
"""
from __future__ import annotations

import argparse
import functools
import json
import os
import posixpath
import shlex
import subprocess
import sys
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

from common.docker_env import container_name as _derive_container_name
from common.docker_env import pull_image, start_container

TASK_FILE_ENV = "SWEBENCH_TASK_FILE"
CONTAINER_NAME_ENV = "SWEBENCH_CONTAINER_NAME"
TESTBED_PATH_ENV = "TESTBED_PATH"

REPO_DIR = "/testbed"  # SWE-bench images always check the repo out here
DEFAULT_TIMEOUT_SECONDS = 120

# truncate() thresholds, in characters (not lines -- a single line, e.g. a
# stack trace, can be huge on its own). Keep a chunk from the start (what
# command produced this) and a chunk from the end (errors are usually near
# the bottom); only cut the middle out.
TRUNCATE_MAX_CHARS = 20_000
TRUNCATE_HEAD_CHARS = 10_000
TRUNCATE_TAIL_CHARS = 5_000


@dataclass
class ExecResult:
    """Result of a docker_exec() call. Deliberately never raised as an
    exception on failure: a nonzero exit code or a timeout is a normal
    outcome the LLM needs to see, not a bug in our own code."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool


def never_raise(fn: Callable[..., str]) -> Callable[..., str]:
    """Tool-registration wrapper: catches anything fn raises and turns it
    into a "[error] <type>: <message>" string instead of letting the
    exception reach the MCP protocol layer. Every tool registered in
    mcp_tools_swebench.py goes through this, so no individual tool
    implementation has to remember to add its own top-level try/except for
    infrastructure failures (e.g. get_container()/get_task() raising on a
    missing task file). A tool can still return its own more specific
    "[error] ..." string for a domain-specific failure (edit_file's old_str
    not found, say) -- this wrapper is only the last-resort safety net."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            return f"[error] {type(e).__name__}: {e}"

    return wrapper


def _parse_cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--container", default=None)
    parser.add_argument("--task-file", default=None)
    # parse_known_args, not parse_args: mcp_tools_swebench.py is normally
    # launched with no flags at all (see module docstring, tier 2), so any
    # unrecognized argv should be ignored rather than causing a crash.
    args, _unknown = parser.parse_known_args()
    return args


_CLI_ARGS = _parse_cli_args()

_owned_container: Optional[str] = None
_container_error: Optional[str] = None

def _task_file() -> Optional[str]:
    return _CLI_ARGS.task_file or os.environ.get(TASK_FILE_ENV)

def _given_container() -> Optional[str]:
    return _CLI_ARGS.container or os.environ.get(CONTAINER_NAME_ENV)

def get_task() -> dict:
    task_file = _task_file()
    if not task_file:
        raise RuntimeError(
            f"No task file configured. Pass --task-file to "
            f"mcp_tools_swebench.py or set {TASK_FILE_ENV}."
        )
    if not os.path.exists(task_file):
        raise RuntimeError(f"Task file not found: {task_file!r}")
    with open(task_file, encoding="utf-8") as f:
        return json.load(f)

def get_container() -> str:
    container = _given_container() or _owned_container
    if container:
        return container
    if _container_error:
        raise RuntimeError(f"Could not start the task container: {_container_error}")
    raise RuntimeError(
        f"No container configured. Either pass --container / set "
        f"{CONTAINER_NAME_ENV} to use a running container, or pass "
        f"--task-file / set {TASK_FILE_ENV} to have the server start (and clean up its own)."
    )

def prepare_container() -> None:
    global _owned_container, _container_error
    if _given_container() or not _task_file():
        return
    try:
        task = get_task()
        name = f"{_derive_container_name(task['instance_id'])}-mcp-{os.getpid()}"
        _owned_container = name
        print(f"[mcp_tools_swebench] starting container {name} ...", file=sys.stderr)
        pull_image(task["docker_image"])
        start_container(task["docker_image"], name)
    except Exception as e:
        _container_error = f"{type(e).__name__}: {e}"
        cleanup_container()

def cleanup_container() -> None:
    global _owned_container
    name, _owned_container = _owned_container, None
    if name is None:
        return
    try:
        for cmd in (["docker", "kill", name], ["docker", "rm", "-f", name]):
            subprocess.run(
                cmd,
                capture_output=True,
                timeout=30,
                start_new_session=True,
            )
    except Exception as e:
        print(f"[mcp_tools_swebench] clean up of {name} failed: {e}", file=sys.stderr)

def to_abs(path: str) -> str:
    """Resolve a filepath the LLM gave us to an absolute path inside the
    container. A relative path is assumed to be relative to the repo
    checkout (REPO_DIR); an already-absolute path is returned unchanged.
    Uses posixpath explicitly (not pathlib) because the target path is
    always a Linux container path, regardless of what OS this bridge runs
    on."""
    if posixpath.isabs(path):
        return path
    return posixpath.normpath(posixpath.join(REPO_DIR,path))


def truncate(text: str) -> str:
    """Cut long tool output down to a manageable size before it goes back
    to the LLM (V.1 feedback (4)). Keeps a head and a tail instead of just
    hard-cutting from the start -- the command that produced the output is
    usually at the top, the error is usually at the bottom -- and always
    says explicitly how much was cut. Do NOT call this on get_patch()'s
    output: a truncated diff is a broken patch, not a shortened one."""
    if len(text) <= TRUNCATE_MAX_CHARS:
        return text
    omitted = len(text) - TRUNCATE_HEAD_CHARS - TRUNCATE_TAIL_CHARS
    head = text[:TRUNCATE_HEAD_CHARS]
    tail = text[-TRUNCATE_TAIL_CHARS:] if TRUNCATE_TAIL_CHARS else ""
    return f"{head}\n[... {omitted} characters omitted ...]\n{tail}"


# The container runtime prints its own chatter on every single call
# (podman stands in for docker on the 42 machines and announces itself each
# time); it is noise in every observation the LLM sees.
_RUNTIME_NOISE = ("Emulate Docker CLI using podman",)


def clean_stderr(stderr: str) -> str:
    """Drop the container runtime's own messages from a command's stderr,
    so a tool reports what the command said and nothing else."""
    kept = [
        line
        for line in (stderr or "").splitlines()
        if not any(noise in line for noise in _RUNTIME_NOISE)
    ]
    return "\n".join(kept).strip()


def docker_exec(
    command: str,
    workdir: str = REPO_DIR,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    input: Optional[str] = None,
) -> ExecResult:
    """Run `command` inside the current task's container via `docker exec`,
    optionally feeding it `input` on stdin. Never raises for a command
    failure: a timeout, a nonzero exit code, or anything docker itself
    prints to stderr all come back as a normal ExecResult for the caller to
    inspect -- only a genuine setup problem (no container configured at
    all, see get_container()) is still allowed to raise, and that gets
    caught by never_raise() at the tool-registration layer instead.

    Runs through `bash -lc` because each `docker exec` call is a brand new
    process -- it does NOT remember a previous call's
    `conda activate testbed`. If a tool needs that environment (running
    tests almost certainly does), prepend the activation to `command`
    yourself, e.g.:

        docker_exec(
            "source /opt/miniconda3/etc/profile.d/conda.sh && "
            "conda activate testbed && bin/test -C sympy/some/test.py"
        )

    `workdir` is passed straight to `docker exec -w`, a single argv item
    that never goes through a shell, so it needs no escaping here. Escaping
    only matters where a *tool* builds a piece of `command` itself out of
    LLM-supplied values (a filepath, a pattern, ...) -- use shlex.quote()
    there, e.g. `docker_exec(f"cat -n {shlex.quote(to_abs(filepath))}")` in
    read_file. `command` as a whole is never escaped or sanitized, on
    purpose: run_command's entire job is to let the LLM execute arbitrary
    shell.
    """
    local_root = _local_testbed()
    if local_root:
        return _local_exec(local_root, command, workdir, timeout, input)

    container = get_container()
    # -i keeps stdin attached: without it docker closes the container
    # process's stdin, so anything passed as `input` never arrives.
    stdin_flag = ["-i"] if input is not None else []

    # subprocess.run(timeout=...) below only kills the local `docker exec`
    # client process running on the HOST if we time out -- it has no way to
    # reach the actual work happening inside the container, which is left
    # running as an orphan. To make that recoverable, wrap `command` in a
    # thin outer shell that records its own PID to `marker` and then
    # `exec`s into `bash -lc command` -- exactly the original invocation,
    # just handed off via exec (which replaces the process image in place,
    # so the PID we recorded is still correct afterwards). On a timeout,
    # _kill_orphan() reads that PID back and kills it for real. See its
    # docstring for why.
    marker = f"/tmp/.docker_exec_{uuid.uuid4().hex}.pid"
    wrapped_command = (
        f"echo $$ > {marker}; "
        f"exec timeout -s KILL {timeout + 5} bash -lc {shlex.quote(command)}"
    )
    full_cmd = ["docker", "exec", *stdin_flag, "-w", workdir, container, "bash", "-c", wrapped_command]
    try:
        result = subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input,
        )
        return ExecResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            timed_out=False,
        )
    except subprocess.TimeoutExpired as e:
        _kill_orphan(container, marker)
        return ExecResult(
            returncode=-1,
            stdout=e.stdout or "",
            stderr=(e.stderr or "") + f"\n[docker_exec] timed out after {timeout}s",
            timed_out=True,
        )


def _local_testbed() -> Optional[str]:
    """The local testbed directory, or None to use a container as usual.
    A configured container or task file always wins over TESTBED_PATH."""
    if _given_container() or _task_file():
        return None
    path = os.environ.get(TESTBED_PATH_ENV)
    return os.path.abspath(path) if path else None


def _local_exec(
    root: str,
    command: str,
    workdir: str,
    timeout: int,
    input: Optional[str],
) -> ExecResult:
    """docker_exec() for local testbed mode: run `command` on this machine.

    The tools speak in container paths (/testbed/...), so /testbed is mapped
    to `root` on the way in -- in the command, the workdir, and stdin too,
    since edit_file sends its path as JSON on stdin -- and back to /testbed
    in the output, so tools still report /testbed/... paths.
    """

    def to_local(text):
        return text.replace(REPO_DIR, root) if text else text

    def to_container(text):
        return text.replace(root, REPO_DIR) if text else text

    try:
        result = subprocess.run(
            ["bash", "-c", to_local(command)],
            cwd=to_local(workdir),
            capture_output=True,
            text=True,
            timeout=timeout,
            input=to_local(input),
        )
    except subprocess.TimeoutExpired:
        return ExecResult(
            returncode=-1,
            stdout="",
            stderr=f"[docker_exec] timed out after {timeout}s",
            timed_out=True,
        )
    return ExecResult(
        returncode=result.returncode,
        stdout=to_container(result.stdout),
        stderr=to_container(result.stderr),
        timed_out=False,
    )


def _kill_orphan(container: str, marker: str) -> None:
    """Best-effort cleanup after a docker_exec() timeout.

    subprocess.run(timeout=...) only kills the local `docker exec` client
    process on the host; it has no idea the actual work is happening
    inside the container and never signals it. Left alone, that process
    (and anything it spawned -- a test runner, a build, ...) keeps running
    in the container, wasting CPU/memory and potentially still holding
    files or locks the next tool call needs.

    docker_exec() wrote that process's own PID to `marker` right before
    handing off to the caller's real command via `exec` (see its
    docstring). This reads the PID back and kills it three ways for good
    measure, since we can't be sure the shell became its own process
    group leader: as a negative PID (the whole process group, so children
    like pytest's own subprocesses die too), as the bare PID (the process
    itself), and via `pkill -P` (its direct children specifically) --
    then removes the marker file.

    Every step is wrapped so a missing container, a missing marker (the
    process may have already finished right as the timeout fired), or
    kill/pkill finding nothing to signal never raises: this is cleanup,
    not something the caller should have to handle failures from.
    """
    try:
        subprocess.run(
            [
                "docker", "exec", container, "bash", "-c",
                f'pid=$(cat {marker} 2>/dev/null); '
                f'if [ -n "$pid" ]; then '
                f'kill -KILL -- -"$pid" 2>/dev/null; '
                f'kill -KILL -- "$pid" 2>/dev/null; '
                f'pkill -KILL -P "$pid" 2>/dev/null; '
                f'fi; '
                f'rm -f {marker}',
            ],
            capture_output=True,
            timeout=10,
        )
    except Exception:
        pass  # best-effort only -- a failed cleanup must never crash the caller
