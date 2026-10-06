"""File system tools: read_file / edit_file / list_files (Section V.5.1)."""
import json
import shlex

from swebench_tools.docker_bridge import clean_stderr, docker_exec, to_abs, truncate

TESTBED_PYTHON = "/opt/miniconda3/envs/testbed/bin/python"

_PAST_EOF_EXIT = 3

_EDIT_CONTEXT_LINES = 3

_EDIT_SCRIPT = r'''
import io, json, os, py_compile, shutil, sys, tempfile

args = json.load(sys.stdin)
path, old, new, ctx = args["path"], args["old_str"], args["new_str"], args["context"]

def finish(msg):
    sys.stdout.write(msg + "\n")
    sys.exit(0)

if not old:
    finish("[error] old_str must not be empty.")
if not os.path.isfile(path):
    finish("[error] {} is not a file.".format(path))
try:
    with io.open(path, encoding="utf-8", newline="") as f:
        content = f.read()
except UnicodeDecodeError:
    finish("[error] {} is not UTF-8 text.".format(path))

count = content.count(old)
if count == 0:
    finish("[error] old_str not found in {}. It must match the file exactly, "
           "including whitespace and indentation -- use read_file to check "
           "the current content.".format(path))
if count > 1:
    lines, start = [], 0
    for _ in range(count):
        idx = content.find(old, start)
        lines.append(str(content.count("\n", 0, idx) + 1))
        start = idx + len(old)
    shown = ", ".join(lines[:10]) + (", ..." if count > 10 else "")
    finish("[error] old_str found {} times in {} (starting at lines {}). "
           "Nothing was changed. Include more surrounding lines in old_str so "
           "it matches exactly once.".format(count, path, shown))

idx = content.find(old)
first = content.count("\n", 0, idx) + 1
last = first + new.count("\n")
content = content[:idx] + new + content[idx + len(old):]
with io.open(path, "w", encoding="utf-8", newline="") as f:
    f.write(content)

all_lines = content.splitlines()
lo, hi = max(1, first - ctx), min(len(all_lines), last + ctx)
snippet = "\n".join("{}: {}".format(n, all_lines[n - 1]) for n in range(lo, hi + 1))
msg = "Edited {} (lines {}-{} now):\n{}".format(path, first, last, snippet)

if path.endswith(".py"):
    tmp = tempfile.mkdtemp()
    try:
        py_compile.compile(path, cfile=os.path.join(tmp, "check.pyc"), doraise=True)
    except py_compile.PyCompileError as e:
        msg += ("\n\n[warning] The edit was applied, but {} no longer compiles. "
                "Fix this before moving on:\n{}".format(path, e.msg.strip()))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
finish(msg)
'''


def read_file(filepath: str, start_line: int, end_line: int) -> str:
    """Read lines start_line..end_line (1-based, inclusive) of a file.
    Each output line is formatted as `<line_number>: <content>`.
    filepath can be relative to /testbed or absolute."""
    path = to_abs(filepath)
    start_line, end_line = int(start_line), int(end_line)
    if start_line < 1 or end_line < start_line:
        return (
            f"[error] Invalid line range {start_line}-{end_line}: need "
            f"1 <= start_line <= end_line."
        )

    quoted = shlex.quote(path)
    awk_prog = (
        f'NR >= {start_line} && NR <= {end_line} {{ print NR ": " $0 }} '
        f"NR > {end_line} {{ exit }} "
        f'END {{ if (NR < {start_line}) {{ print NR > "/dev/stderr"; exit {_PAST_EOF_EXIT} }} }}'
    )
    result = docker_exec(
        f"if [ ! -f {quoted} ]; then echo 'no such file' >&2; exit 1; fi; "
        f"awk {shlex.quote(awk_prog)} {quoted}"
    )
    if result.returncode == _PAST_EOF_EXIT:
        total = clean_stderr(result.stderr)
        return (
            f"[error] {path} has only {total} lines; start_line={start_line} "
            f"is past the end of the file."
        )
    if result.returncode != 0:
        return f"[error] Cannot read {path}: {clean_stderr(result.stderr)}"
    return truncate(result.stdout.rstrip("\n"))


def edit_file(filepath: str, old_str: str, new_str: str) -> str:
    """Replace old_str with new_str in a file. old_str must appear in the
    file exactly once (whitespace and indentation included); otherwise
    nothing is changed and an error says whether it was missing or found
    several times. After editing a .py file, it is compiled and any syntax
    error is reported."""
    path = to_abs(filepath)
    payload = json.dumps(
        {
            "path": path,
            "old_str": old_str,
            "new_str": new_str,
            "context": _EDIT_CONTEXT_LINES,
        }
    )
    python = shlex.quote(TESTBED_PYTHON)
    result = docker_exec(
        f"PY={python}; [ -x \"$PY\" ] || PY=python3; "
        f"PYTHONIOENCODING=utf-8 \"$PY\" -c {shlex.quote(_EDIT_SCRIPT)}",
        input=payload,
    )
    if result.returncode != 0:
        return f"[error] edit_file failed on {path}: {clean_stderr(result.stderr) or result.stdout.strip()}"
    return truncate(result.stdout.rstrip("\n"))


def list_files(directory: str, pattern: str) -> str:
    """List files under a directory (recursively) whose name matches a
    shell glob pattern such as "*.py". .git is skipped. One absolute path
    per line, sorted."""
    path = to_abs(directory)
    quoted = shlex.quote(path)
    result = docker_exec(
        f"if [ ! -d {quoted} ]; then echo 'no such directory' >&2; exit 1; fi; "
        f"find {quoted} -name .git -prune -o -type f -name {shlex.quote(pattern)} -print | sort"
    )
    if result.returncode != 0:
        return f"[error] Cannot list {path}: {clean_stderr(result.stderr)}"
    if not result.stdout.strip():
        return f"No files matching {pattern!r} under {path}."
    return truncate(result.stdout.rstrip("\n"))