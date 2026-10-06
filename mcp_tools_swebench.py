"""
MCP server entry point for SWE-bench tasks (Section V.4 + V.5).

Must live at the repository root (the PDF requires this). This file's only
job is to register the 9 mandatory tools and start the server -- the actual
implementations live in swebench_tools/{fs_tools,search_tools,exec_tools}.py,
and the shared plumbing (docker_exec, get_container, get_task, to_abs,
truncate, never_raise) lives in swebench_tools/docker_bridge.py. See that
module's docstring for the full set of shared conventions (never raise,
absolute-path output, truncate everything except get_patch) and for how
this process learns which container/task to serve.

Each tool is wrapped in never_raise() before being registered, so a bug or
an infrastructure failure (e.g. a misconfigured container) comes back to
the LLM as a "[error] ..." string instead of crashing this server.
"""
import os
import signal
import sys
import threading
import time

from mcp.server.fastmcp import FastMCP

from swebench_tools.docker_bridge import (
    cleanup_container,
    never_raise,
    prepare_container,
)

from swebench_tools.exec_tools import get_patch, run_command, run_tests
from swebench_tools.fs_tools import edit_file, list_files, read_file
from swebench_tools.search_tools import (
    find_references,
    search_code,
    search_function_or_class_definition_in_code,
)

mcp = FastMCP("swebench-tools")

_TOOLS = (
    read_file,
    edit_file,
    list_files,
    search_code,
    search_function_or_class_definition_in_code,
    find_references,
    run_tests,
    get_patch,
    run_command,
)

for _fn in _TOOLS:
    mcp.tool()(never_raise(_fn))

def _exit_on_sigterm(signum, frame):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    sys.exit(0)

def _exit_when_orphaned(parent_pid: int) -> None:
    while os.getppid() == parent_pid:
        time.sleep(1)
    cleanup_container()
    os._exit(0)

if __name__ == "__main__":
    # Default: stdio (the client launches us). With --http we run as a
    # standalone server that clients connect to by URL -- same flags as
    # mcp_tools_mbpp.py:
    #   python mcp_tools_swebench.py --http --port 8000 --task-file cache/swebench_task.json
    #   -> http://127.0.0.1:8000/mcp
    # parse_known_args, not parse_args: --container / --task-file belong to
    # docker_bridge's own parser, so they must pass through untouched here.
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--http", action="store_true", help="serve over streamable HTTP instead of stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args, _unknown = parser.parse_known_args()

    signal.signal(signal.SIGTERM, _exit_on_sigterm)
    threading.Thread(target=_exit_when_orphaned, args=(os.getppid(),),daemon=True).start()
    try:
        prepare_container()
        if args.http:
            # uvicorn takes over SIGINT/SIGTERM while serving, then restores
            # our handlers and re-raises the signal on shutdown -- so Ctrl+C
            # still ends up in the finally below and removes the container.
            mcp.settings.host = args.host
            mcp.settings.port = args.port
            mcp.run(transport="streamable-http")
        else:
            mcp.run()
    finally:
        cleanup_container()