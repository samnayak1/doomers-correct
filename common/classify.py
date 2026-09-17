"""Classify job titles into role categories with Gemini.

Each listing is classified once and the answer is stored on the row, so the
nightly cost is a function of how many *new* listings arrived (~640) rather than
how many exist (16k and growing).

Deliberately plain HTTP rather than the google-genai SDK: `requests` is already
present via JobSpy, the endpoint is one POST, and the worker image does not need
another dependency.

Never fatal. A missing key, a timeout, a malformed response - all leave the rows
unclassified for the next run to pick up. A scrape must not fail because an
external API is having a bad day.
"""

from __future__ import annotations

import json
from typing import Iterable

from . import config

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

PROMPT = """You are labelling software job titles by role category.

For each numbered title, return the single category that best fits.
Use `other` when a title genuinely fits none of the categories — including
non-engineering roles, and titles too generic to place (a bare "Software
Engineer" with no further signal is `other`, not a guess at backend or
frontend). Do not invent categories.

Titles:
{titles}"""


def enabled() -> bool:
    return bool(config.GEMINI_API_KEY)


def _schema(n: int) -> dict:
    """An array of {n, role} labels, each role constrained to the category enum.

    The enum is enforced by the API, so a category outside config.ROLE_CATEGORIES
    cannot come back at all.

    No minItems/maxItems: the API rejects the whole request with a bare
    "Request contains an invalid argument" once those exceed a small value -
    a batch of 5 is accepted, 40 is not. They were never load-bearing anyway,
    since every item carries its own index and classify_batch validates it.
    """
    return {
        "type": "ARRAY",
        "items": {
            "type": "OBJECT",
            "properties": {
                "n": {"type": "INTEGER"},
                "role": {"type": "STRING", "enum": list(config.ROLE_CATEGORIES)},
            },
            "required": ["n", "role"],
            "propertyOrdering": ["n", "role"],
        },
    }


def classify_batch(titles: list[str], *, log=print) -> dict[int, str]:
    """Label one batch. Returns {index in `titles`: category}; missing = unlabelled."""
    import requests

    numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(titles))
    body = {
        "contents": [{"parts": [{"text": PROMPT.format(titles=numbered)}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": _schema(len(titles)),
            "temperature": 0,
        },
    }
    try:
        r = requests.post(
            ENDPOINT.format(model=config.GEMINI_MODEL),
            params={"key": config.GEMINI_API_KEY},
            json=body, timeout=config.CLASSIFY_TIMEOUT,
        )
        if r.status_code != 200:
            log(f"[classify] HTTP {r.status_code}: {r.text[:180]}")
            return {}
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        rows = json.loads(text)
    except Exception as exc:
        log(f"[classify] {type(exc).__name__}: {str(exc)[:180]}")
        return {}

    valid = set(config.ROLE_CATEGORIES)
    out: dict[int, str] = {}
    for row in rows:
        try:
            i, role = int(row["n"]), str(row["role"])
        except (KeyError, TypeError, ValueError):
            continue
        # The enum is enforced server-side, but a truncated or reordered response
        # is still possible, so the index and value are both re-checked here.
        if 0 <= i < len(titles) and role in valid:
            out[i] = role
    return out


def classify(items: Iterable[tuple[str, str, str]], *, log=print) -> list[tuple[str, str, str]]:
    """items: (country, id, title). Returns (country, id, role) for what was labelled."""
    items = list(items)
    if not items or not enabled():
        return []

    labelled: list[tuple[str, str, str]] = []
    size = max(1, config.CLASSIFY_BATCH)
    for start in range(0, len(items), size):
        chunk = items[start:start + size]
        got = classify_batch([t for _, _, t in chunk], log=log)
        # Re-check the index here too. classify_batch bounds it already, but this
        # runs unattended at midnight and an IndexError would take the whole run
        # down — a mislabelled row is recoverable, a failed scrape is not.
        labelled += [(chunk[i][0], chunk[i][1], role)
                     for i, role in got.items()
                     if isinstance(i, int) and 0 <= i < len(chunk)
                     and role in set(config.ROLE_CATEGORIES)]
        log(f"[classify] batch {start // size + 1}: {len(got)}/{len(chunk)} labelled")
    return labelled
