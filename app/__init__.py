"""LongCat FastAPI backend package."""

from __future__ import annotations

import os
from pathlib import Path


def load_local_env(env_path: str | os.PathLike[str] | None = None) -> None:
    """Load a local .env file without overriding process environment values."""
    path = Path(env_path) if env_path else Path(__file__).resolve().parents[1] / ".env"
    if not path.exists() or not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        lines = path.read_text(encoding="utf-8-sig", errors="ignore").splitlines()
    except OSError:
        return
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


load_local_env()
