"""Connection management and schema migrations.

Queries live in repository.py and rules live in service.py; this file owns only
how the database is opened and how its shape is kept current.

`CREATE TABLE IF NOT EXISTS` creates tables on a fresh database and silently
does nothing to an existing one, so adding a column would never reach a deployed
instance. MIGRATIONS closes that: each entry runs once, in order, and the
highest applied index is recorded in `meta`.
"""

from __future__ import annotations

from pathlib import Path

from peewee import OperationalError, SqliteDatabase

from .models import ALL_MODELS, Meta_, database

SCHEMA_VERSION_KEY = "schema_version"

# Ordered, append-only. Each is (description, callable taking a migrator+db).
# Never edit or reorder an entry that has shipped - add a new one.
MIGRATIONS: list[tuple[str, object]] = []


def connect(path: str | Path, read_only: bool = False) -> SqliteDatabase:
    """Bind the model proxy to a SQLite file and return the database.

    The API passes read_only=True, which opens a `mode=ro` URI handle: SQLite
    itself then rejects writes, rather than trusting callers not to attempt any.
    """
    path = Path(path)
    if read_only:
        db = SqliteDatabase(f"file:{path}?mode=ro", uri=True,
                            pragmas={"busy_timeout": 15000})
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = SqliteDatabase(str(path), pragmas={
            # WAL is not optional here: it lets the API read while the worker
            # writes, and it is the file Litestream tails to replicate.
            "journal_mode": "wal",
            "busy_timeout": 15000,
            "foreign_keys": 0,
        })
    database.initialize(db)
    db.connect(reuse_if_open=True)
    if not read_only:
        db.create_tables(ALL_MODELS, safe=True)
        _migrate(db)
    return db


def _schema_version(db: SqliteDatabase) -> int:
    try:
        row = Meta_.get_or_none(Meta_.k == SCHEMA_VERSION_KEY)
    except OperationalError:
        return 0
    return int(row.v) if row and str(row.v).isdigit() else 0


def _migrate(db: SqliteDatabase) -> None:
    """Apply any migrations this database has not seen, inside one transaction."""
    from playhouse.migrate import SqliteMigrator

    current = _schema_version(db)
    pending = list(enumerate(MIGRATIONS))[current:]
    if not pending:
        return
    migrator = SqliteMigrator(db)
    with db.atomic():
        for index, (label, fn) in pending:
            fn(migrator, db)
            print(f"[db] migration {index + 1}: {label}", flush=True)
        Meta_.replace(k=SCHEMA_VERSION_KEY, v=str(len(MIGRATIONS))).execute()


def close() -> None:
    if not database.is_closed():
        database.close()
