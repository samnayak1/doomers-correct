"""Accounts: Cognito Hosted UI login, ID-token verification, signed cookies.

Flow (authorization code + PKCE, exchanged server-side):

    /api/auth/login     -> 302 to Cognito, with state + PKCE verifier kept in a
                           short-lived signed cookie
    /api/auth/callback  -> code exchanged here for tokens, which go into
                           HttpOnly cookies: the ID token (checked on every
                           request) and the refresh token (sent only to
                           /api/auth/refresh)

Page scripts never see a token, so an XSS bug in the predictions list - the
first user-written text this site shows - cannot steal a session.

Verification is local: Cognito's signing keys (JWKS) are fetched once and
cached, so no request waits on AWS after the first.

Also here, because they share SESSION_SECRET: the anonymous voter cookie and
the signed unsubscribe token.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from . import config

ID_COOKIE = "dc_id"
REFRESH_COOKIE = "dc_rt"
OAUTH_COOKIE = "dc_oauth"
VOTER_COOKIE = "dc_voter"

REFRESH_PATH = "/api/auth/refresh"
SESSION_MAX_AGE = 30 * 86400        # how long a browser stays signed in (Cognito's refresh default)
OAUTH_MAX_AGE = 600                 # login round trip
VOTER_MAX_AGE = 365 * 86400


class AuthError(Exception):
    """`code` is what the page acts on: login_required, expired, ..."""

    def __init__(self, code: str, status: int = 401, message: str = "Please log in.") -> None:
        super().__init__(message)
        self.code, self.status = code, status


def enabled() -> bool:
    return all((config.COGNITO_USER_POOL_ID, config.COGNITO_CLIENT_ID,
                config.COGNITO_CLIENT_SECRET, config.COGNITO_DOMAIN, config.SESSION_SECRET))


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── signing ─────────────────────────────────────────────────────────────────
def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _mac(purpose: str, payload: str) -> str:
    # The purpose is mixed in so a value signed for one use (say, a voter id)
    # can never be replayed as another (an unsubscribe token).
    key = config.SESSION_SECRET.encode()
    return _b64(hmac.new(key, f"{purpose}\x00{payload}".encode(), hashlib.sha256).digest()[:18])


def sign(purpose: str, payload: str) -> str:
    return f"{payload}.{_mac(purpose, payload)}"


def unsign(purpose: str, value: str | None) -> str | None:
    if not value or "." not in value or not config.SESSION_SECRET:
        return None
    payload, mac = value.rsplit(".", 1)
    return payload if hmac.compare_digest(mac, _mac(purpose, payload)) else None


# ── Cognito endpoints ───────────────────────────────────────────────────────
def redirect_uri() -> str:
    return f"{config.SITE_URL}/api/auth/callback"


def issuer() -> str:
    return f"https://cognito-idp.{config.COGNITO_REGION}.amazonaws.com/{config.COGNITO_USER_POOL_ID}"


def safe_return(path: str | None) -> str:
    """Only paths on this site, so the login cannot be used as an open redirect."""
    p = (path or "/").strip()
    if not p.startswith("/") or p.startswith("//") or "\\" in p or len(p) > 300:
        return "/"
    return p


def begin_login(return_to: str | None, *, signup: bool = False) -> tuple[str, str]:
    """(URL to send the browser to, value for the OAUTH_COOKIE)."""
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    params = {
        "response_type": "code",
        "client_id": config.COGNITO_CLIENT_ID,
        "redirect_uri": redirect_uri(),
        "scope": "openid email",
        "state": state,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
    }
    page = "signup" if signup else "oauth2/authorize"
    url = f"{config.COGNITO_DOMAIN}/{page}?{urllib.parse.urlencode(params)}"
    blob = _b64(json.dumps({"s": state, "v": verifier, "r": safe_return(return_to),
                            "t": int(time.time())}).encode())
    return url, sign("oauth", blob)


def read_login_cookie(value: str | None, state: str | None) -> tuple[str, str]:
    """(PKCE verifier, return path) if the callback's state matches the cookie."""
    blob = unsign("oauth", value)
    if not blob or not state:
        raise AuthError("bad_state", 400, "That login link expired. Please log in again.")
    try:
        d = json.loads(_unb64(blob))
    except ValueError:
        raise AuthError("bad_state", 400, "That login link expired. Please log in again.")
    if not hmac.compare_digest(str(d.get("s", "")), state) or time.time() - d.get("t", 0) > OAUTH_MAX_AGE:
        raise AuthError("bad_state", 400, "That login link expired. Please log in again.")
    return d["v"], safe_return(d.get("r"))


