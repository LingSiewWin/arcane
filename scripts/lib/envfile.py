"""envfile.py — a tiny, zero-dependency .env loader for the Python backend.

Next.js auto-loads `web/apps/web/.env.local` for the web app; the Python side has
no such mechanism, so this loads a root `.env` into `os.environ`. It deliberately
does NOT override variables already set in the environment, so an explicit
`export` or a CLI-provided value always wins over the file.

Format: `KEY=VALUE` per line; `#` comments and blank lines ignored; surrounding
single/double quotes stripped. No interpolation, no multiline — keep `.env` simple.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

# scripts/lib/envfile.py → repo root is two parents up from `lib`.
REPO_ROOT = Path(__file__).resolve().parents[2]


def peek_env(path: Optional[Path | str] = None) -> dict[str, str]:
    """Parse `path` (default repo-root `.env`) and RETURN its key/values WITHOUT
    mutating os.environ. Use this when you only need to check whether a value is
    present (e.g. a test skip-gate) and must not pollute the process environment
    for other tests. Missing file → {}."""
    env_path = Path(path) if path else REPO_ROOT / ".env"
    if not env_path.is_file():
        return {}
    out: dict[str, str] = {}
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            out[key] = value.strip().strip('"').strip("'")
    return out


def load_env(path: Optional[Path | str] = None, *, override: bool = False) -> dict[str, str]:
    """Load `path` (default: repo-root `.env`) into os.environ. Returns the dict
    of keys it set. Missing file is a no-op (returns {})."""
    env_path = Path(path) if path else REPO_ROOT / ".env"
    if not env_path.is_file():
        return {}
    loaded: dict[str, str] = {}
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        if override or key not in os.environ:
            os.environ[key] = value
            loaded[key] = value
    return loaded
