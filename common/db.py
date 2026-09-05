"""SQLite storage: the current scrape, every past scrape, and the derived series.

Deliberately stdlib-only so the API container does not need numpy or pandas.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from . import config

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS jobs (
    country      TEXT NOT NULL,
    id           TEXT NOT NULL,
    site         TEXT,
    title        TEXT,
    company      TEXT,
    location     TEXT,
    is_remote    INTEGER,
    job_type     TEXT,
    date_posted  TEXT,
    job_url      TEXT,
    min_amount   REAL,
    max_amount   REAL,
    currency     TEXT,
    pay_interval TEXT,
    is_tech      INTEGER NOT NULL DEFAULT 0,
    description  TEXT,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    seen_count   INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (country, id)
);
CREATE INDEX IF NOT EXISTS ix_jobs_country_tech ON jobs(country, is_tech);
CREATE INDEX IF NOT EXISTS ix_jobs_lastseen     ON jobs(country, last_seen);
CREATE INDEX IF NOT EXISTS ix_jobs_posted       ON jobs(country, date_posted);

-- One row per country per nightly run: the audit trail of what we actually saw.
CREATE TABLE IF NOT EXISTS snapshots (
    country       TEXT NOT NULL,
    scrape_date   TEXT NOT NULL,
    ran_at        TEXT NOT NULL,
    duration_sec  REAL,
    rows_seen     INTEGER,
    new_jobs      INTEGER,
    tech_jobs     INTEGER,
    active_total  INTEGER,
    tech_active   INTEGER,
    remote_active INTEGER,
    by_site       TEXT,
    s3_key        TEXT,
    ok            INTEGER NOT NULL DEFAULT 1,
    note          TEXT,
    PRIMARY KEY (country, scrape_date)
);

CREATE TABLE IF NOT EXISTS forecasts (
    country      TEXT NOT NULL,
    metric       TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    payload      TEXT NOT NULL,
    PRIMARY KEY (country, metric)
);

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""

JOB_COLUMNS = [
    "site", "title", "company", "location", "is_remote", "job_type", "date_posted",
    "job_url", "min_amount", "max_amount", "currency", "pay_interval", "is_tech",
    "description",
]


def connect(path: str | Path, read_only: bool = False) -> sqlite3.Connection:
    path = Path(path)
    if read_only:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=15)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=30)
        conn.executescript(SCHEMA)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #
def upsert_jobs(conn: sqlite3.Connection, country: str, rows: list[dict], seen: str) -> tuple[int, int]:
    """Insert new listings, refresh `last_seen` on ones we have seen before.

    `first_seen` is never moved backwards by a later run, so the historical
    series stays stable as data accumulates.
    """
    if not rows:
        return 0, 0
    before = conn.execute("SELECT COUNT(*) FROM jobs WHERE country=?", (country,)).fetchone()[0]
    conn.executemany(
        f"""
        INSERT INTO jobs (country, id, {", ".join(JOB_COLUMNS)}, first_seen, last_seen, seen_count)
        VALUES (:country, :id, {", ".join(":" + c for c in JOB_COLUMNS)}, :seen, :seen, 1)
        ON CONFLICT(country, id) DO UPDATE SET
            last_seen   = excluded.last_seen,
            seen_count  = jobs.seen_count + 1,
            title       = COALESCE(excluded.title, jobs.title),
            company     = COALESCE(excluded.company, jobs.company),
            location    = COALESCE(excluded.location, jobs.location),
            min_amount  = COALESCE(excluded.min_amount, jobs.min_amount),
            max_amount  = COALESCE(excluded.max_amount, jobs.max_amount),
            date_posted = COALESCE(jobs.date_posted, excluded.date_posted)
        """,
        [{"country": country, "seen": seen, **r} for r in rows],
    )
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM jobs WHERE country=?", (country,)).fetchone()[0]
    return after - before, len(rows)


def record_snapshot(conn: sqlite3.Connection, country: str, scrape_date: str, **fields) -> None:
    fields.setdefault("ran_at", "")
    by_site = fields.get("by_site")
    if isinstance(by_site, dict):
        fields["by_site"] = json.dumps(by_site, separators=(",", ":"))
    cols = ["country", "scrape_date", *fields]
    conn.execute(
        f"INSERT OR REPLACE INTO snapshots ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' * len(cols))})",
        [country, scrape_date, *fields.values()],
    )
    conn.commit()


def save_forecast(conn: sqlite3.Connection, country: str, metric: str, payload: dict, at: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO forecasts (country, metric, generated_at, payload) VALUES (?,?,?,?)",
        (country, metric, at, json.dumps(payload, separators=(",", ":"))),
    )
    conn.commit()


def set_meta(conn: sqlite3.Connection, k: str, v: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (k, str(v)))
    conn.commit()


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
def get_forecast(conn: sqlite3.Connection, country: str, metric: str = "tech_active") -> dict | None:
    row = conn.execute(
        "SELECT payload, generated_at FROM forecasts WHERE country=? AND metric=?", (country, metric)
    ).fetchone()
    if not row:
        return None
    payload = json.loads(row["payload"])
    payload["generated_at"] = row["generated_at"]
    return payload


def observed_dates(conn: sqlite3.Connection, country: str) -> list[str]:
    """The days we actually ran a successful scrape."""
    return [r[0] for r in conn.execute(
        "SELECT scrape_date FROM snapshots WHERE country=? AND ok=1 ORDER BY scrape_date", (country,)
    )]


def daily_series(
    conn: sqlite3.Connection,
    country: str,
    *,
    tech_only: bool = True,
    reconstruct_days: int | None = None,
    today: str | None = None,
) -> dict:
    """Reconstruct active-listing counts per day.

    Each listing contributes an interval:

        start = min(date_posted, first_seen)          # when the listing went live
        start = max(start, first_seen - reconstruct)  # bound the pre-launch tail
        end   = min(last_seen, today)                 # when we last saw it

    The second line matters more than it looks. Boards carry postings with very
    old dates - a listing dated 2024 can still be live today - and without that
    bound one stale row stretches the chart across years nobody observed.

    Counting overlaps per day gives the curve.  Using `date_posted` means the
    chart has real shape from the very first scrape instead of a single dot -
    but days before our first scrape are *survivorship-biased* (we can only see
    listings that were still live when we first looked), so they are reported
    separately as `reconstructed` and are never used to fit the model.
    """
    # Defaults come from config, never from literals here: a caller that omits
    # them must get the same curve the pipeline recorded, not a different one.
    reconstruct_days = config.RECONSTRUCT_DAYS if reconstruct_days is None else reconstruct_days

    today_d = date.fromisoformat(today) if today else date.today()
    where = "country=?" + (" AND is_tech=1" if tech_only else "")

    counts: dict[int, int] = {}
    lo_ord = hi_ord = None
    for posted, first_seen, last_seen in conn.execute(
        f"SELECT date_posted, first_seen, last_seen FROM jobs WHERE {where}", (country,)
    ):
        try:
            fs = date.fromisoformat(first_seen)
        except (TypeError, ValueError):
            continue
        start = fs
        if posted:
            try:
                pd_ = date.fromisoformat(str(posted)[:10])
                if pd_ < start:
                    start = pd_
            except ValueError:
                pass
        # Do not let one stale posting date stretch the chart across years we
        # never observed.
        start = max(start, fs - timedelta(days=reconstruct_days))
        try:
            ls = date.fromisoformat(last_seen)
        except (TypeError, ValueError):
            ls = fs
        end = min(ls, today_d)
        if end < start:
            continue
        a, b = start.toordinal(), end.toordinal()
        # difference array: +1 at start, -1 just past the end
        counts[a] = counts.get(a, 0) + 1
        counts[b + 1] = counts.get(b + 1, 0) - 1
        lo_ord = a if lo_ord is None else min(lo_ord, a)
        hi_ord = b if hi_ord is None else max(hi_ord, b)

    if lo_ord is None:
        return {"dates": [], "values": [], "observed_from": None, "reconstructed_until": None}

    dates, values, running = [], [], 0
    for o in range(lo_ord, hi_ord + 1):
        running += counts.get(o, 0)
        dates.append(date.fromordinal(o).isoformat())
        values.append(running)

    obs = observed_dates(conn, country)
    observed_from = obs[0] if obs else None
    return {
        "dates": dates,
        "values": values,
        "observed_from": observed_from,
        "reconstructed_until": observed_from,
        "observed_days": len(obs),
    }


def fit_window(series: dict, max_days: int | None = -1) -> tuple[list[str], list[float]]:
    """The slice of the series the model is allowed to see.

    Observed days only (never the reconstructed, survivorship-biased tail), and
    optionally just the most recent `max_days` of those.
    """
    obs_from = series.get("observed_from")
    if not obs_from:
        return [], []
    dates, values = series["dates"], series["values"]
    try:
        i = dates.index(obs_from)
    except ValueError:
        return [], []
    if max_days == -1:
        max_days = config.FORECAST_FIT_DAYS
    d, v = dates[i:], [float(x) for x in values[i:]]
    if max_days and len(d) > max_days:
        d, v = d[-max_days:], v[-max_days:]
    return d, v


def list_jobs(conn: sqlite3.Connection, country: str, *, tech_only: bool = True,
              limit: int = 200, offset: int = 0, q: str = "", site: str = "",
              remote: str = "", sort: str = "date_posted") -> dict:
    where = ["country=?"]
    args: list = [country]
    if tech_only:
        where.append("is_tech=1")
    if q:
        where.append("(title LIKE ? OR company LIKE ? OR location LIKE ?)")
        args += [f"%{q}%"] * 3
    if site:
        where.append("site=?")
        args.append(site)
    if remote == "yes":
        where.append("is_remote=1")
    elif remote == "no":
        where.append("(is_remote=0 OR is_remote IS NULL)")

    sort_col = {
        "date_posted": "date_posted DESC, first_seen DESC",
        "first_seen": "first_seen DESC",
        "title": "title COLLATE NOCASE ASC",
        "company": "company COLLATE NOCASE ASC",
        "salary": "COALESCE(max_amount, min_amount) DESC",
    }.get(sort, "date_posted DESC, first_seen DESC")

    clause = " AND ".join(where)
    total = conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {clause}", args).fetchone()[0]
    rows = conn.execute(
        f"""SELECT id, site, title, company, location, is_remote, job_type, date_posted,
                   job_url, min_amount, max_amount, currency, pay_interval, first_seen, last_seen
            FROM jobs WHERE {clause} ORDER BY {sort_col} LIMIT ? OFFSET ?""",
        [*args, min(limit, 1000), max(offset, 0)],
    ).fetchall()
    return {"total": total, "rows": [dict(r) for r in rows]}


def summary(conn: sqlite3.Connection, country: str) -> dict:
    snap = conn.execute(
        "SELECT * FROM snapshots WHERE country=? AND ok=1 ORDER BY scrape_date DESC LIMIT 1", (country,)
    ).fetchone()
    prev = conn.execute(
        "SELECT * FROM snapshots WHERE country=? AND ok=1 ORDER BY scrape_date DESC LIMIT 1 OFFSET 1", (country,)
    ).fetchone()
    sites = [dict(r) for r in conn.execute(
        "SELECT site, COUNT(*) n FROM jobs WHERE country=? AND is_tech=1 GROUP BY site ORDER BY n DESC", (country,)
    )]
    # Before the first run finishes there is no snapshot, but there may already
    # be listings on disk. Reporting 0 next to a chart showing a thousand of them
    # is worse than reporting the live count and saying no scrape has completed.
    live = None
    if not snap:
        series = daily_series(conn, country)
        live = series["values"][-1] if series["values"] else 0

    out = {
        "latest_scrape": snap["scrape_date"] if snap else None,
        "scrape_in_progress": snap is None,
        "tech_active": snap["tech_active"] if snap else live,
        "active_total": snap["active_total"] if snap else live,
        "remote_active": snap["remote_active"] if snap else conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE country=? AND is_tech=1 AND is_remote=1", (country,)
        ).fetchone()[0],
        "new_jobs": snap["new_jobs"] if snap else 0,
        "scrapes": conn.execute("SELECT COUNT(*) FROM snapshots WHERE country=? AND ok=1", (country,)).fetchone()[0],
        "tracked_total": conn.execute("SELECT COUNT(*) FROM jobs WHERE country=?", (country,)).fetchone()[0],
        "by_site": sites,
        "s3_key": snap["s3_key"] if snap else None,
    }
    if snap and prev and prev["tech_active"]:
        out["delta_pct"] = round(100.0 * (snap["tech_active"] - prev["tech_active"]) / prev["tech_active"], 2)
    return out
