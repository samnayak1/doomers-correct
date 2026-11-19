"""Opening app.db and keeping its schema current.

Same idea as db.py's MIGRATIONS, but the applied count lives in SQLite's own
`PRAGMA user_version` rather than a meta table - app.db has nothing else that
needs one.
"""

from __future__ import annotations

from pathlib import Path

from peewee import SqliteDatabase

from .appmodels import ALL_APP_MODELS, app_database

INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_pred_upcoming ON predictions (status, target_date, id)",
    "CREATE INDEX IF NOT EXISTS ix_pred_newest ON predictions (status, created_at, id)",
    "CREATE INDEX IF NOT EXISTS ix_pred_top ON predictions (status, score, id)",
    "CREATE INDEX IF NOT EXISTS ix_pred_user ON predictions (user_sub, created_at)",
    "CREATE INDEX IF NOT EXISTS ix_outbox_due ON outbox (status, next_attempt_at)",
]

# Ordered, append-only, as in db.py. Each takes the database.
APP_MIGRATIONS: list[tuple[str, object]] = []

_bound: SqliteDatabase | None = None


def connect_app(path: str | Path) -> SqliteDatabase:
    """Bind the app model proxy to app.db (read-write), creating it if needed.

    Binding is process-wide, so call this once. Peewee keeps the connection
    itself per thread, which is what the API's threadpool needs.
    """
    global _bound
    if _bound is not None:
        return _bound
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = SqliteDatabase(str(path), pragmas={
        "journal_mode": "wal",        # Litestream tails the WAL
        "busy_timeout": 15000,
        "foreign_keys": 0,
        "synchronous": "normal",      # safe under WAL; fsync per checkpoint, not per commit
    })
    app_database.initialize(db)
    db.connect(reuse_if_open=True)
    with db.atomic():
        db.create_tables(ALL_APP_MODELS, safe=True)
        for sql in INDEXES:
            db.execute_sql(sql)
        _migrate(db)
    db.close()
    _bound = db
    return db


def _migrate(db: SqliteDatabase) -> None:
    current = db.execute_sql("PRAGMA user_version").fetchone()[0]
    for index, (label, fn) in list(enumerate(APP_MIGRATIONS))[current:]:
        fn(db)
        print(f"[appdb] migration {index + 1}: {label}", flush=True)
    if current < len(APP_MIGRATIONS):
        db.execute_sql(f"PRAGMA user_version = {len(APP_MIGRATIONS)}")
