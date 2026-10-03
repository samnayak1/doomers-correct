"""Contact form: validate, rate-limit, verify a Cap token, deliver via Telegram.

The API stays read-only against the database - messages are not stored, only
delivered. If Telegram refuses, the visitor is told so and can retry, rather
than the message vanishing into a table nobody reads.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
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
                config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID))


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
    """Post the message to your chat as your bot.

    Plain text on purpose: no parse_mode means nothing a visitor types can be
    read as Markdown or HTML formatting. Link previews are off so Telegram does
    not go and fetch whatever URL a visitor pastes.
    """
    head = f"New message from aredoomerscorrect\nFrom: {name or '(no name given)'} <{email}>\nIP: {ip}\n\n"
    text = head + message[: 4096 - len(head)]   # Telegram's per-message limit
    req = urllib.request.Request(
        f"{config.TELEGRAM_API}/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
        data=json.dumps({"chat_id": config.TELEGRAM_CHAT_ID, "text": text,
                         "disable_web_page_preview": True}).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            ok = json.load(r).get("ok")
    except urllib.error.HTTPError as exc:
        # Telegram explains itself in the body ("chat not found", "Unauthorized").
        # Log that, never the URL - the bot token is part of it.
        try:
            why = json.load(exc).get("description", "")
        except Exception:
            why = ""
        print(f"[contact] Telegram refused: HTTP {exc.code} {why}", flush=True)
        ok = False
    except Exception as exc:
        print(f"[contact] Telegram unreachable: {type(exc).__name__}", flush=True)
        ok = False
    if not ok:
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
