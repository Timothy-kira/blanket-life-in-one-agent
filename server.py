"""Compatibility launcher for the FastAPI backend."""

from __future__ import annotations

import io
import os
import sys

import uvicorn

from app.main import app, create_app


def main() -> None:
    host = (os.environ.get("LONGCAT_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    try:
        port = int((os.environ.get("LONGCAT_PORT") or "5000").strip())
    except ValueError:
        port = 5000

    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    if hasattr(sys.stderr, "buffer"):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

    display_host = "localhost" if host in {"0.0.0.0", "::"} else host
    print("[LongCat] FastAPI backend started!")
    print(f"[LongCat] Open UI: http://{display_host}:{port}")
    print(f"[LongCat] API prefix: http://{display_host}:{port}/api/v1")
    print(f"[LongCat] Health: http://{display_host}:{port}/api/v1/health")
    print(f"[LongCat] Browser takeover WS: ws://{display_host}:{port}/api/v1/browser/ws")
    print("=" * 50)

    uvicorn.run("app.main:app", host=host, port=port, reload=False, workers=1)


if __name__ == "__main__":
    main()

