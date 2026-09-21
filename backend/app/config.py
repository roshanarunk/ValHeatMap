"""Environment configuration.

Reads a `.env` file from the repo root if present, so the API key can live
in one gitignored file instead of being exported in every shell.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


def load_env(path: Path | None = None) -> dict[str, str]:
    """Load KEY=VALUE lines into the environment. Existing vars win."""
    target = path or ENV_PATH
    loaded: dict[str, str] = {}
    if not target.exists():
        return loaded
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key:
            loaded[key] = value
            # A real environment variable takes precedence over the file.
            os.environ.setdefault(key, value)
    return loaded
