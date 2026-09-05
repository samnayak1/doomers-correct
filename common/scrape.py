"""Thin wrapper over JobSpy that yields normalised rows, a batch at a time.

Memory discipline matters here: on a 1 GB box we cannot hold every board's
DataFrame at once.  Each (site, term, location) request is converted to plain
dicts and the DataFrame is dropped immediately, so peak RSS is bounded by a
single response rather than by the whole run.
"""

from __future__ import annotations

import re
import time
from datetime import date
from typing import Iterator

from . import config

_TECH = re.compile(config.TECH_TITLE_PATTERN, re.I | re.X)
_NOT_TECH = re.compile(config.TECH_TITLE_EXCLUDE, re.I | re.X)


def is_tech(title: str | None) -> bool:
    t = (title or "").strip()
    if not t:
        return False
    return bool(_TECH.search(t)) and not bool(_NOT_TECH.search(t))


def _clean(v):
    """pandas NA/NaN -> None, numpy scalars -> python scalars."""
    if v is None:
        return None
    try:
        import pandas as pd
        if pd.isna(v):
            return None
    except (TypeError, ValueError, ImportError):
        pass
    if hasattr(v, "item"):
        try:
            return v.item()
        except (ValueError, AttributeError):
            pass
    return v


def _to_rows(df) -> list[dict]:
    rows = []
    for rec in df.to_dict("records"):
        jid = _clean(rec.get("id"))
        url = _clean(rec.get("job_url"))
        if not jid and not url:
            continue
        title = _clean(rec.get("title"))
        desc = _clean(rec.get("description")) if config.KEEP_DESCRIPTIONS else None
        rows.append({
            "id": str(jid or url),
            "site": _clean(rec.get("site")),
            "title": title,
            "company": _clean(rec.get("company")),
            "location": _clean(rec.get("location")),
            "is_remote": 1 if _clean(rec.get("is_remote")) else 0,
            "job_type": _clean(rec.get("job_type")),
            "date_posted": str(_clean(rec.get("date_posted")) or "")[:10] or None,
            "job_url": url,
            "min_amount": _clean(rec.get("min_amount")),
            "max_amount": _clean(rec.get("max_amount")),
            "currency": _clean(rec.get("currency")),
            "pay_interval": _clean(rec.get("interval")),
            "is_tech": 1 if is_tech(title) else 0,
            "description": desc[:4000] if isinstance(desc, str) else None,
        })
    return rows


def plan(country: str, run_day: date | None = None) -> list[tuple[str, str, str]]:
    """The (site, term, location) requests for one run.

    Every run issues exactly the same set of queries. A rotating subset would be
    cheaper, but then the active count would move with the rotation instead of
    with the market rather than with the rotation.
    """
    cfg = config.COUNTRIES[country]
    return [(site, term, loc)
            for site in cfg["sites"]
            for term in config.SEARCH_TERMS
            for loc in cfg["locations"]]


def scrape_country(country: str, run_day: date | None = None, log=print) -> Iterator[tuple[str, list[dict]]]:
    """Yield (site, rows) for each successful request.  Failures are logged, not fatal."""
    from jobspy import scrape_jobs  # imported lazily: pulls in pandas

    cfg = config.COUNTRIES[country]
    requests_ = plan(country, run_day)
    log(f"[scrape] {country}: {len(requests_)} requests planned")

    for i, (site, term, loc) in enumerate(requests_, 1):
        kwargs = dict(
            site_name=[site],
            search_term=term,
            location=loc,
            results_wanted=config.RESULTS_WANTED,
            country_indeed=cfg["indeed"],
            description_format="markdown",
            linkedin_fetch_description=False,
            verbose=0,
            proxies=config.PROXIES,
        )
        if site == "google":
            # Google needs a natural-language query, not a keyword + location pair.
            kwargs["google_search_term"] = f"{term} jobs near {loc} since yesterday"
        if site in {"indeed", "linkedin", "glassdoor"} and config.HOURS_OLD:
            kwargs["hours_old"] = config.HOURS_OLD

        try:
            df = scrape_jobs(**kwargs)
        except Exception as exc:  # a single board being down must not lose the run
            log(f"[scrape]   {i}/{len(requests_)} {site}/{term}/{loc}: FAILED {type(exc).__name__}: {exc}")
            time.sleep(config.SCRAPE_PAUSE_SEC)
            continue

        rows = _to_rows(df) if df is not None and not df.empty else []
        del df
        log(f"[scrape]   {i}/{len(requests_)} {site}/{term}/{loc}: {len(rows)} rows")
        if rows:
            yield site, rows
        time.sleep(config.SCRAPE_PAUSE_SEC)
