"""Outbox relay: send due reminder emails through SES.

The `outbox` table in app.db is the queue. A row is written in the same
transaction as the prediction it belongs to (common/predictions.create), so
this process only has to drain it:

  1. claim  - due rows flip to `claimed` with a lease (claimed_until). A row
              whose lease ran out - this process died mid-send - is claimable
              again, which is what makes a crash safe.
  2. check  - cancel if the user unsubscribed, the account is gone, or the
              row is more than REMINDER_LATE_CUTOFF_DAYS late (the box was
              down; a surprise email weeks later helps nobody).
  3. send   - SES SendEmail, as a raw MIME message.
  4. record - sent / retry with backoff / failed.

Delivery is at-least-once: dying after SES accepted a message but before the
row says `sent` sends it again after the lease. SES has no idempotency key,
so that rare duplicate is the accepted cost.

Runs as its own container from the worker image, so a long scrape never
delays mail and neither can take the other down.
"""

from __future__ import annotations

import html
import signal
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

sys.path.insert(0, "/app")

from common import auth, config  # noqa: E402
from common.appdb import connect_app  # noqa: E402
from common.appmodels import Outbox, Prediction, User, app_database  # noqa: E402

BACKOFF = [60, 300, 1800, 7200, 43200]        # seconds after attempt 1, 2, 3...
LEASE = timedelta(minutes=5)
PERMANENT = {"MessageRejected", "BadRequestException", "InvalidParameterValue"}

_stop = False


def _handle(signum, _frame):
    global _stop
    _stop = True


def log(msg: str) -> None:
    print(f"{datetime.now().isoformat(timespec='seconds')} [mailer] {msg}", flush=True)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


# ── queue ───────────────────────────────────────────────────────────────────
def claim(now: datetime) -> list[tuple]:
    """Lease up to MAILER_BATCH due rows. One statement, so two relays (should
    one ever run by accident) can never claim the same row."""
    n = iso(now)
    with app_database.atomic(lock_type="IMMEDIATE"):
        cur = app_database.execute_sql(
            """UPDATE outbox SET status = 'claimed', claimed_until = ?
               WHERE id IN (SELECT id FROM outbox
                            WHERE (status = 'pending' AND next_attempt_at <= ?)
                               OR (status = 'claimed' AND claimed_until < ?)
                            ORDER BY next_attempt_at LIMIT ?)
               RETURNING id, prediction_id, kind, due_at, attempts""",
            (iso(now + LEASE), n, n, config.MAILER_BATCH))
        return cur.fetchall()


def finish(row_id: int, **fields) -> None:
    Outbox.update(claimed_until=None, **fields).where(Outbox.id == row_id).execute()


# ── content ─────────────────────────────────────────────────────────────────
def listing_counts(created: str) -> tuple[int | None, int | None]:
    """(tech listings when the prediction was made, tech listings now), from
    jobs.db read-only. Purely decorative: any failure just omits the line."""
    try:
        con = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True, timeout=5)
        try:
            q = ("SELECT tech_active FROM snapshots WHERE country = ? AND ok = 1 AND tech_active IS NOT NULL "
                 "AND scrape_date <= ? ORDER BY scrape_date DESC LIMIT 1")
            then = con.execute(q, (config.DEFAULT_COUNTRY, created[:10])).fetchone()
            now = con.execute(q, (config.DEFAULT_COUNTRY, "9999-12-31")).fetchone()
            return (then[0] if then else None), (now[0] if now else None)
        finally:
            con.close()
    except Exception:
        return None, None


def fmt_day(d: str) -> str:
    return date.fromisoformat(d[:10]).strftime("%-d %B %Y")


def build(p: Prediction, user: User) -> EmailMessage:
    link = f"{config.SITE_URL}/#prediction-{p.id}"
    unsub = f"{config.SITE_URL}/api/unsubscribe?t={auth.unsubscribe_token(user.sub)}"
    then, now = listing_counts(p.created_at)
    votes = f"{p.up} agreed, {p.down} disagreed"
    market = (f"Active tech listings in {config.COUNTRIES[config.DEFAULT_COUNTRY]['prose']}: "
              f"{then:,} when you made it, {now:,} today." if then and now else "")

    text = "\n".join(filter(None, [
        f"On {fmt_day(p.created_at)} you predicted, for {fmt_day(p.target_date)}:",
        "", f"  \"{p.text}\"", "",
        f"Today is the day. Votes so far: {votes}.",
        market, "", f"See it on the site: {link}", "", "--",
        f"You asked to be reminded when you made this prediction. Stop these emails: {unsub}",
    ]))
    e = html.escape
    body = f"""<!doctype html><html><body style="margin:0;background:#ffffff">
<div style="max-width:560px;margin:0 auto;padding:24px 16px;font:15px/1.55 Helvetica,Arial,sans-serif;color:#24292f">
<p style="margin:0 0 12px;color:#57606a">On {e(fmt_day(p.created_at))} you predicted, for {e(fmt_day(p.target_date))}:</p>
<blockquote style="margin:0 0 16px;padding:10px 14px;border-left:3px solid #0a84c2;background:#f4f5f7;font-size:17px;color:#24292f">{e(p.text)}</blockquote>
<p style="margin:0 0 8px"><b>Today is the day.</b> Votes so far: {e(votes)}.</p>
{f'<p style="margin:0 0 8px;color:#57606a">{e(market)}</p>' if market else ''}
<p style="margin:16px 0"><a href="{e(link)}" style="display:inline-block;background:#0a84c2;color:#ffffff;text-decoration:none;font-weight:600;padding:9px 16px;border-radius:4px">See it on the site</a></p>
<p style="margin:24px 0 0;font-size:12px;color:#8b949e">You asked to be reminded when you made this prediction.
<a href="{e(unsub)}" style="color:#8b949e">Stop these emails</a>.</p>
</div></body></html>"""

    msg = EmailMessage()
    msg["From"] = config.SES_FROM
    msg["To"] = user.email
    msg["Subject"] = "Your prediction is due today"
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=config.SES_FROM.rsplit("@", 1)[-1].strip("> ") or None)
    # RFC 8058 one-click: Gmail and Yahoo show an Unsubscribe button and POST here.
    msg["List-Unsubscribe"] = f"<{unsub}>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg.set_content(text)
    msg.add_alternative(body, subtype="html")
    return msg


