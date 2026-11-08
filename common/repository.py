"""Data access, expressed as Peewee queries.

Repositories know tables and columns; they know nothing about what an "active
listing" is or what the API returns. That separation is what lets the service
layer be driven from a stub object rather than a real database.

Every value reaches SQLite as a bound parameter, so user input is never able to
reach the SQL text. `sort` is resolved through a fixed map rather than
interpolated, because an ORDER BY clause cannot be parameterised.
"""

from __future__ import annotations

import json
import struct
from typing import Iterable, Iterator

import peewee as pw

from .models import Forecast, Job, Meta_, Snapshot

# Columns the upsert writes from a scraped row.
JOB_COLUMNS = [
    "site", "title", "company", "location", "is_remote", "job_type", "date_posted",
    "job_url", "min_amount", "max_amount", "currency", "pay_interval", "is_tech",
    "description",
]

LIST_FIELDS = [
    Job.id, Job.site, Job.title, Job.company, Job.location, Job.is_remote,
    Job.job_type, Job.date_posted, Job.job_url, Job.min_amount, Job.max_amount,
    Job.currency, Job.pay_interval, Job.role, Job.first_seen, Job.last_seen,
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
               remote: str = "", role: str = "", sort: str = "date_posted",
               limit: int = 200, offset: int = 0) -> tuple[int, list[dict]]:
        where = Job.country == country
        if tech_only:
            where &= Job.is_tech == 1
        if q:
            like = f"%{q}%"
            where &= (Job.title ** like) | (Job.company ** like) | (Job.location ** like)
        if site:
            where &= Job.site == site
        if role:
            where &= Job.role == role
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

    def unclassified(self, country: str, limit: int) -> list[tuple[str, str, str]]:
        """(country, id, title) for tech listings with no role yet."""
        return list(
            Job.select(Job.country, Job.id, Job.title)
               .where((Job.country == country) & (Job.is_tech == 1)
                      & Job.role.is_null() & Job.title.is_null(False))
               .order_by(Job.first_seen.desc()).limit(limit).tuples()
        )

    def set_roles(self, labelled: list[tuple[str, str, str]]) -> int:
        """Write role labels back. One statement per row, batched in a transaction."""
        if not labelled:
            return 0
        with self.db.atomic():
            for country, jid, role in labelled:
                (Job.update(role=role)
                    .where((Job.country == country) & (Job.id == jid)).execute())
        return len(labelled)

    def count_by_role(self, country: str) -> list[dict]:
        n = pw.fn.COUNT(pw.SQL("*")).alias("n")
        return list(
            Job.select(Job.role, n)
               .where((Job.country == country) & (Job.is_tech == 1) & Job.role.is_null(False))
               .group_by(Job.role).order_by(pw.SQL("n").desc()).dicts()
        )

    def matchable(self, country: str, since: str) -> list[tuple]:
        """(id, title, company, location, role, is_remote) for tech listings seen on or after `since`."""
        return list(
            Job.select(Job.id, Job.title, Job.company, Job.location, Job.role, Job.is_remote)
               .where((Job.country == country) & (Job.is_tech == 1)
                      & (Job.last_seen >= since) & Job.title.is_null(False))
               .tuples()
        )

    def by_ids(self, country: str, ids: list[str], *, since: str | None = None) -> dict[str, dict]:
        """Listing rows keyed by id; ids not found (or seen before `since`) are absent."""
        out: dict[str, dict] = {}
        for chunk in pw.chunked(ids, 400):
            where = (Job.country == country) & Job.id.in_(list(chunk))
            if since:
                where &= Job.last_seen >= since
            for row in Job.select(*LIST_FIELDS).where(where).dicts():
                out[row["id"]] = row
        return out

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
        """The days a scrape actually completed — where the chart begins."""
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

    def clear(self, country: str, metric: str) -> int:
        return Forecast.delete().where(
            (Forecast.country == country) & (Forecast.metric == metric)).execute()