def _token_request(fields: dict) -> dict:
    basic = base64.b64encode(f"{config.COGNITO_CLIENT_ID}:{config.COGNITO_CLIENT_SECRET}".encode()).decode()
    req = urllib.request.Request(
        f"{config.COGNITO_DOMAIN}/oauth2/token",
        data=urllib.parse.urlencode(fields).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Authorization": f"Basic {basic}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:
        try:
            why = json.load(exc).get("error", "")
        except Exception:
            why = ""
        print(f"[auth] token endpoint refused: HTTP {exc.code} {why}", flush=True)
        # invalid_grant = an expired or revoked refresh token / reused code.
        raise AuthError("login_required", 401, "Your session ended. Please log in again.")
    except Exception as exc:
        print(f"[auth] token endpoint unreachable: {type(exc).__name__}", flush=True)
        raise AuthError("unavailable", 503, "Login is unavailable right now. Please try again.")


def exchange_code(code: str, verifier: str) -> dict:
    """{'id_token', 'refresh_token', ...} for the code the Hosted UI returned."""
    return _token_request({"grant_type": "authorization_code", "client_id": config.COGNITO_CLIENT_ID,
                           "code": code, "redirect_uri": redirect_uri(), "code_verifier": verifier})


def refresh(refresh_token: str) -> str:
    """A fresh ID token. Cognito does not rotate the refresh token by default."""
    return _token_request({"grant_type": "refresh_token", "client_id": config.COGNITO_CLIENT_ID,
                           "refresh_token": refresh_token})["id_token"]


def logout_url() -> str:
    q = urllib.parse.urlencode({"client_id": config.COGNITO_CLIENT_ID, "logout_uri": f"{config.SITE_URL}/"})
    return f"{config.COGNITO_DOMAIN}/logout?{q}"


# ── ID token verification ───────────────────────────────────────────────────
_jwks = None


def _jwks_client():
    global _jwks
    if _jwks is None:
        import jwt
        _jwks = jwt.PyJWKClient(f"{issuer()}/.well-known/jwks.json",
                                cache_keys=True, lifespan=86400, timeout=10)
    return _jwks


def warm() -> None:
    """Fetch the signing keys ahead of the first login, off the request path."""
    if not enabled():
        return
    try:
        _jwks_client().get_jwk_set()
    except Exception as exc:
        print(f"[auth] could not fetch Cognito signing keys yet: {type(exc).__name__}", flush=True)


def verify(id_token: str | None) -> dict:
    """The token's claims, or AuthError. Checks signature, issuer, audience,
    expiry, token_use and that Cognito has verified the email address."""
    import jwt
    if not enabled():
        raise AuthError("disabled", 503, "Accounts are not configured.")
    if not id_token:
        raise AuthError("login_required")
    try:
        key = _jwks_client().get_signing_key_from_jwt(id_token).key
        claims = jwt.decode(id_token, key, algorithms=["RS256"], audience=config.COGNITO_CLIENT_ID,
                            issuer=issuer(), options={"require": ["exp", "iat", "sub"]}, leeway=30)
    except jwt.ExpiredSignatureError:
        raise AuthError("expired", 401, "Your session expired.")
    except jwt.PyJWKClientConnectionError:
        raise AuthError("unavailable", 503, "Login is unavailable right now. Please try again.")
    except jwt.PyJWTError:
        raise AuthError("login_required")
    if claims.get("token_use") != "id":
        raise AuthError("login_required")
    if claims.get("email_verified") not in (True, "true") or not claims.get("email"):
        raise AuthError("unverified", 403, "Please verify your email address first.")
    return claims


def cookie_value(cookie_header: str, name: str) -> str | None:
    """One cookie out of a raw Cookie header (for the ASGI middleware, which
    runs before FastAPI has parsed anything)."""
    for part in cookie_header.split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v
    return None


# ── anonymous voters and unsubscribe links ──────────────────────────────────
def new_voter_cookie() -> str:
    return sign("voter", "v1-" + secrets.token_urlsafe(12))


def voter_id(cookie: str | None) -> str | None:
    return unsign("voter", cookie)


def unsubscribe_token(sub: str) -> str:
    return sign("unsub", sub)


def unsubscribe_sub(token: str | None) -> str | None:
    return unsign("unsub", token)
