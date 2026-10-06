"""
Tiny .env loader.

Reads KEY=VALUE lines from a .env file and puts them into os.environ,
without overwriting a value that's already set in the environment
(a value you `export`ed yourself in the shell should win over the file).
"""
from __future__ import annotations

import os


def load_env_file(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, value)
