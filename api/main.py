"""HTTP routes.

Deliberately thin: parse and validate query parameters, hand off to a service,
return what it produces. No SQL, no business rules — those live in
common/service.py and common/repository.py respectively.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

import peewee

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from pydantic import BaseModel, Field

from common import config, contact, db, service

app = FastAPI(title="Are doomers correct?", docs_url="/api/docs", openapi_url="/api/openapi.json")
app.add_middleware(GZipMiddleware, minimum_size=1024)

STARTED = datetime.now(timezone.utc)


_db = None


def _database():
    """Bind the model proxy once, not per request.

    The models share a global DatabaseProxy, so re-initialising it on every
    request would race between the threads FastAPI runs sync endpoints on.
    Bind once; Peewee keeps the connection itself thread-local.
    """
    global _db
    if _db is None:
        if not config.DB_PATH.exists():
            raise HTTPException(503, "dataset not built yet - the first scrape has not finished")
        try:
            _db = db.connect(config.DB_PATH, read_only=True)
        except peewee.OperationalError:
            raise HTTPException(503, "dataset not built yet - the first scrape has not finished")
    return _db


@contextmanager
def services():
    """A per-request connection from the shared, read-only database."""
    database = _database()
    with database.connection_context():
        yield service.build(database)


@app.get("/api/health")
def health():
    return {"ok": True, "db": str(config.DB_PATH), "db_present": config.DB_PATH.exists(),
            "uptime_sec": round((datetime.now(timezone.utc) - STARTED).total_seconds())}


@app.get("/api/countries")
def countries():
    return {
        "default": config.DEFAULT_COUNTRY,
        "countries": [
            {"key": k, "label": v["label"], "prose": v["prose"],
             "currency": v["currency"], "sites": v["sites"]}
            for k, v in config.COUNTRIES.items()
        ],
    }


@app.get("/api/series")
def series(country: str = Query(config.DEFAULT_COUNTRY)):
    country = config.country_key(country)
    with services() as svc:
        payload = {
            "country": country,
            "label": config.COUNTRIES[country]["label"],
            "prose": config.COUNTRIES[country]["prose"],
            "metric": "tech_active",
            "series": svc.series.daily_series(country),
            "forecast": svc.forecasts.get(country),
            "summary": svc.series.summary(country),
            "config": {
                "forecast_until": config.FORECAST_UNTIL,
                "arima_min_points": config.ARIMA_MIN_POINTS,
                "forecast_min_points": config.FORECAST_MIN_POINTS,
                "forecast_fit_days": config.FORECAST_FIT_DAYS,
            },
        }
    return JSONResponse(payload, headers={"Cache-Control": "public, max-age=300"})


@app.get("/api/jobs")
def jobs(country: str = Query(config.DEFAULT_COUNTRY), q: str = "",
         site: str = "", remote: str = "", role: str = "", sort: str = "date_posted",
         limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
    country = config.country_key(country)
    with services() as svc:
        out = svc.jobs.list(country, q=q.strip()[:80],
                            site=site.strip()[:24], remote=remote,
                            role=role.strip()[:40], sort=sort,
                            limit=limit, offset=offset)
    out.update({"country": country, "limit": limit, "offset": offset,
                "currency": config.COUNTRIES[country]["currency"]})
    return JSONResponse(out, headers={"Cache-Control": "public, max-age=120"})


@app.get("/api/history")
def history(country: str = Query(config.DEFAULT_COUNTRY), limit: int = Query(60, ge=1, le=400)):
    country = config.country_key(country)
    with services() as svc:
        return {"country": country, "runs": svc.series.history(country, limit)}


class ContactIn(BaseModel):
    # Generous upper bounds only; the real rules live in common.contact.clean.
    name: str = Field("", max_length=200)
    email: str = Field(..., max_length=320)
    message: str = Field(..., max_length=8000)
    token: str = Field(..., max_length=2000)


@app.get("/api/site")
def site():
    gc = config.GOATCOUNTER_HOST
    return {"analytics": f"https://{gc}/count" if gc else None}


@app.get("/api/contact")
def contact_config():
    return {"enabled": contact.enabled(), "endpoint": contact.widget_endpoint()}


@app.post("/api/contact")
def contact_send(body: ContactIn, request: Request):
    # Caddy and nginx both append the real client to X-Forwarded-For, so the
    # last entry is the one a visitor cannot forge. The API is never exposed
    # directly - only through the proxy - which is what makes that safe.
    fwd = request.headers.get("x-forwarded-for", "")
    ip = fwd.split(",")[-1].strip() if fwd else (request.client.host if request.client else "?")
    try:
        contact.submit(body.name, body.email, body.message, body.token, ip)
    except contact.ContactError as exc:
        raise HTTPException(exc.status, str(exc))
    return {"ok": True}
