#!/usr/bin/env python3
"""Run the whole site on one port without Docker, for local development.

In production nginx serves `web/public` and proxies `/api` to uvicorn; this
collapses both into a single process so you can iterate on the UI quickly.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.staticfiles import StaticFiles  # noqa: E402

from api.main import app  # noqa: E402

app.mount("/", StaticFiles(directory=ROOT / "web" / "public", html=True), name="static")

if __name__ == "__main__":
    import argparse

    import uvicorn

    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--reload", action="store_true")
    a = ap.parse_args()
    print(f"http://{a.host}:{a.port}  (DB: {__import__('common.config', fromlist=['x']).DB_PATH})")
    uvicorn.run("scripts.dev_server:app" if a.reload else app,
                host=a.host, port=a.port, reload=a.reload, log_level="warning")
