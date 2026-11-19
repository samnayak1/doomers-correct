"""Routes for accounts, predictions, votes, reports and unsubscribing.

Kept apart from main.py because these are the only routes that write - to
app.db, never to jobs.db - and the only ones that read cookies.

Writes are protected from cross-site requests three ways: the cookies are
SameSite=Lax (not sent on a cross-site POST), FastAPI only parses a JSON body
sent as application/json (which a plain HTML form cannot send), and
`same_origin` refuses a browser request whose Origin is another site.
"""

from __future__ import annotations

import html
import threading
from contextlib import contextmanager

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from common import appdb, auth, config, contact, predictions
from common.appmodels import User
from common.auth import AuthError
from common.predictions import PredictionError

router = APIRouter()
NO_STORE = {"Cache-Control": "no-store"}


# ── plumbing ────────────────────────────────────────────────────────────────
_ready = threading.Lock()


@contextmanager
def app_session():
    """A connection to app.db for this request (Peewee keeps one per thread)."""
    with _ready:   # first call creates the schema; never twice at once
        try:
            db = appdb.connect_app(config.APP_DB_PATH)
        except Exception as exc:
            print(f"[accounts] app.db unavailable: {type(exc).__name__}: {exc}", flush=True)
            raise HTTPException(503, "Accounts are unavailable right now.")
    with db.connection_context():
        yield


def same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != config.SITE_URL:
        raise HTTPException(403, "Cross-site request refused.")


def client_ip(request: Request) -> str:
    # Same rule as main.client_ip: the proxy appends the real client last.
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[-1].strip() if fwd else (request.client.host if request.client else "?")


def _cookie(resp, name: str, value: str, max_age: int, path: str = "/") -> None:
    resp.set_cookie(name, value, max_age=max_age, path=path, httponly=True,
                    secure=config.COOKIE_SECURE, samesite="lax")


def _clear_session(resp) -> None:
    resp.delete_cookie(auth.ID_COOKIE, path="/", secure=config.COOKIE_SECURE, samesite="lax", httponly=True)
    resp.delete_cookie(auth.REFRESH_COOKIE, path=auth.REFRESH_PATH, secure=config.COOKIE_SECURE,
                       samesite="lax", httponly=True)


def require_user(request: Request) -> User:
    """The signed-in user, or AuthError. Call inside app_session()."""
    claims = auth.verify(request.cookies.get(auth.ID_COOKIE))
    return predictions.upsert_user(claims)


def optional_user(request: Request) -> User | None:
    """The signed-in user if there is a valid session. An EXPIRED one still
    raises, so the page refreshes it instead of silently acting anonymously."""
    if not auth.enabled() or not request.cookies.get(auth.ID_COOKIE):
        return None
    try:
        return require_user(request)
    except AuthError as exc:
        if exc.code == "expired":
            raise
        return None


def actor_key(request: Request) -> tuple[str, User | None]:
    """Who is voting or reporting: an account if signed in, else the voter cookie."""
    user = optional_user(request)
    if user:
        return f"u:{user.sub}", user
    vid = auth.voter_id(request.cookies.get(auth.VOTER_COOKIE))
    if vid:
        return f"v:{vid}", None
    raise AuthError("voter_required", 401, "Please verify you are human first.")


def auth_error_response(exc: AuthError) -> JSONResponse:
    return JSONResponse({"detail": str(exc), "code": exc.code}, status_code=exc.status, headers=NO_STORE)


def prediction_rules() -> dict:
    lo, hi = predictions.date_bounds()
    return {"min_date": lo.isoformat(), "max_date": hi.isoformat(),
            "max_chars": config.PREDICTION_MAX_CHARS, "reminder_time": config.REMINDER_TIME,
            "per_day": config.PREDICTIONS_PER_USER_DAY,
            "captcha": contact.widget_endpoint_any()}


# ── login ───────────────────────────────────────────────────────────────────
@router.get("/api/auth/login")
def login(return_to: str = Query("/", max_length=300), signup: bool = False):
    if not auth.enabled():
        raise HTTPException(503, "Accounts are not configured.")
    url, cookie = auth.begin_login(return_to, signup=signup)
    resp = RedirectResponse(url, status_code=302, headers=NO_STORE)
    _cookie(resp, auth.OAUTH_COOKIE, cookie, auth.OAUTH_MAX_AGE, path="/api/auth")
    return resp


