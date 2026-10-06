"""Code search tools: search_code / search_function_or_class_definition_in_code /
find_references (Section V.5.2)."""
import re
import shlex

from swebench_tools.docker_bridge import REPO_DIR, clean_stderr, docker_exec, to_abs, truncate

# A broad pattern can match thousands of lines; showing the first hundred
# and saying so is more useful to the LLM than a wall of text.
MAX_MATCHES = 100

# Directories that are never the answer to "where is this code", and that
# make the ast walk below much slower if not skipped.
SKIP_DIRS = ".git __pycache__ .tox build dist node_modules .eggs"

# Runs inside the container: parses every .py file with Python's own ast
# module and reports real definitions of the wanted name.
_AST_DEFINITION_SCRIPT = """
import ast, os

TARGET = %(name)r
SKIP_DIRS = set(%(skip)r.split())

for root, dirs, files in os.walk("."):
    dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
    for filename in files:
        if not filename.endswith(".py"):
            continue
        path = os.path.join(root, filename)
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                source = handle.read()
            tree = ast.parse(source)
        except (SyntaxError, OSError, ValueError):
            continue  # unparsable file: not what we are looking for anyway
        lines = source.splitlines()
        for node in ast.walk(tree):
            is_definition = isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            )
            if is_definition and node.name == TARGET:
                number = node.lineno
                content = lines[number - 1].strip() if number <= len(lines) else ""
                print(os.path.abspath(path) + ":" + str(number) + " " + content)
"""

# SWE-bench images ship different Pythons (some only have `python`), so use
# whichever exists. `exec` replaces the shell, which keeps stdin attached to
# the interpreter so it can read the script piped into it.
_PYTHON_IN_CONTAINER = (
    "if command -v python3 >/dev/null 2>&1; then exec python3 -; else exec python -; fi"
)


def _format_matches(grep_output: str, skip: tuple = ()) -> str:
    """Turn `grep -n` output ("./path.py:12:    content") into the format
    Section V.5.2 requires ("/testbed/path.py:12 content").

    Paths come back relative because the search runs from the repo
    directory, so "./" is expanded rather than calling to_abs() per line.

    `skip` holds (absolute_path, line_number) pairs to leave out, which is
    how find_references drops the definition line itself.
    """
    lines = []
    for raw_line in grep_output.splitlines():
        parts = raw_line.split(":", 2)
        if len(parts) != 3:
            continue  # not a match line (e.g. "grep: ...: No such file")
        path, line_number, content = parts
        if not line_number.isdigit():
            continue
        path = to_abs(path[2:] if path.startswith("./") else path)
        if (path, int(line_number)) in skip:
            continue
        lines.append(f"{path}:{line_number} {content.strip()}")

    if not lines:
        return "(no matches)"
    total = len(lines)
    if total > MAX_MATCHES:
        lines = lines[:MAX_MATCHES]
        lines.append(f"... [{total - MAX_MATCHES} more matches; narrow your search]")
    return truncate("\n".join(lines))


def search_code(pattern: str, file_pattern: str) -> str:
    """Grep-like search. Output format:
    /absolute/path/to/file.py:<line_number> <line_content>

    `pattern` is an extended grep regex (so "a|b" works), `file_pattern` a
    shell glob such as "*.py".
    """
    # -E because LLMs write "a|b" and expect alternation, which basic grep
    # reads as the literal text "a|b". A pattern that is not a valid regex
    # (e.g. "def handle(") is searched as plain text (-F) instead of failing.
    try:
        re.compile(pattern)
        mode = "-E"
    except re.error:
        mode = "-F"
    if "/" in file_pattern:
        # --include only matches the base name, so "tests/x/test_y.py" would
        # silently match nothing; the LLM then concludes the code is absent
        # and keeps searching. Match the whole path with find instead.
        # -H keeps the file name even when only one file matches.
        result = docker_exec(
            f"find {REPO_DIR} -path {shlex.quote(to_abs(file_pattern))} -type f "
            f"-exec grep -nIH {mode} -e {shlex.quote(pattern)} {{}} +"
        )
    else:
        # -I skips binary files, -n adds line numbers, --include applies the glob.
        result = docker_exec(
            f"grep -rnI {mode} --include={shlex.quote(file_pattern)} -e {shlex.quote(pattern)} ."
        )
    if result.timed_out:
        return "[search_code] timed out; try a narrower pattern"
    # grep exits 1 when there is simply no match, which is not an error here.
    if result.returncode not in (0, 1):
        return f"[search_code] failed: {truncate(clean_stderr(result.stderr))}"
    return _format_matches(result.stdout)


def search_function_or_class_definition_in_code(name: str) -> str:
    """Find the definition of a function or class. Same output format as
    search_code.

    Parses every .py file in the repo with Python's own `ast` module inside
    the container, rather than grepping for "def name". A grep also matches
    calls, comments, strings and similarly-named functions, while the
    parser only reports real definitions, and finds methods inside classes
    too (which "^def name" would miss because of the indentation).
    """
    script = _AST_DEFINITION_SCRIPT % {"name": name, "skip": SKIP_DIRS}
    result = docker_exec(_PYTHON_IN_CONTAINER, input=script)
    if result.timed_out:
        return "[search_function_or_class_definition_in_code] timed out"
    if result.returncode != 0:
        return (
            "[search_function_or_class_definition_in_code] failed: "
            f"{truncate(clean_stderr(result.stderr))}"
        )
    output = result.stdout.strip()
    return truncate(output) if output else "(no definition found)"


def find_references(name: str, filepath: str, line: int) -> str:
    """Find all usages of a symbol. Same output format as search_code.

    `filepath` and `line` point at the symbol's own definition, which is
    left out of the result: the caller already knows where it is defined
    and wants to see who *uses* it.
    """
    # -w matches whole words only, so "run" does not also match "prerun".
    result = docker_exec(f"grep -rnIw --include=*.py -e {shlex.quote(name)} .")
    if result.timed_out:
        return "[find_references] timed out"
    if result.returncode not in (0, 1):
        return f"[find_references] failed: {truncate(clean_stderr(result.stderr))}"
    return _format_matches(result.stdout, skip=((to_abs(filepath), int(line)),))
