"""Data access, expressed as Peewee queries.

Repositories know tables and columns; they know nothing about what an "active
listing" is or what the API returns. That rule is what lets the service layer be
tested against a stub instead of a database - see
tests/test_core.py::test_series_service_runs_without_a_database.

Every value reaches SQLite as a bound parameter, so user input is never able to
reach the SQL text. `sort` is resolved through a fixed map rather than
interpolated, because an ORDER BY clause cannot be parameterised.
"""

from __future__ import annotations

import json
from typing import Iterator

import peewee as pw

from .models import Forecast, Job, Snapshot

# Columns the upsert writes from a scraped row.
JOB_COLUMNS = [
    "site", "title", "company", "location", "is_remote", "job_type", "date_posted",
    "job_url", "min_amount", "max_amount", "currency", "pay_interval", "is_tech",
    "description",
]

LIST_FIELDS = [
    Job.id, Job.site, Job.title, Job.company, Job.location, Job.is_remote,
    Job.job_type, Job.date_posted, Job.job_url, Job.min_amount, Job.max_amount,
    Job.currency, Job.pay_interval, Job.first_seen, Job.last_seen,
]

SNAPSHOT_FIELDS = [
    Snapshot.scrape_date, Snapshot.tech_active, Snapshot.active_total,
    Snapshot.remote_active, Snapshot.new_jobs, Snapshot.s3_key,
]

HISTORY_FIELDS = [
    Snapshot.scrape_date, Snapshot.ran_at, Snapshot.rows_seen, Snapshot.new_jobs,
    Snapshot.tech_active, Snapshot.active_total, Snapshot.remote_active,
    Snapshot.duration_sec, Snapshot.ok, Snapshot.s3_key,
]

# An ORDER BY cannot be a bound parameter, so `sort` is a key into this map and
# never reaches SQL as text. An unknown value falls back to the first entry.
SORTS: dict[str, list] = {
    "date_posted": [Job.date_posted.desc(), Job.first_seen.desc()],
    "first_seen": [Job.first_seen.desc()],
    "title": [pw.fn.LOWER(Job.title).asc()],
    "company": [pw.fn.LOWER(Job.company).asc()],
    "salary": [pw.fn.COALESCE(Job.max_amount, Job.min_amount).desc()],
}


class JobRepository:
    def __init__(self, db) -> None:
        self.db = db

    def upsert(self, country: str, rows: list[dict], seen: str) -> tuple[int, int]:
        """Insert new listings, refresh `last_seen` on ones seen before.

        `first_seen` is never moved backwards by a later run, so the historical
        series stays stable as data accumulates.
        """
        if not rows:
            return 0, 0
        before = self.count(country)
        payload = [
            {"country": country, "id": r["id"], "first_seen": seen, "last_seen": seen,
             "seen_count": 1, **{c: r.get(c) for c in JOB_COLUMNS}}
            for r in rows
        ]
        with self.db.atomic():
            # SQLite caps host parameters per statement; chunk to stay under it.
            for chunk in pw.chunked(payload, 200):
                (Job.insert_many(list(chunk))
                    .on_conflict(
                        conflict_target=[Job.country, Job.id],
                        update={
                            Job.last_seen: pw.EXCLUDED.last_seen,
                            Job.seen_count: Job.seen_count + 1,
                            Job.title: pw.fn.COALESCE(pw.EXCLUDED.title, Job.title),
                            Job.company: pw.fn.COALESCE(pw.EXCLUDED.company, Job.company),
                            Job.location: pw.fn.COALESCE(pw.EXCLUDED.location, Job.location),
                            Job.min_amount: pw.fn.COALESCE(pw.EXCLUDED.min_amount, Job.min_amount),
                            Job.max_amount: pw.fn.COALESCE(pw.EXCLUDED.max_amount, Job.max_amount),
                            Job.date_posted: pw.fn.COALESCE(Job.date_posted, pw.EXCLUDED.date_posted),
                        })
                    .execute())
        return self.count(country) - before, len(rows)

    def intervals(self, country: str, *, tech_only: bool = True) -> Iterator[tuple]:
        """(date_posted, first_seen, last_seen) for every listing.

        `.tuples().iterator()` on purpose: this is the hot path - roughly 500k
        rows after a year - and building a model object per row costs about 2x
        what handing back a plain tuple does, for data the caller immediately
        folds into a per-day counter.
        """
        q = Job.select(Job.date_posted, Job.first_seen, Job.last_seen).where(Job.country == country)
        if tech_only:
            q = q.where(Job.is_tech == 1)
        return q.tuples().iterator()

    def search(self, country: str, *, tech_only: bool = True, q: str = "", site: str = "",
               remote: str = "", sort: str = "date_posted", limit: int = 200,
               offset: int = 0) -> tuple[int, list[dict]]:
        where = Job.country == country
        if tech_only:
            where &= Job.is_tech == 1
        if q:
            like = f"%{q}%"
            where &= (Job.title ** like) | (Job.company ** like) | (Job.location ** like)
        if site:
            where &= Job.site == site
        if remote == "yes":
            where &= Job.is_remote == 1
        elif remote == "no":
            where &= (Job.is_remote == 0) | (Job.is_remote.is_null())

        total = Job.select().where(where).count()
        rows = list(
            Job.select(*LIST_FIELDS).where(where)
               .order_by(*SORTS.get(sort, SORTS["date_posted"]))
               .limit(min(limit, 1000)).offset(max(offset, 0))
               .dicts()
        )
        return total, rows

    def count(self, country: str) -> int:
        return Job.select().where(Job.country == country).count()

    def count_by_site(self, country: str) -> list[dict]:
        n = pw.fn.COUNT(pw.SQL("*")).alias("n")
        return list(
            Job.select(Job.site, n)
               .where((Job.country == country) & (Job.is_tech == 1))
               .group_by(Job.site).order_by(pw.SQL("n").desc()).dicts()
        )

    def count_remote(self, country: str, *, seen_on_or_after: str | None = None) -> int:
        where = (Job.country == country) & (Job.is_tech == 1) & (Job.is_remote == 1)
        if seen_on_or_after:
            where &= Job.last_seen >= seen_on_or_after
        return Job.select().where(where).count()

    def count_seen_on(self, country: str, day: str) -> int:
        return Job.select().where(
            (Job.country == country) & (Job.is_tech == 1) & (Job.last_seen == day)).count()


