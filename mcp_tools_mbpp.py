"""
MBPP MCP tools server (Section V.3, point 2).

Exposes one tool, run_tests(code, test_list=None), which checks a candidate
solution against the current task's tests, or against `test_list` if given.
It returns JSON: {"success": <all tests passed>, "output": <per-test report>}.

The task file path comes from the MBPP_TASK_FILE environment variable
(set by agent_mbpp/__main__.py before this server is launched). It is only
needed when `test_list` is not given.
"""
import json
import os
import re
import subprocess
import sys
from typing import List, Optional

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("mbpp-tools")

TASK_FILE_ENV = "MBPP_TASK_FILE"
TEST_TIMEOUT_SECONDS = 10

# Small script that runs each test separately and prints PASS/FAIL per test.
# It is executed in a separate Python process, never inside this server,
# so broken or malicious code cannot crash or take over the MCP server.
_HARNESS = '''
import json as _json
_tests = _json.loads({tests_json!r})
_passed = 0
for _i, _t in enumerate(_tests, start=1):
    try:
        exec(_t, globals())
        print(f"  test {{_i}}: PASS  ({{_t}})")
        _passed += 1
    except AssertionError:
        print(f"  test {{_i}}: FAIL  ({{_t}})")
    except Exception as _e:
        print(f"  test {{_i}}: ERROR ({{_t}}) -> {{type(_e).__name__}}: {{_e}}")
print(f"[run_tests] {{_passed}}/{{len(_tests)}} tests passed")
'''

# The harness's last line, used to decide `success`.
_SUMMARY_LINE = re.compile(r"\[run_tests\] (\d+)/(\d+) tests passed")


def _load_current_task() -> dict:
    task_file = os.environ.get(TASK_FILE_ENV)
    if not task_file or not os.path.exists(task_file):
        raise RuntimeError(
            f"Could not find the task file (env var {TASK_FILE_ENV}={task_file!r})."
        )
    with open(task_file, encoding="utf-8") as f:
        return json.load(f)


@mcp.tool()
def run_tests(code: str, test_list: Optional[List[str]] = None) -> str:
    """Run the task's tests against the given solution code.

    Pass the full source code of your solution as `code`, for example:
        print(run_tests(code=solution))
    `test_list` is optional: a list of assert statements to run instead of
    the task's own tests.
    Returns JSON: {"success": true/false, "output": "<pass/fail line per test and a summary>"}.
    """
    output = _run_tests_report(code, test_list)
    match = _SUMMARY_LINE.search(output)
    success = bool(match) and match.group(1) == match.group(2)
    return json.dumps({"success": success, "output": output})


def _run_tests_report(code: str, test_list: Optional[List[str]]) -> str:
    """Run the tests in a separate process; return the plain-text report."""
    if test_list is None:
        task = _load_current_task()
        test_imports = task.get("test_imports", [])
        test_list = task.get("test_list", [])
    else:
        test_imports = []

    script ="\n".join(test_imports) + "\n" + code + "\n" + _HARNESS.format(
        tests_json=json.dumps(test_list)
    )

    try:
        proc = subprocess.run(
            [sys.executable, "-c", script],
            # Don't inherit our stdin: under the stdio transport it is the
            # MCP pipe, and the tested code must not read (or block on) it.
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=TEST_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return f"[run_tests] Timed out after {TEST_TIMEOUT_SECONDS}s (infinite loop?)"

    if "[run_tests]" not in proc.stdout:
        # The solution itself failed before any test could run (e.g. SyntaxError).
        return "[run_tests] Failed to load the solution:\n" + proc.stderr.strip()
    return proc.stdout.strip()


if __name__ == "__main__":
    # Default: stdio (the client launches us). With --http we run as a
    # standalone server that clients connect to by URL:
    #   python mcp_tools_mbpp.py --http --port 8000  ->  http://127.0.0.1:8000/mcp
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--http", action="store_true", help="serve over streamable HTTP instead of stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    if args.http:
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport="streamable-http")
    else:
        mcp.run()