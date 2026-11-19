"""Models for app.db: accounts, predictions, votes, reports and the email outbox.

A separate SQLite file from jobs.db, on purpose. The API opens jobs.db
read-only and the worker holds its write lock for minutes during a scrape; a
vote or a new prediction must never queue behind that. Litestream replicates
both files.

All timestamps are UTC ISO-8601 strings ("2026-11-19T08:30:00Z"), so they sort
and compare as text. Dates (`target_date`) are plain YYYY-MM-DD.
"""

from __future__ import annotations

from peewee import (
    BooleanField, CompositeKey, DatabaseProxy, IntegerField, Model, TextField,
    FloatField, AutoField,
)

app_database = DatabaseProxy()


class AppModel(Model):
    class Meta:
        database = app_database


class User(AppModel):
    sub = TextField(primary_key=True)              # Cognito's stable user id
    email = TextField()                            # refreshed from the ID token on each login
    handle = TextField(unique=True)                # public name, e.g. doomer-4821; never the email
    reminders_off = BooleanField(default=False)
    created_at = TextField()
    last_login_at = TextField()

    class Meta:
        table_name = "users"


class Prediction(AppModel):
    id = AutoField()
    user_sub = TextField()
    text = TextField()
    target_date = TextField()
    remind = BooleanField(default=True)
    status = TextField(default="active")           # active | hidden (set by scripts/moderate.py)
    up = IntegerField(default=0)
    down = IntegerField(default=0)
    score = FloatField(default=0.0)                # Wilson lower bound of up/(up+down)
    report_count = IntegerField(default=0)
    created_at = TextField()

    class Meta:
        table_name = "predictions"


class Vote(AppModel):
    prediction_id = IntegerField()
    voter_key = TextField()                        # "u:<sub>" signed in, "v:<id>" anonymous
    value = IntegerField()                         # +1 / -1
    created_at = TextField()

    class Meta:
        table_name = "votes"
        primary_key = CompositeKey("prediction_id", "voter_key")


class Report(AppModel):
    prediction_id = IntegerField()
    reporter_key = TextField()
    reason = TextField(null=True)
    created_at = TextField()

    class Meta:
        table_name = "reports"
        primary_key = CompositeKey("prediction_id", "reporter_key")


class Outbox(AppModel):
    """Transactional outbox: written in the same transaction as the thing that
    needs the email, drained by worker/mailer.py. The table *is* the queue."""
    id = AutoField()
    prediction_id = IntegerField()
    kind = TextField()                             # "reminder"
    due_at = TextField()
    status = TextField(default="pending")          # pending | claimed | sent | failed | cancelled
    attempts = IntegerField(default=0)
    next_attempt_at = TextField()
    claimed_until = TextField(null=True)
    last_error = TextField(null=True)
    ses_message_id = TextField(null=True)
    sent_at = TextField(null=True)

    class Meta:
        table_name = "outbox"
        indexes = ((("prediction_id", "kind"), True),)


ALL_APP_MODELS = [User, Prediction, Vote, Report, Outbox]
