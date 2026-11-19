"""Per-account daily quotas, kept in app.db.

Rolling 24 hours rather than calendar days: there is no midnight at which
everyone's allowance resets at once, and no timezone to argue about. Stored,
not in memory, because an in-memory count resets on every deploy - and with
an allowance of 3 a day that is the whole limit.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from .appmodels import Usage, app_database
from .contact import ContactError

WINDOW = timedelta(days=1)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def wait_text(oldest_iso: str, now: datetime) -> str:
    """'in about 5 hours' - when the oldest use in the window drops out of it."""
    free_at = datetime.strptime(oldest_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) + WINDOW
    secs = max(60, (free_at - now).total_seconds())
    if secs < 3600:
        return f"in about {math.ceil(secs / 60)} minutes"
    hours = math.ceil(secs / 3600)
    return f"in about {hours} hour{'s' if hours != 1 else ''}"


def limit_message(limit: int, one: str, many: str, wait: str) -> str:
    allowance = f"your {one}" if limit == 1 else f"all {limit} {many}"
    return f"You have used {allowance} for today. You can try again {wait}."


def take(kind: str, user_sub: str, limit: int, one: str, many: str) -> None:
    """Spend one of `limit` uses in the last 24 hours, or raise ContactError(429).

    Count and insert happen in one IMMEDIATE transaction, so two requests at
    once cannot both take the last use. `one`/`many` name the thing:
    "resume match", "resume matches".
    """
    now = datetime.now(timezone.utc)
    since = _iso(now - WINDOW)
    with app_database.atomic(lock_type="IMMEDIATE"):
        q = Usage.select().where((Usage.kind == kind) & (Usage.user_sub == user_sub) & (Usage.at >= since))
        used = q.count()
        if used >= limit:
            oldest = q.order_by(Usage.at.asc()).first().at
            raise ContactError(429, limit_message(limit, one, many, wait_text(oldest, now)))
        Usage.create(kind=kind, user_sub=user_sub, at=_iso(now))
