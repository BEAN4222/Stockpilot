"""Execution tools: run_tests / get_patch / run_command (Section V.5.3)."""
import re

from swebench_tools.docker_bridge import clean_stderr, docker_exec, get_task, to_abs, truncate

# The eval script reinstalls the package and runs a whole test suite, so it
# needs far more than docker_exec's default timeout.
RUN_TESTS_TIMEOUT_SECONDS = 300

# eval_script's "restore the test files" step: git checkout <base_commit> <test files...>
_RESET_TESTS_LINE = re.compile(r"^git checkout [0-9a-f]{7,40} .+$", re.MULTILINE)

# Written inside the container, deliberately outside the repo: a file under
# /testbed would show up in get_patch()'s diff and corrupt the answer.
EVAL_SCRIPT_PATH = "/tmp/agent_eval_script.sh"


def _summarize_test_output(log: str) -> str:
    """Turn a full eval-script log into something an LLM can read.

    `log` is stdout and stderr merged in the order they were written (the
    script runs with 2>&1). That matters: under `set -x` the shell traces
    the ">>>>> Start/End Test Output" markers to stderr, and some runners
    also write their results to stderr (unittest/Django) while others use
    stdout (sympy's bin/test, pytest). Only one ordered stream puts the
    markers and the results in the right place relative to each other.
    """
    section = log
    start = log.find("Start Test Output")
    if start != -1:
        end = log.find("End Test Output", start)
        section = log[start:end if end != -1 else len(log)]

    summary_lines = []
    # unittest / Django: "test_x (module.Class) ... ok" / "... FAIL" / "... ERROR"
    unittest_results = re.findall(r" \.\.\. (ok|FAIL|ERROR)\s*$", section, flags=re.MULTILINE)
    if unittest_results:
        passed = unittest_results.count("ok")
        failed = unittest_results.count("FAIL")
        errors = unittest_results.count("ERROR")
        summary_lines = [
            line.strip()
            for line in section.splitlines()
            if re.match(r"^(Ran \d+ tests? in|FAILED \(|OK\b)", line.strip())
        ]
        failing = re.findall(r"^(?:FAIL|ERROR): (\S+ \([^)]*\))", section, flags=re.MULTILINE)
    else:
        summary_lines = [
            line.strip()
            for line in section.splitlines()
            if re.search(r"\b\d+\s+(passed|failed|error)", line)
            or re.match(r"^=+.*\b(passed|failed|error)\b.*=+$", line.strip())
            or "tests finished" in line
        ]
        # Prefer the runner's own totals ("tests finished: 45 passed, 1 failed",
        # "== 1 failed, 1 passed in 0.1s =="). Only without them, count result
        # tokens -- pytest -rA prints each one twice (live line + short summary).
        counts = {"passed": 0, "failed": 0, "error": 0}
        found = False
        for line in summary_lines:
            for count, word in re.findall(r"\b(\d+)\s+(passed|failed|error)", line):
                counts[word] = max(counts[word], int(count))
                found = True
        if found:
            passed, failed, errors = counts["passed"], counts["failed"], counts["error"]
        else:
            passed = len(re.findall(r"\bPASSED\b", section))
            failed = len(re.findall(r"\bFAILED\b", section))
            errors = len(re.findall(r"\bERROR\b", section))
        # pytest: "FAILED path::test_name"; sympy underlines the name instead.
        failing = re.findall(r"^(?:FAILED|ERROR)\s+(\S+)", section, flags=re.MULTILINE)
        failing += re.findall(
            r"^_{3,}\s+(\S*[A-Za-z0-9]\S*)\s+_{3,}$", section, flags=re.MULTILINE
        )

    lines = [f"[run_tests] {passed} passed, {failed} failed, {errors} errors"]
    lines.extend(dict.fromkeys(summary_lines))  # de-duplicated, order kept

    if failing:
        lines.append("Failing tests:")
        lines.extend(f"  {name}" for name in dict.fromkeys(failing[:20]))
        if len(failing) > 20:
            lines.append(f"  ... and {len(failing) - 20} more")

    # Keep the tail, so a failure before any test ran (an import error, a
    # patch that would not apply) is still visible.
    tail_source = section if (passed or failed or errors) else log
    lines.append("--- end of test output ---")
    lines.append("\n".join(tail_source.strip().splitlines()[-25:]))
    return truncate("\n".join(lines))


def run_tests() -> str:
    """Execute the evaluation script (eval_script from SWEBenchTaskInput).

    The script is passed on stdin, not on the command line: it contains
    quotes, heredocs and patch text that would break if pasted into a shell
    command, and a single command-line argument is limited to 128KB.

    It is written to /tmp, never into the repository, because any file
    under /testbed would appear in get_patch()'s diff.

    If it times out, the eval script never reaches its final step that
    restores the test files, so that step is run here -- otherwise the
    gold test patch would leak into get_patch()'s diff.

    Returns a summary (pass/fail counts, failing test names, the tail of
    the log), not the full log, which is thousands of lines long.
    """
    task = get_task()
    eval_script = task.get("eval_script", "")
    if not eval_script.strip():
        return "[run_tests] the task file has no eval_script"

    result = docker_exec(
        f"cat > {EVAL_SCRIPT_PATH} && bash {EVAL_SCRIPT_PATH} 2>&1",
        timeout=RUN_TESTS_TIMEOUT_SECONDS,
        input=eval_script,
    )
    if result.timed_out:
        reset = _RESET_TESTS_LINE.search(eval_script)
        if reset:
            docker_exec(reset.group(0))
        return f"[run_tests] timed out after {RUN_TESTS_TIMEOUT_SECONDS}s (test files restored)"
    return _summarize_test_output(result.stdout)


def get_patch() -> str:
    """Return `git -c core.fileMode=false diff` from the repo.

    This is the agent's actual answer (final_answer() submits it), so the
    output is returned exactly as git produced it and is never truncated:
    a cut-off diff cannot be applied.

    `core.fileMode=false` keeps permission-only changes out of the diff,
    as the project requires.
    """
    result = docker_exec("git -c core.fileMode=false diff")
    if result.timed_out:
        return "[get_patch] timed out"
    if result.returncode != 0:
        return f"[get_patch] failed: {truncate(clean_stderr(result.stderr))}"
    return result.stdout if result.stdout.strip() else "(no changes in the repository yet)"


def run_command(command: str, workdir: str) -> str:
    """Run a shell command in the container and report what happened.

    Both streams are reported separately, along with the exit code, since
    a command can fail while still printing something useful.
    """
    result = docker_exec(command, workdir=to_abs(workdir or "."))
    if result.timed_out:
        return f"exit_code: -1\n[run_command] timed out\n{truncate(result.stderr)}"

    return "\n".join(
        [
            f"exit_code: {result.returncode}",
            "stdout:\n" + (truncate(result.stdout.rstrip()) or "(empty)"),
            "stderr:\n" + (truncate(clean_stderr(result.stderr)) or "(empty)"),
        ]
    )
