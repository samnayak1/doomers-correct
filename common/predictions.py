"""Predictions: create, list, vote, report, moderate.

Rules that keep the feature honest:
  * A prediction can never be edited or deleted by its author - otherwise it
    could be quietly rewritten after the fact. Only moderation hides one.
  * Creating one and queueing its reminder email happen in ONE transaction
    (the transactional outbox): there is never a prediction whose reminder
    was lost, or a reminder for a prediction that failed to save.
  * Vote counts are kept on the prediction row and changed in the same
    transaction as the vote, so listing never has to COUNT(*).
"""

from __future__ import annotations

import math
import re
import secrets
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

from peewee import IntegrityError

from . import config, contact, quotas
from .appmodels import Outbox, Prediction, Report, User, Vote, app_database

IST = ZoneInfo(config.REMINDER_TZ)
PAGE = 20
SORTS = ("upcoming", "due", "newest", "top")

_URL = re.compile(r"(https?://|www\.|\b[a-z0-9-]+\.(com|net|org|io|in|co|xyz|ly|me|app|dev|ai)\b)", re.I)
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f​-‏‪-‮⁦-⁩]")


class PredictionError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


report_limiter = contact.RateLimiter(
    config.REPORTS_PER_IP_HOUR, 500,
    busy="Reporting is busy right now. Please try again later.",
    too_many="Too many reports from your connection. Please try again later.",
    error=PredictionError,
)
voter_limiter = contact.RateLimiter(
    config.VOTER_ISSUE_PER_IP_DAY, 20000,
    busy="Voting is busy right now. Please try again later.",
    too_many="Too many new voters from your connection today.",
    window=86400, error=PredictionError,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_ist() -> date:
    return datetime.now(IST).date()


def date_bounds() -> tuple[date, date]:
    """(earliest, latest) allowed target date."""
    return today_ist() + timedelta(days=config.PREDICTION_MIN_DAYS), date.fromisoformat(config.PREDICTION_MAX_DATE)


def reminder_due(target: date) -> datetime:
    """REMINDER_TIME IST on the target date, as an aware UTC datetime."""
    hh, mm = (int(x) for x in config.REMINDER_TIME.split(":", 1))
    return datetime.combine(target, dtime(hh, mm), IST).astimezone(timezone.utc)


def wilson(up: int, down: int, z: float = 1.96) -> float:
    """Lower bound of the 95% interval on the upvote share. Ranks 90-10 above
    2-0, which up-minus-down and plain ratios both get wrong."""
    n = up + down
    if n == 0:
        return 0.0
    p = up / n
    return (p + z * z / (2 * n) - z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)) / (1 + z * z / n)


# ── accounts ────────────────────────────────────────────────────────────────
def upsert_user(claims: dict) -> User:
    """The user row for these ID-token claims, created on first sight."""
    sub, email, now = claims["sub"], claims["email"], iso(utcnow())
    u = User.get_or_none(User.sub == sub)
    if u:
        if u.email != email or not u.last_login_at.startswith(now[:10]):
            User.update(email=email, last_login_at=now).where(User.sub == sub).execute()
            u.email = email
        return u
    for _ in range(20):
        handle = f"doomer-{secrets.randbelow(900000) + 100000}"
        try:
            with app_database.atomic():
                return User.create(sub=sub, email=email, handle=handle,
                                   created_at=now, last_login_at=now)
        except IntegrityError:
            existing = User.get_or_none(User.sub == sub)   # a parallel first request won
            if existing:
                return existing
    raise PredictionError(503, "Could not create your account. Please try again.")


# ── create ──────────────────────────────────────────────────────────────────
def clean_text(text: str) -> str:
    t = " ".join(_CTRL.sub("", str(text or "")).split())
    if len(t) < 10:
        raise PredictionError(400, "Write a little more - at least 10 characters.")
    if len(t) > config.PREDICTION_MAX_CHARS:
        raise PredictionError(400, f"Keep it to {config.PREDICTION_MAX_CHARS} characters.")
    if _URL.search(t):
        raise PredictionError(400, "Links are not allowed in predictions.")
    return t


