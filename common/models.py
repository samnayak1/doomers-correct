"""Peewee models.

Field names and `table_name`s match the schema that raw SQL created before this,
so an existing database is picked up as-is with no conversion step. Changing a
name here without a migration would silently start reading a column that is not
there.

The database is a Proxy: `db.connect()` binds it at runtime from config, which
keeps import-time free of side effects and lets the API bind a read-only handle
to the same models the worker writes through.
"""

from __future__ import annotations

from peewee import (
    BooleanField, CompositeKey, DatabaseProxy, FloatField, IntegerField,
    Model, TextField,
)

database = DatabaseProxy()


class BaseModel(Model):
    class Meta:
        database = database


# NOTE: no `index=True` on any field. Indexes are declared once, in db.SCHEMA
# and in MIGRATIONS, under the ix_jobs_* names. Declaring them here too made
# Peewee create a second, differently-named index over the same column
# (job_role alongside jobs_role), which is pure write amplification at best -
# and the pair on `role` ended up inconsistent, with integrity_check reporting
# "row N missing from index job_role".
class Job(BaseModel):
    country = TextField()
    id = TextField()
    site = TextField(null=True)
    title = TextField(null=True)
    company = TextField(null=True)
    location = TextField(null=True)
    is_remote = BooleanField(null=True)
    job_type = TextField(null=True)
    date_posted = TextField(null=True)
    job_url = TextField(null=True)
    min_amount = FloatField(null=True)
    max_amount = FloatField(null=True)
    currency = TextField(null=True)
    pay_interval = TextField(null=True)
    is_tech = IntegerField(default=0)
    role = TextField(null=True)   # set once by common/classify.py
    description = TextField(null=True)
    first_seen = TextField()
    last_seen = TextField()
    seen_count = IntegerField(default=1)

    class Meta:
        table_name = "jobs"
        primary_key = CompositeKey("country", "id")


class Snapshot(BaseModel):
    """One row per country per nightly run - the audit trail behind the chart."""
    country = TextField()
    scrape_date = TextField()
    ran_at = TextField()
    duration_sec = FloatField(null=True)
    rows_seen = IntegerField(null=True)
    new_jobs = IntegerField(null=True)
    tech_jobs = IntegerField(null=True)
    active_total = IntegerField(null=True)
    tech_active = IntegerField(null=True)
    remote_active = IntegerField(null=True)
    by_site = TextField(null=True)
    s3_key = TextField(null=True)
    ok = IntegerField(default=1)
    note = TextField(null=True)

    class Meta:
        table_name = "snapshots"
        primary_key = CompositeKey("country", "scrape_date")


class Forecast(BaseModel):
    country = TextField()
    metric = TextField()
    generated_at = TextField()
    payload = TextField()

    class Meta:
        table_name = "forecasts"
        primary_key = CompositeKey("country", "metric")


class Meta_(BaseModel):
    """Key/value store, currently only the schema version the migrator reads."""
    k = TextField(primary_key=True)
    v = TextField(null=True)

    class Meta:
        table_name = "meta"


ALL_MODELS = [Job, Snapshot, Forecast, Meta_]
