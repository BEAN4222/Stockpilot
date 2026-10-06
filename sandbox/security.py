"""
Sandbox security (Section V.2 points 3, 4).

Five things the sandbox has to hold up under `tests/test_sandbox_security.py`:
  1. import allowlist    -- only whitelisted modules can be imported
  2. path allowlist       -- file access restricted to allowed_directories
  3. network block         -- socket.socket() patched to raise (block_network())
  4. timeout               -- enforced by killing the worker process (executor.py)
  5. memory limit          -- resource.setrlimit, applied here, called once when
                              the worker process starts (executor.py)

Before this, network access was only blocked as a side effect of
authorized_imports never including socket/urllib/requests/etc. -- true
today, but not a dedicated guarantee: if a networking module is ever
added to the allowlist for a legitimate reason, access would silently
reopen with no separate safety net. block_network() patches the actual
primitive underneath nearly all Python networking (socket.socket) so
construction raises no matter how code got a reference to it.

Only the standard library is used here (no third-party sandboxing libs).
"""
from __future__ import annotations

import builtins
import os
import resource
from typing import Iterable, Set


def check_path_allowed(path: str, allowed_directories: Iterable[str]) -> bool:
    """True if `path` resolves to somewhere inside one of `allowed_directories`.

    `os.path.realpath` collapses `..`, symlinks and relative segments into a
    clean absolute path first, so a trick like "/testbed/../../etc/passwd"
    can't sneak past the string comparison below.
    """
    real_path = os.path.realpath(path)
    for allowed in allowed_directories:
        real_allowed = os.path.realpath(allowed)
        if real_path == real_allowed or real_path.startswith(real_allowed + os.sep):
            return True
    return False


class ImportGuard:
    """Intercepts `__import__`, only letting whitelisted modules through.

    `authorized_imports` entries can be an exact module name ("math") or a
    wildcard covering all of a package's submodules ("math.*").
    """

    def __init__(self, authorized_imports: Iterable[str]):
        self.authorized: Set[str] = set(authorized_imports)
        self._real_import = builtins.__import__
        self._safe_operator = None

    def _is_authorized(self, module_name: str) -> bool:
        if module_name in self.authorized:
            return True
        top_level = module_name.split(".")[0]
        if top_level in self.authorized:
            return True
        if f"{top_level}.*" in self.authorized:
            return True
        return False

    def guarded_import(self, name, globals=None, locals=None, fromlist=(), level=0):
        if not self._is_authorized(name):
            raise ImportError(f"Module '{name}' is not in the authorized imports allowlist")
        module = self._real_import(name, globals, locals, fromlist, level)
        # `operator` gets a checked copy (attrgetter/methodcaller refuse private names).
        if name == "operator" and getattr(module, "__name__", "") == "operator":
            from sandbox.code_guard import make_safe_operator

            if self._safe_operator is None:
                self._safe_operator = make_safe_operator(module)
            return self._safe_operator
        return module

    def install(self) -> None:
        builtins.__import__ = self.guarded_import

    def uninstall(self) -> None:
        builtins.__import__ = self._real_import


def block_network() -> None:
    """Defense-in-depth network block (Section V.2: "No network access").

    Must be called from trusted code -- executor.py's _worker_main(),
    right alongside where ImportGuard is set up -- BEFORE the worker
    installs the ImportGuard as the sandboxed namespace's __import__.
    It needs the real, unguarded `import socket` to patch the module in
    the first place; by the time sandboxed code runs, socket.socket is
    already neutered regardless of whether `socket` itself is
    importable.

    Patches socket.socket (not just adds "socket" to a blocklist)
    because it's the primitive nearly every higher-level networking
    path -- sockets directly, socket.create_connection, urllib,
    http.client, requests via urllib3 -- ultimately constructs. One
    patch, one place, covers all of them instead of chasing each
    library separately.
    """
    import socket

    def _blocked(*args, **kwargs):
        raise PermissionError("Network access is not allowed in the sandbox")

    socket.socket = _blocked