@router.get("/api/auth/callback")
def callback(request: Request, code: str = Query("", max_length=2000),
             state: str = Query("", max_length=200), error: str = Query("", max_length=200)):
    def failed(why: str):
        print(f"[accounts] login failed: {why}", flush=True)
        r = RedirectResponse("/?login=failed", status_code=302, headers=NO_STORE)
        r.delete_cookie(auth.OAUTH_COOKIE, path="/api/auth")
        return r

    if error or not code:
        return failed(error or "no code")
    try:
        verifier, return_to = auth.read_login_cookie(request.cookies.get(auth.OAUTH_COOKIE), state)
        tokens = auth.exchange_code(code, verifier)
        claims = auth.verify(tokens["id_token"])
        with app_session():
            predictions.upsert_user(claims)
    except (AuthError, KeyError) as exc:
        return failed(getattr(exc, "code", "no id_token"))
    resp = RedirectResponse(return_to, status_code=302, headers=NO_STORE)
    resp.delete_cookie(auth.OAUTH_COOKIE, path="/api/auth")
    # The ID token itself expires in an hour; the cookie lives as long as the
    # refresh token, so an expired session reads as "expired" (refresh it)
    # rather than as "never logged in".
    _cookie(resp, auth.ID_COOKIE, tokens["id_token"], auth.SESSION_MAX_AGE)
    if tokens.get("refresh_token"):
        _cookie(resp, auth.REFRESH_COOKIE, tokens["refresh_token"], auth.SESSION_MAX_AGE, path=auth.REFRESH_PATH)
    return resp


@router.post("/api/auth/refresh", dependencies=[Depends(same_origin)])
def refresh_session(request: Request):
    rt = request.cookies.get(auth.REFRESH_COOKIE)
    try:
        if not rt:
            raise AuthError("login_required")
        id_token = auth.refresh(rt)
        auth.verify(id_token)
    except AuthError as exc:
        resp = auth_error_response(exc)
        if exc.code != "unavailable":
            _clear_session(resp)
        return resp
    resp = JSONResponse({"ok": True}, headers=NO_STORE)
    _cookie(resp, auth.ID_COOKIE, id_token, auth.SESSION_MAX_AGE)
    return resp


@router.post("/api/auth/logout", dependencies=[Depends(same_origin)])
def logout():
    resp = JSONResponse({"logout_url": auth.logout_url() if auth.enabled() else "/"}, headers=NO_STORE)
    _clear_session(resp)
    return resp


@router.get("/api/me")
def me(request: Request):
    out = {"enabled": auth.enabled(), "logged_in": False}
    if not auth.enabled():
        return JSONResponse(out, headers=NO_STORE)
    try:
        with app_session():
            user = require_user(request)
        out.update(logged_in=True, handle=user.handle, reminders_off=bool(user.reminders_off))
    except AuthError as exc:
        out["expired"] = exc.code == "expired"
    return JSONResponse(out, headers=NO_STORE)


class RemindersIn(BaseModel):
    on: bool


@router.post("/api/me/reminders", dependencies=[Depends(same_origin)])
def set_reminders(body: RemindersIn, request: Request):
    with app_session():
        user = require_user(request)
        User.update(reminders_off=not body.on).where(User.sub == user.sub).execute()
    return JSONResponse({"reminders_off": not body.on}, headers=NO_STORE)


# ── predictions ─────────────────────────────────────────────────────────────
def viewer(request: Request) -> tuple[User | None, str | None]:
    """(user, voter key) for read-only views: never refuses, so an expired
    session still gets the public list."""
    try:
        user = optional_user(request)
    except AuthError:
        user = None
    if user:
        return user, f"u:{user.sub}"
    vid = auth.voter_id(request.cookies.get(auth.VOTER_COOKIE))
    return None, f"v:{vid}" if vid else None


@router.get("/api/predictions")
def list_predictions(request: Request, sort: str = Query("upcoming", max_length=12),
                     cursor: str = Query("", max_length=80)):
    with app_session():
        user, key = viewer(request)
        out = predictions.list_page(sort, cursor or None, voter_key=key,
                                    viewer_sub=user.sub if user else None)
    out["rules"] = prediction_rules()
    # Personal (my_vote, mine), so never cached by the browser or a proxy.
    return JSONResponse(out, headers={"Cache-Control": "private, no-store"})


@router.get("/api/predictions/mine")
def my_predictions(request: Request):
    with app_session():
        user = require_user(request)
        items = predictions.list_mine(user)
    return JSONResponse({"items": items}, headers=NO_STORE)


@router.get("/api/predictions/{pid}")
def one_prediction(pid: int, request: Request):
    with app_session():
        user, key = viewer(request)
        row = predictions.get_one(pid, voter_key=key, viewer_sub=user.sub if user else None)
    return JSONResponse(row, headers={"Cache-Control": "private, no-store"})


class PredictionIn(BaseModel):
    text: str = Field(..., max_length=2000)
    target_date: str = Field(..., max_length=10)
    remind: bool = True


