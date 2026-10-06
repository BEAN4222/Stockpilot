"""Hard time limit helpers: a write-once result file, a watchdog timer,
and a helper to run a function with a time budget."""
from __future__ import annotations

import os
import threading
import time
from typing import Callable, Optional


class ResultWriter:
    """Writes solution.json at most once, so the normal path and the
    watchdog can never overwrite each other."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._written = False

    def write(self, result) -> bool:
        with self._lock:
            if self._written:
                return False
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(result.model_dump_json(indent=2))
            self._written = True
            return True


def start_watchdog(
    seconds_from_start: float, start: float, on_expire: Callable[[], None]
) -> threading.Timer:
    """After `seconds_from_start` (measured from `start`, a perf_counter
    value), call on_expire() and then kill the whole process. A daemon
    timer dies by itself if the program finishes first."""
    delay = max(0.0, start + seconds_from_start - time.perf_counter())

    def _fire() -> None:
        try:
            on_expire()
        except Exception:
            pass
        finally:
            os._exit(0)

    timer = threading.Timer(delay, _fire)
    timer.daemon = True
    timer.start()
    return timer


def run_with_timeout(fn: Callable[[], Optional[str]], seconds: float) -> Optional[str]:
    """Run fn() in a daemon thread and give up after `seconds`."""
    box: dict = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except Exception:
            box["value"] = None

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(max(0.0, seconds))
    return box.get("value")