class VectorRepository:
    """Listing embeddings in a sqlite-vec `vec0` virtual table.

    Raw SQL because vec0 is a virtual table Peewee has no model for. Keys are
    "country|id" - jobs has a composite primary key and its implicit rowid is
    not stable across VACUUM, so it cannot be the link. vec0 has no UPSERT, so
    a replacement is a delete followed by an insert.

    Every method needs the extension loaded on the connection (db.connect does
    that whenever sqlite-vec is installed); `available()` says whether it is.
    """

    TABLE = "job_vec"
    _DIM_KEY = "vec_dim"
    _MODEL_KEY = "vec_model"

    def __init__(self, db) -> None:
        self.db = db

    @staticmethod
    def key(country: str, jid: str) -> str:
        return f"{country}|{jid}"

    @staticmethod
    def split(key: str) -> tuple[str, str]:
        country, _, jid = key.partition("|")   # country names never contain "|"
        return country, jid

    @staticmethod
    def pack(vec: Iterable[float]) -> bytes:
        v = list(vec)
        return struct.pack(f"{len(v)}f", *v)

    def available(self) -> bool:
        try:
            self.db.execute_sql("SELECT vec_version()")
            return True
        except Exception:
            return False

    def ready(self) -> bool:
        """The table exists - false until the worker's first embedding pass."""
        return bool(self.db.execute_sql(
            "SELECT 1 FROM sqlite_master WHERE name = ?", (self.TABLE,)).fetchone())

    def ensure(self, dim: int, model: str) -> bool:
        """Create the table, rebuilding it if the model or size changed. True if (re)created."""
        def meta(k):
            row = Meta_.get_or_none(Meta_.k == k)
            return row.v if row else None

        same = meta(self._DIM_KEY) == str(dim) and meta(self._MODEL_KEY) == model
        if same and self.ready():
            return False
        with self.db.atomic():
            self.db.execute_sql(f"DROP TABLE IF EXISTS {self.TABLE}")
            self.db.execute_sql(
                f"CREATE VIRTUAL TABLE {self.TABLE} USING vec0("
                f"job_key TEXT PRIMARY KEY, country TEXT, remote INTEGER, "
                f"embedding float[{int(dim)}] distance_metric=cosine)")
            Meta_.replace(k=self._DIM_KEY, v=str(dim)).execute()
            Meta_.replace(k=self._MODEL_KEY, v=model).execute()
        return True

    def keys(self, country: str) -> set[str]:
        return {r[0] for r in self.db.execute_sql(
            f"SELECT job_key FROM {self.TABLE} WHERE country = ?", (country,)).fetchall()}

    def delete(self, keys: Iterable[str]) -> int:
        n = 0
        with self.db.atomic():
            for k in keys:
                self.db.execute_sql(f"DELETE FROM {self.TABLE} WHERE job_key = ?", (k,))
                n += 1
        return n

    def put(self, rows: Iterable[tuple[str, str, int, list[float]]]) -> int:
        """rows: (key, country, remote, vector)."""
        n = 0
        with self.db.atomic():
            for key, country, remote, vec in rows:
                self.db.execute_sql(f"DELETE FROM {self.TABLE} WHERE job_key = ?", (key,))
                self.db.execute_sql(
                    f"INSERT INTO {self.TABLE}(job_key, country, remote, embedding) VALUES (?, ?, ?, ?)",
                    (key, country, 1 if remote else 0, self.pack(vec)))
                n += 1
        return n

    def nearest(self, country: str, vec: list[float], k: int,
                remote: bool | None = None) -> list[tuple[str, float]]:
        """(job_key, cosine distance) for the k nearest, closest first."""
        sql = (f"SELECT job_key, distance FROM {self.TABLE} "
               f"WHERE embedding MATCH ? AND k = ? AND country = ?")
        params: list = [self.pack(vec), max(1, min(int(k), 4096)), country]
        if remote is not None:
            sql += " AND remote = ?"
            params.append(1 if remote else 0)
        return [(r[0], float(r[1])) for r in
                self.db.execute_sql(sql + " ORDER BY distance", params).fetchall()]