# ── sending ─────────────────────────────────────────────────────────────────
_ses = None


def ses():
    global _ses
    if _ses is None:
        import boto3
        _ses = boto3.client("sesv2", region_name=config.SES_REGION)
    return _ses


def send(msg: EmailMessage) -> str:
    kw = {"Content": {"Raw": {"Data": msg.as_bytes()}}}
    if config.SES_CONFIGURATION_SET:
        kw["ConfigurationSetName"] = config.SES_CONFIGURATION_SET
    return ses().send_email(**kw)["MessageId"]


def process(row: tuple, now: datetime) -> str:
    row_id, prediction_id, kind, due_at, attempts = row
    p = Prediction.get_or_none(Prediction.id == prediction_id)
    user = User.get_or_none(User.sub == p.user_sub) if p else None
    if kind != "reminder" or not p or not user or not p.remind:
        finish(row_id, status="cancelled", last_error="prediction or account gone")
        return "cancelled"
    if user.reminders_off:
        finish(row_id, status="cancelled", last_error="unsubscribed")
        return "cancelled"
    if now - parse(due_at) > timedelta(days=config.REMINDER_LATE_CUTOFF_DAYS):
        finish(row_id, status="cancelled", last_error="too late to send")
        return "cancelled"

    from botocore.exceptions import BotoCoreError, ClientError
    attempts += 1
    try:
        mid = send(build(p, user))
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "ClientError")
        return retry_or_fail(row_id, attempts, now, code, permanent=code in PERMANENT)
    except (BotoCoreError, OSError) as exc:
        return retry_or_fail(row_id, attempts, now, type(exc).__name__, permanent=False)
    finish(row_id, status="sent", attempts=attempts, ses_message_id=mid, sent_at=iso(now), last_error=None)
    return "sent"


def retry_or_fail(row_id: int, attempts: int, now: datetime, why: str, *, permanent: bool) -> str:
    if permanent or attempts >= config.MAILER_MAX_ATTEMPTS:
        finish(row_id, status="failed", attempts=attempts, last_error=why)
        log(f"outbox {row_id}: failed for good ({why})")
        return "failed"
    wait = BACKOFF[min(attempts - 1, len(BACKOFF) - 1)]
    finish(row_id, status="pending", attempts=attempts, last_error=why,
           next_attempt_at=iso(now + timedelta(seconds=wait)))
    log(f"outbox {row_id}: attempt {attempts} failed ({why}), retrying in {wait}s")
    return "retry"


def tick() -> None:
    now = datetime.now(timezone.utc)
    with app_database.connection_context():
        rows = claim(now)
        counts: dict[str, int] = {}
        for row in rows:
            if _stop:
                # Leave the rest claimed; their lease runs out and the next
                # start picks them up. Never stop half-way through one send.
                break
            result = process(row, now)
            counts[result] = counts.get(result, 0) + 1
    if counts:
        log(", ".join(f"{k} {v}" for k, v in sorted(counts.items())))


def main() -> None:
    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    connect_app(config.APP_DB_PATH)
    if not config.SES_FROM:
        log("SES_FROM is not set - reminders stay queued until it is")
    else:
        log(f"started; sending as {config.SES_FROM} via {config.SES_REGION}, polling every {config.MAILER_POLL_SEC}s")
    while not _stop:
        if config.SES_FROM:
            try:
                tick()
            except Exception as exc:   # a bad tick must not kill the relay
                log(f"tick failed: {type(exc).__name__}: {exc}")
        for _ in range(config.MAILER_POLL_SEC):
            if _stop:
                break
            time.sleep(1)
    log("stopped")


if __name__ == "__main__":
    main()
