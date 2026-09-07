from __future__ import annotations

import json
import sqlite3
from typing import Iterator

JOB_COLUMNS = [
    "site", "title", "company", "location", "is_remote", "job_type", "date_posted",
    "job_url", "min_amount", "max_amount", "currency", "pay_interval", "is_tech",
    "description",
]

LIST_COLUMNS = """id, site, title, company, location, is_remote, job_type, date_posted,
                  job_url, min_amount, max_amount, currency, pay_interval, first_seen, last_seen"""

SORTS = {
    "date_posted": "date_posted DESC, first_seen DESC",
    "first_seen": "first_seen DESC",
    "title": "title COLLATE NOCASE ASC",
    "company": "company COLLATE NOCASE ASC",
    "salary": "COALESCE(max_amount, min_amount) DESC",
}


class JobRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def upsert(self, country: str, rows: list[dict], seen: str) -> tuple[int, int]:
        """Insert new listings, refresh `last_seen` on ones seen before.

        `first_seen` is never moved backwards by a later run, so the historical
        series stays stable as data accumulates.
        """
        if not rows:
            return 0, 0
        before = self.count(country)
        self.conn.executemany(
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
        self.conn.commit()
        return self.count(country) - before, len(rows)

    def intervals(self, country: str, *, tech_only: bool = True) -> Iterator[tuple]:
        """(date_posted, first_seen, last_seen) for every listing.

        Streamed rather than materialised: at a year's scale this is ~500k rows,
        and the caller only needs to fold them into a per-day counter.
        """
        where = "country=?" + (" AND is_tech=1" if tech_only else "")
        return self.conn.execute(
            f"SELECT date_posted, first_seen, last_seen FROM jobs WHERE {where}", (country,)
        )

    def search(self, country: str, *, tech_only: bool = True, q: str = "", site: str = "",
               remote: str = "", sort: str = "date_posted", limit: int = 200,
               offset: int = 0) -> tuple[int, list[dict]]:
        where, args = ["country=?"], [country]
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

        clause = " AND ".join(where)
        order = SORTS.get(sort, SORTS["date_posted"])
        total = self.conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {clause}", args).fetchone()[0]
        rows = self.conn.execute(
            f"SELECT {LIST_COLUMNS} FROM jobs WHERE {clause} ORDER BY {order} LIMIT ? OFFSET ?",
            [*args, min(limit, 1000), max(offset, 0)],
        ).fetchall()
        return total, [dict(r) for r in rows]

    def count(self, country: str) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM jobs WHERE country=?", (country,)).fetchone()[0]

    def count_by_site(self, country: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT site, COUNT(*) n FROM jobs WHERE country=? AND is_tech=1 "
            "GROUP BY site ORDER BY n DESC", (country,))]

    def count_remote(self, country: str, *, seen_on_or_after: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM jobs WHERE country=? AND is_tech=1 AND is_remote=1"
        args: list = [country]
        if seen_on_or_after:
            sql += " AND last_seen>=?"
            args.append(seen_on_or_after)
        return self.conn.execute(sql, args).fetchone()[0]

    def count_seen_on(self, country: str, day: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE country=? AND is_tech=1 AND last_seen=?",
            (country, day)).fetchone()[0]


class SnapshotRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def record(self, country: str, scrape_date: str, **fields) -> None:
        fields.setdefault("ran_at", "")
        if isinstance(fields.get("by_site"), dict):
            fields["by_site"] = json.dumps(fields["by_site"], separators=(",", ":"))
        cols = ["country", "scrape_date", *fields]
        self.conn.execute(
            f"INSERT OR REPLACE INTO snapshots ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' * len(cols))})",
            [country, scrape_date, *fields.values()],
        )
        self.conn.commit()

    def latest(self, country: str, offset: int = 0) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM snapshots WHERE country=? AND ok=1 "
            "ORDER BY scrape_date DESC LIMIT 1 OFFSET ?", (country, offset)).fetchone()

    def observed_dates(self, country: str) -> list[str]:
        """The days a scrape actually completed. Anything else on the chart is reconstructed."""
        return [r[0] for r in self.conn.execute(
            "SELECT scrape_date FROM snapshots WHERE country=? AND ok=1 ORDER BY scrape_date",
            (country,))]

    def count_ok(self, country: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM snapshots WHERE country=? AND ok=1", (country,)).fetchone()[0]

    def recent(self, country: str, limit: int = 60) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT scrape_date, ran_at, rows_seen, new_jobs, tech_active, active_total, "
            "remote_active, duration_sec, ok, s3_key FROM snapshots "
            "WHERE country=? ORDER BY scrape_date DESC LIMIT ?", (country, limit))]

    def set_s3_key(self, country: str, scrape_date: str, key: str) -> None:
        self.conn.execute("UPDATE snapshots SET s3_key=? WHERE country=? AND scrape_date=?",
                          (key, country, scrape_date))
        self.conn.commit()


class ForecastRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get(self, country: str, metric: str = "tech_active") -> dict | None:
        row = self.conn.execute(
            "SELECT payload, generated_at FROM forecasts WHERE country=? AND metric=?",
            (country, metric)).fetchone()
        if not row:
            return None
        payload = json.loads(row["payload"])
        payload["generated_at"] = row["generated_at"]
        return payload

    def save(self, country: str, metric: str, payload: dict, at: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO forecasts (country, metric, generated_at, payload) "
            "VALUES (?,?,?,?)",
            (country, metric, at, json.dumps(payload, separators=(",", ":"))))
        self.conn.commit()
