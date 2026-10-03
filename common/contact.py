"""Contact form: validate, rate-limit, verify a Cap token, email through SES.

The API stays read-only against the database - messages are not stored, only
delivered. If SES refuses, the visitor is told so and can retry, rather than
the message vanishing into a table nobody reads.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from collections import deque

from . import config

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ContactError(Exception):
    """A refusal the visitor should see. `status` becomes the HTTP code."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def enabled() -> bool:
    return all((config.CAP_SITE_KEY, config.CAP_SECRET,
                config.CONTACT_TO, config.CONTACT_FROM))


def widget_endpoint() -> str | None:
    """The public path the widget talks to. Caddy/nginx forward exactly two
    routes under it - challenge and redeem - and nothing else Cap serves."""
    return f"/cap/{config.CAP_SITE_KEY}/" if enabled() else None


def _flat(text: str) -> str:
    return " ".join(str(text or "").split())


def clean(name: str, email: str, message: str) -> tuple[str, str, str]:
    name, email = _flat(name)[:100], _flat(email)[:254]
    message = str(message or "").strip()
    if not _EMAIL.match(email):
        raise ContactError(400, "Please enter a valid email address so we can reply.")
    if len(message) < 10:
        raise ContactError(400, "The message is too short.")
    if len(message) > 4000:
        raise ContactError(400, "The message is too long (4,000 characters at most).")
    return name, email, message


class RateLimiter:
    """In-process sliding windows. Correct because the API runs one uvicorn
    worker; a second worker would need these counts somewhere shared."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_ip: dict[str, deque[float]] = {}
        self._all: deque[float] = deque()

    @staticmethod
    def _trim(q: deque[float], horizon: float) -> None:
        while q and q[0] < horizon:
            q.popleft()

    def check(self, ip: str) -> None:
        now = time.time()
        with self._lock:
            self._trim(self._all, now - 86400)
            if len(self._all) >= config.CONTACT_PER_DAY:
                raise ContactError(429, "The contact form is busy today. Please try again tomorrow.")
            q = self._by_ip.get(ip)
            if q is not None:
                self._trim(q, now - 3600)
                if len(q) >= config.CONTACT_PER_IP_HOUR:
                    raise ContactError(429, "Too many messages from your connection. Please try again later.")

    def record(self, ip: str) -> None:
        now = time.time()
        with self._lock:
            self._all.append(now)
            self._by_ip.setdefault(ip, deque()).append(now)
            # Drop idle entries so the map cannot grow without bound.
            if len(self._by_ip) > 5000:
                horizon = now - 3600
                for k in [k for k, v in self._by_ip.items() if not v or v[-1] < horizon]:
                    del self._by_ip[k]


limiter = RateLimiter()


def verify(token: str) -> bool:
    """Ask the Cap container whether this token is genuine and unspent.

    Goes straight to cap:3000 on the compose network - siteverify is never
    exposed publicly, since the secret travels with the request.
    """
    if not token or len(token) > 2000:
        return False
    req = urllib.request.Request(
        f"{config.CAP_URL}/{config.CAP_SITE_KEY}/siteverify",
        data=json.dumps({"secret": config.CAP_SECRET, "response": token}).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return bool(json.load(r).get("success"))
    except Exception:
        return False


def send(name: str, email: str, message: str, ip: str) -> None:
    import boto3

    who = name or "(no name given)"
    body = f"From: {who} <{email}>\nIP: {ip}\n\n{message}\n"
    try:
        boto3.client("ses", region_name=config.SES_REGION).send_email(
            Source=config.CONTACT_FROM,
            Destination={"ToAddresses": [config.CONTACT_TO]},
            # Reply-To is the visitor, so hitting reply in your mail client just works.
            ReplyToAddresses=[email],
            Message={
                "Subject": {"Data": f"[aredoomerscorrect] {who}"[:150], "Charset": "UTF-8"},
                "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
            },
        )
    except Exception as exc:
        print(f"[contact] SES send failed: {type(exc).__name__}: {exc}", flush=True)
        raise ContactError(502, "The message could not be sent. Please try again in a few minutes.")


def submit(name: str, email: str, message: str, token: str, ip: str) -> None:
    if not enabled():
        raise ContactError(503, "The contact form is not configured.")
    name, email, message = clean(name, email, message)
    limiter.check(ip)
    if not verify(token):
        raise ContactError(403, "The verification check failed. Please complete it again.")
    send(name, email, message, ip)
    limiter.record(ip)