def create(user: User, text: str, target_date: str, remind: bool) -> dict:
    text = clean_text(text)
    try:
        target = date.fromisoformat(str(target_date))
    except ValueError:
        raise PredictionError(400, "Pick a date for your prediction.")
    lo, hi = date_bounds()
    if target < lo:
        raise PredictionError(400, f"Pick a date at least {config.PREDICTION_MIN_DAYS} days away.")
    if target > hi:
        raise PredictionError(400, f"Pick a date no later than {hi.isoformat()}.")

    now = utcnow()
    with app_database.atomic(lock_type="IMMEDIATE"):
        recent = (Prediction.select(Prediction.created_at)
                  .where((Prediction.user_sub == user.sub)
                         & (Prediction.created_at >= iso(now - timedelta(days=1))))
                  .order_by(Prediction.created_at.asc()))
        if recent.count() >= config.PREDICTIONS_PER_USER_DAY:
            raise PredictionError(429, quotas.limit_message(
                config.PREDICTIONS_PER_USER_DAY, "prediction", "predictions",
                quotas.wait_text(recent.first().created_at, now)))
        p = Prediction.create(user_sub=user.sub, text=text, target_date=target.isoformat(),
                              remind=bool(remind), created_at=iso(now))
        if remind:
            due = iso(reminder_due(target))
            Outbox.create(prediction_id=p.id, kind="reminder", due_at=due, next_attempt_at=due)
    return _row(p, user.handle, my_vote=0, mine=True)


# ── list ────────────────────────────────────────────────────────────────────
def _row(p, handle: str, *, my_vote: int, mine: bool) -> dict:
    return {
        "id": p.id, "handle": handle, "text": p.text, "target_date": p.target_date,
        "created": p.created_at[:10], "up": p.up, "down": p.down,
        "my_vote": my_vote, "mine": mine, "due": p.target_date <= today_ist().isoformat(),
        **({"hidden": True} if p.status != "active" else {}),
    }


def _cursor(sort: str, p) -> str:
    key = {"upcoming": p.target_date, "due": p.target_date, "newest": p.created_at,
           "top": repr(float(p.score))}[sort]
    return f"{key}|{p.id}"


def list_page(sort: str, cursor: str | None, *, voter_key: str | None, viewer_sub: str | None) -> dict:
    sort = sort if sort in SORTS else "upcoming"
    today = today_ist().isoformat()
    q = (Prediction.select(Prediction, User.handle)
         .join(User, on=(User.sub == Prediction.user_sub), attr="author")
         .where(Prediction.status == "active"))
    col, asc = {
        "upcoming": (Prediction.target_date, True),
        "due": (Prediction.target_date, False),
        "newest": (Prediction.created_at, False),
        "top": (Prediction.score, False),
    }[sort]
    if sort == "upcoming":
        q = q.where(Prediction.target_date > today)
    elif sort == "due":
        q = q.where(Prediction.target_date <= today)

    if cursor and "|" in cursor:
        raw, _, sid = cursor.rpartition("|")
        try:
            cid = int(sid)
            cval = float(raw) if sort == "top" else raw
        except ValueError:
            raise PredictionError(400, "Bad cursor.")
        # Keyset pagination: (col, id) strictly after the last row shown.
        q = q.where((col > cval) | ((col == cval) & (Prediction.id > cid))) if asc else \
            q.where((col < cval) | ((col == cval) & (Prediction.id < cid)))
    q = q.order_by(col.asc() if asc else col.desc(),
                   Prediction.id.asc() if asc else Prediction.id.desc()).limit(PAGE + 1)

    rows = list(q)
    more = len(rows) > PAGE
    rows = rows[:PAGE]
    mine = _my_votes([p.id for p in rows], voter_key)
    return {
        "sort": sort,
        "items": [_row(p, p.author.handle, my_vote=mine.get(p.id, 0), mine=p.user_sub == viewer_sub)
                  for p in rows],
        "next": _cursor(sort, rows[-1]) if more and rows else None,
    }


def get_one(prediction_id: int, *, voter_key: str | None, viewer_sub: str | None) -> dict:
    """One prediction, for the link in its reminder email. Hidden ones are
    visible only to their author."""
    p = (Prediction.select(Prediction, User.handle)
         .join(User, on=(User.sub == Prediction.user_sub), attr="author")
         .where(Prediction.id == prediction_id).first())
    if not p or (p.status != "active" and p.user_sub != viewer_sub):
        raise PredictionError(404, "That prediction is not available.")
    return _row(p, p.author.handle, my_vote=_my_votes([p.id], voter_key).get(p.id, 0),
                mine=p.user_sub == viewer_sub)