class SnapshotRepository:
    def __init__(self, db) -> None:
        self.db = db

    def record(self, country: str, scrape_date: str, **fields) -> None:
        fields.setdefault("ran_at", "")
        if isinstance(fields.get("by_site"), dict):
            fields["by_site"] = json.dumps(fields["by_site"], separators=(",", ":"))
        Snapshot.replace(country=country, scrape_date=scrape_date, **fields).execute()

    def latest(self, country: str, offset: int = 0):
        return (Snapshot.select(*SNAPSHOT_FIELDS)
                        .where((Snapshot.country == country) & (Snapshot.ok == 1))
                        .order_by(Snapshot.scrape_date.desc())
                        .limit(1).offset(offset).dicts().first())

    def observed_dates(self, country: str) -> list[str]:
        """The days a scrape actually completed. Anything else on the chart is reconstructed."""
        return [r[0] for r in (
            Snapshot.select(Snapshot.scrape_date)
                    .where((Snapshot.country == country) & (Snapshot.ok == 1))
                    .order_by(Snapshot.scrape_date).tuples())]

    def count_ok(self, country: str) -> int:
        return Snapshot.select().where(
            (Snapshot.country == country) & (Snapshot.ok == 1)).count()

    def recent(self, country: str, limit: int = 60) -> list[dict]:
        return list(Snapshot.select(*HISTORY_FIELDS)
                            .where(Snapshot.country == country)
                            .order_by(Snapshot.scrape_date.desc())
                            .limit(limit).dicts())

    def set_s3_key(self, country: str, scrape_date: str, key: str) -> None:
        (Snapshot.update(s3_key=key)
                 .where((Snapshot.country == country) & (Snapshot.scrape_date == scrape_date))
                 .execute())


class ForecastRepository:
    def __init__(self, db) -> None:
        self.db = db

    def get(self, country: str, metric: str = "tech_active") -> dict | None:
        row = (Forecast.select(Forecast.payload, Forecast.generated_at)
                       .where((Forecast.country == country) & (Forecast.metric == metric))
                       .dicts().first())
        if not row:
            return None
        payload = json.loads(row["payload"])
        payload["generated_at"] = row["generated_at"]
        return payload

    def save(self, country: str, metric: str, payload: dict, at: str) -> None:
        Forecast.replace(country=country, metric=metric, generated_at=at,
                         payload=json.dumps(payload, separators=(",", ":"))).execute()
