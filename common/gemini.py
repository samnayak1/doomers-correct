"""Minimal Gemini client on the standard library, shared by worker and API.

classify.py uses `requests` because the worker has it via JobSpy; the API image
does not, and one POST does not justify adding it. The key goes in a header,
never the URL, so nothing that logs a URL can leak it.
"""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.request

from . import config

BASE = "https://generativelanguage.googleapis.com/v1beta/models"


class GeminiError(Exception):
    pass


def enabled() -> bool:
    return bool(config.GEMINI_API_KEY)


def call(model: str, method: str, body: dict, *, timeout: int) -> dict:
    req = urllib.request.Request(
        f"{BASE}/{model}:{method}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "x-goog-api-key": config.GEMINI_API_KEY},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:
        try:
            why = json.load(exc).get("error", {}).get("message", "")
        except Exception:
            why = ""
        raise GeminiError(f"HTTP {exc.code} {why[:200]}") from None
    except Exception as exc:
        raise GeminiError(f"{type(exc).__name__}: {str(exc)[:200]}") from None


def _unit(v: list[float]) -> list[float]:
    # gemini-embedding-001 only normalises at full size; normalising here keeps
    # cosine distance meaningful whichever model and dimension is configured.
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def embed(texts: list[str], *, task: str) -> list[list[float]]:
    """One vector per text, in order. `task` is RETRIEVAL_DOCUMENT or RETRIEVAL_QUERY."""
    model = config.EMBED_MODEL
    out: list[list[float]] = []
    size = max(1, min(config.EMBED_BATCH, 100))
    for start in range(0, len(texts), size):
        chunk = texts[start:start + size]
        d = call(model, "batchEmbedContents", {"requests": [
            {"model": f"models/{model}", "content": {"parts": [{"text": t}]},
             "taskType": task, "outputDimensionality": config.EMBED_DIM}
            for t in chunk
        ]}, timeout=config.EMBED_TIMEOUT)
        vecs = [e.get("values") or [] for e in d.get("embeddings", [])]
        if len(vecs) != len(chunk) or any(len(v) != config.EMBED_DIM for v in vecs):
            raise GeminiError(f"expected {len(chunk)} x {config.EMBED_DIM} vectors, got "
                              f"{len(vecs)} x {len(vecs[0]) if vecs else 0}")
        out += [_unit(v) for v in vecs]
    return out