def build_restricted_builtins(allowed_directories: Iterable[str]) -> dict:
    """A copy of the normal builtins, with the dangerous ones removed or wrapped.

    - `open` is replaced with a version that checks the path first.
    - `eval` / `exec` / `compile` are removed: the sandbox itself needs `exec`
      to run the LLM's code once, but the LLM's code should not be able to
      call `exec`/`eval` again from inside to route around the guards above
      (`ImportGuard` already covers plain `import` statements).
    - `input` / `breakpoint` are removed: no interactive prompts from inside
      untrusted code.
    - `__import__` is intentionally left in place here; the caller (the
      sandbox worker) overwrites it with the ImportGuard's guarded version.
      The import statement needs *some* callable named __import__ to exist
      in builtins, so removing it outright breaks even whitelisted imports.
    """
    safe_builtins = dict(vars(builtins))
    real_open = builtins.open

    def guarded_open(file, mode="r", *args, **kwargs):
        if not check_path_allowed(str(file), allowed_directories):
            raise PermissionError(f"Access to path '{file}' is not allowed")
        return real_open(file, mode, *args, **kwargs)

    safe_builtins["open"] = guarded_open

    for name in ("eval", "exec", "compile", "input", "breakpoint"):
        safe_builtins.pop(name, None)

    return safe_builtins


def apply_memory_limit(max_memory_mb: int) -> None:
    """Cap this process's total address space by lowering the SOFT
    RLIMIT_AS only -- the hard limit is left untouched (usually
    RLIM_INFINITY), specifically so a later `reset_memory_limit()` call can
    raise the soft limit back up. Call this around the one thing that
    actually needs capping (the worker's `exec(code, namespace)` call, see
    sandbox/executor.py) -- never in the parent/agent process, and never
    left on permanently in the worker (see reset_memory_limit below for why).

    KNOWN LIMITATION: RLIMIT_AS is enforced reliably by the Linux kernel,
    but is loosely enforced by the macOS (Darwin/XNU) kernel -- on Mac this
    is a best-effort cap, not a guarantee. What still holds on Mac even if
    this doesn't fire: since the sandboxed code now runs in its own worker
    process (sandbox/executor.py), if the OS ends up OOM-killing that
    process anyway, only the worker dies -- the agent loop and the rest of
    the program keep running, and the crash is reported back as an
    observation instead of taking the whole run down.
    """
    if max_memory_mb <= 0:
        return  # 0 or negative means "no limit" -- don't call setrlimit(0)
    limit_bytes = max_memory_mb * 1024 * 1024
    try:
        _, hard = resource.getrlimit(resource.RLIMIT_AS)
        new_soft = limit_bytes if hard == resource.RLIM_INFINITY else min(limit_bytes, hard)
        resource.setrlimit(resource.RLIMIT_AS, (new_soft, hard))
    except (ValueError, OSError):
        # Some platforms/containers refuse to lower RLIMIT_AS at all.
        # Don't crash the worker over it -- the process-level isolation
        # (killable/OOM-killable independently of the parent) still holds.
        pass


def reset_memory_limit() -> None:
    """Undo apply_memory_limit(): raise the soft RLIMIT_AS back up to the
    (untouched) hard limit -- usually RLIM_INFINITY, i.e. no cap.

    Why this exists: a tight memory cap (e.g. 128MB in tests) is barely
    enough headroom for the user's own code, let alone the worker's OWN
    bookkeeping between calls -- notably, `multiprocessing.Queue.put()`
    lazily starts a background feeder thread on its first use, which needs
    to mmap a new thread stack. On a real Linux kernel (which -- unlike
    macOS -- actually enforces RLIMIT_AS) a fresh, fully-loaded, forked
    CPython process can already be sitting close to a tight cap, and that
    mmap fails with "RuntimeError: can't start new thread" -- not because
    anything is actually leaking or wrong, just because the cap meant for
    the sandboxed code was still in effect for the worker's own plumbing.
    Calling this right after exec() finishes, before touching the result
    queue, keeps the cap scoped to exactly the code it's meant to constrain.
    """
    try:
        _, hard = resource.getrlimit(resource.RLIMIT_AS)
        resource.setrlimit(resource.RLIMIT_AS, (hard, hard))
    except (ValueError, OSError):
        pass
