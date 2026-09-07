"""Schema and connection management.

Queries live in repository.py and rules live in service.py; this file owns only
the shape of the database and how to open it. Deliberately stdlib-only, so the
API container needs neither numpy nor pandas.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


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