@router.post("/api/predictions", dependencies=[Depends(same_origin)])
def create_prediction(body: PredictionIn, request: Request):
    with app_session():
        user = require_user(request)
        row = predictions.create(user, body.text, body.target_date, body.remind)
    return JSONResponse(row, status_code=201, headers=NO_STORE)


class VoterIn(BaseModel):
    token: str = Field(..., max_length=2000)


@router.post("/api/voter", dependencies=[Depends(same_origin)])
def issue_voter(body: VoterIn, request: Request):
    """Mint an anonymous voter identity - one per browser - for one Cap solve."""
    if not config.SESSION_SECRET or not contact.captcha_enabled():
        raise HTTPException(503, "Voting is not configured.")
    if auth.voter_id(request.cookies.get(auth.VOTER_COOKIE)):
        return JSONResponse({"ok": True}, headers=NO_STORE)   # already has one
    ip = client_ip(request)
    predictions.voter_limiter.check(ip)
    if not contact.verify(body.token):
        raise HTTPException(403, "The verification check failed. Please try again.")
    predictions.voter_limiter.record(ip)
    resp = JSONResponse({"ok": True}, headers=NO_STORE)
    _cookie(resp, auth.VOTER_COOKIE, auth.new_voter_cookie(), auth.VOTER_MAX_AGE)
    return resp


class VoteIn(BaseModel):
    value: int = Field(..., ge=-1, le=1)


@router.put("/api/predictions/{pid}/vote", dependencies=[Depends(same_origin)])
def vote(pid: int, body: VoteIn, request: Request):
    with app_session():
        key, user = actor_key(request)
        out = predictions.vote(pid, key, body.value, voter_sub=user.sub if user else None)
    return JSONResponse(out, headers=NO_STORE)


class ReportIn(BaseModel):
    reason: str = Field("", max_length=500)


@router.post("/api/predictions/{pid}/report", dependencies=[Depends(same_origin)])
def report(pid: int, body: ReportIn, request: Request):
    with app_session():
        key, _ = actor_key(request)
        predictions.report(pid, key, body.reason, client_ip(request))
    return JSONResponse({"ok": True}, headers=NO_STORE)


# ── unsubscribe (linked from every reminder email) ──────────────────────────
def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>
<style>body{{font:15px/1.55 "Helvetica Neue",Helvetica,Arial,sans-serif;color:#24292f;background:#fff;
max-width:520px;margin:12vh auto;padding:0 16px}}h1{{font-size:20px;font-weight:600}}
button{{font:inherit;font-weight:600;color:#fff;background:#0a84c2;border:0;border-radius:4px;
padding:9px 18px;cursor:pointer}}a{{color:#0f9488}}</style></head><body>{body}</body></html>""",
        headers=NO_STORE)


@router.get("/api/unsubscribe")
def unsubscribe_page(t: str = Query("", max_length=200)):
    # A GET never changes anything: mail scanners follow every link in a message.
    if not auth.unsubscribe_sub(t):
        return _page("Link not valid", "<h1>This link is not valid.</h1><p><a href='/'>Back to the site</a></p>")
    return _page("Stop reminder emails", f"""<h1>Stop prediction reminder emails?</h1>
<p>You will no longer be emailed on the day your predictions come due. You can turn
reminders back on from the Predictions tab.</p>
<form method="post" action="/api/unsubscribe?t={html.escape(t, quote=True)}"><button>Unsubscribe</button></form>""")


@router.post("/api/unsubscribe")
def unsubscribe(t: str = Query("", max_length=200)):
    # Also the RFC 8058 one-click target (List-Unsubscribe-Post), which mail
    # providers call cross-site with a form body - so no same_origin check;
    # the signed token is the authorisation.
    sub = auth.unsubscribe_sub(t)
    if not sub:
        return _page("Link not valid", "<h1>This link is not valid.</h1>")
    with app_session():
        User.update(reminders_off=True).where(User.sub == sub).execute()
    return _page("Unsubscribed", "<h1>Done - no more reminder emails.</h1><p><a href='/'>Back to the site</a></p>")


def install(app) -> None:
    app.include_router(router)
    app.add_exception_handler(AuthError, lambda _r, exc: auth_error_response(exc))
    app.add_exception_handler(PredictionError, lambda _r, exc: JSONResponse(
        {"detail": str(exc)}, status_code=exc.status, headers=NO_STORE))
    threading.Thread(target=_startup, daemon=True).start()


def _startup() -> None:
    """Create app.db now rather than on the first request, so Litestream
    (which waits for the file) starts replicating it straight away; then
    fetch Cognito's signing keys ahead of the first login."""
    try:
        with app_session():
            pass
    except HTTPException:
        pass
    auth.warm()
