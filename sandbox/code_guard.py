"""
Hardening for the sandbox worker (Section V.2 point 3).

ImportGuard only sees `import` statements. Code that may import `random` can
still reach the real `os` module through what `random` holds (`random._os`),
through frames (`gen.gi_frame.f_back.f_globals`) or through
`getattr(random, "_os")`. This module closes those direct routes:

  1. check_code(): rejects code that touches private or introspection attributes
  2. harden_builtins(): getattr/setattr/delattr refuse the same names, and
     `vars` (which hands out a module's whole __dict__) is removed
  3. scrub_environment(): the worker keeps no copy of the parent's environment,
     so secrets such as API keys cannot be read from inside the sandbox

An in-process Python sandbox can never be airtight. These checks raise the
cost of an escape, they do not make one impossible.
"""
from __future__ import annotations

import ast
import os


class SandboxSecurityError(PermissionError):
    """Raised when sandboxed code tries something the sandbox forbids."""


# Dunder attributes that are harmless and that normal code uses.
ALLOWED_DUNDER_ATTRS = frozenset(
    {"__init__", "__name__", "__doc__", "__repr__", "__str__"}
)

# Attributes that expose frames, code objects or tracebacks.
BLOCKED_ATTRS = frozenset(
    {
        "gi_frame", "gi_code", "gi_yieldfrom",
        "cr_frame", "cr_code", "cr_await",
        "ag_frame", "ag_code", "ag_await",
        "tb_frame", "tb_next",
        "f_back", "f_globals", "f_locals", "f_builtins", "f_code", "f_trace",
        "func_globals", "func_code",
    }
)

REMOVED_BUILTINS = ("vars",)


def is_blocked_attr(name: str) -> bool:
    if name in BLOCKED_ATTRS:
        return True
    return name.startswith("_") and name not in ALLOWED_DUNDER_ATTRS


def _deny(name: str) -> SandboxSecurityError:
    return SandboxSecurityError(
        f"Access to attribute '{name}' is not allowed in the sandbox"
    )


def check_code(code: str) -> None:
    """Raise SandboxSecurityError if the code touches a blocked attribute."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return  # compiling the code later reports the syntax error itself
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and is_blocked_attr(node.attr):
            raise _deny(node.attr)
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if is_blocked_attr(alias.name):
                    raise _deny(alias.name)


def harden_builtins(restricted_builtins: dict) -> None:
    """Replace the attribute builtins with checked versions, drop `vars`."""

    def _check_name(name) -> None:
        if isinstance(name, str) and is_blocked_attr(name):
            raise _deny(name)

    def safe_getattr(obj, name, *default):
        _check_name(name)
        return getattr(obj, name, *default)

    def safe_setattr(obj, name, value):
        _check_name(name)
        setattr(obj, name, value)

    def safe_delattr(obj, name):
        _check_name(name)
        delattr(obj, name)

    restricted_builtins["getattr"] = safe_getattr
    restricted_builtins["setattr"] = safe_setattr
    restricted_builtins["delattr"] = safe_delattr
    for name in REMOVED_BUILTINS:
        restricted_builtins.pop(name, None)


def scrub_environment() -> None:
    """Forget the parent's environment variables inside the worker process.

    The worker never needs them: tool calls go through the parent, which owns
    the MCP connection and the API keys.
    """
    os.environ.clear()


# ---------------------------------------------------------------------------
# Checked copy of `operator`
# ---------------------------------------------------------------------------
# operator.attrgetter / methodcaller read attributes by *string*, which the AST
# check cannot see. Sandboxed code gets a copy of the module whose versions of
# these two refuse private names. The real `operator` module is not modified.


def make_safe_operator(real_operator):
    import types

    safe = types.ModuleType("operator")
    for name in dir(real_operator):
        if not name.startswith("__"):
            setattr(safe, name, getattr(real_operator, name))

    def _check_dotted(name) -> None:
        if isinstance(name, str):
            for part in name.split("."):
                if is_blocked_attr(part):
                    raise _deny(part)

    real_attrgetter = real_operator.attrgetter
    real_methodcaller = real_operator.methodcaller

    def attrgetter(*names):
        for n in names:
            _check_dotted(n)
        return real_attrgetter(*names)

    def methodcaller(name, *args, **kwargs):
        _check_dotted(name)
        return real_methodcaller(name, *args, **kwargs)

    safe.attrgetter = attrgetter
    safe.methodcaller = methodcaller
    return safe