def list_mine(user: User) -> list[dict]:
    rows = (Prediction.select().where(Prediction.user_sub == user.sub)
            .order_by(Prediction.created_at.desc()).limit(200))
    return [_row(p, user.handle, my_vote=0, mine=True) for p in rows]


def _my_votes(ids: list[int], voter_key: str | None) -> dict[int, int]:
    if not ids or not voter_key:
        return {}
    return {v.prediction_id: v.value for v in
            Vote.select().where((Vote.voter_key == voter_key) & (Vote.prediction_id.in_(ids)))}


# ── vote ────────────────────────────────────────────────────────────────────
def vote(prediction_id: int, voter_key: str, value: int, *, voter_sub: str | None) -> dict:
    if value not in (-1, 0, 1):
        raise PredictionError(400, "A vote is up, down or none.")
    with app_database.atomic(lock_type="IMMEDIATE"):
        p = Prediction.get_or_none((Prediction.id == prediction_id) & (Prediction.status == "active"))
        if not p:
            raise PredictionError(404, "That prediction is not available.")
        if voter_sub and p.user_sub == voter_sub:
            raise PredictionError(403, "You cannot vote on your own prediction.")
        old = Vote.get_or_none((Vote.prediction_id == p.id) & (Vote.voter_key == voter_key))
        before = old.value if old else 0
        if value == before:
            return {"id": p.id, "up": p.up, "down": p.down, "my_vote": value}
        if value == 0:
            Vote.delete().where((Vote.prediction_id == p.id) & (Vote.voter_key == voter_key)).execute()
        else:
            (Vote.insert(prediction_id=p.id, voter_key=voter_key, value=value, created_at=iso(utcnow()))
             .on_conflict(conflict_target=[Vote.prediction_id, Vote.voter_key],
                          update={Vote.value: value}).execute())
        up = p.up + (value == 1) - (before == 1)
        down = p.down + (value == -1) - (before == -1)
        Prediction.update(up=up, down=down, score=wilson(up, down)).where(Prediction.id == p.id).execute()
    return {"id": p.id, "up": up, "down": down, "my_vote": value}


# ── report ──────────────────────────────────────────────────────────────────
def report(prediction_id: int, reporter_key: str, reason: str, ip: str) -> None:
    reason = " ".join(_CTRL.sub("", str(reason or "")).split())[:200]
    report_limiter.check(ip)
    with app_database.atomic(lock_type="IMMEDIATE"):
        p = Prediction.get_or_none((Prediction.id == prediction_id) & (Prediction.status == "active"))
        if not p:
            raise PredictionError(404, "That prediction is not available.")
        try:
            Report.insert(prediction_id=p.id, reporter_key=reporter_key, reason=reason or None,
                          created_at=iso(utcnow())).execute()
        except IntegrityError:
            return   # already reported by this person: nothing more to tell anyone
        Prediction.update(report_count=Prediction.report_count + 1).where(Prediction.id == p.id).execute()
        count = p.report_count + 1
    report_limiter.record(ip)
    # Best effort: the report is stored either way, and `moderate.py reports`
    # lists everything Telegram may have missed.
    contact.notify(
        f"Prediction #{p.id} reported ({count} total)\n"
        f"Reason: {reason or '(none given)'}\n\n\"{p.text}\"\n\n"
        f"Hide it:  docker compose exec worker python /app/scripts/moderate.py hide {p.id}"
    )


# ── moderation (scripts/moderate.py) ────────────────────────────────────────
def set_status(prediction_id: int, status: str) -> bool:
    """Hide or restore one. The author's reminder is left as it is."""
    assert status in ("active", "hidden")
    return Prediction.update(status=status).where(Prediction.id == prediction_id).execute() > 0


def most_reported(limit: int = 30) -> list:
    return list(Prediction.select().where(Prediction.report_count > 0)
                .order_by(Prediction.report_count.desc(), Prediction.id.desc()).limit(limit))
