"""HTTP routes.

Deliberately thin: parse and validate query parameters, hand off to a service,
return what it produces. No SQL, no business rules — those live in
common/service.py and common/repository.py respectively.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from common import config, db, service

app = FastAPI(title="Are doomers correct?", docs_url="/api/docs", openapi_url="/api/openapi.json")
app.add_middleware(GZipMiddleware, minimum_size=1024)

STARTED = datetime.now(timezone.utc)


@contextmanager
def services():
    """One read-only connection per request, closed on the way out."""
    try:
        conn = db.connect(config.DB_PATH, read_only=True)
    except sqlite3.OperationalError:
        raise HTTPException(503, "dataset not built yet - the first scrape has not finished")
    try:
        yield service.build(conn)
    finally:
        conn.close()


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
def series(country: str = Query(config.DEFAULT_COUNTRY), tech: int = 1):
    country = config.country_key(country)
    with services() as svc:
        payload = {
            "country": country,
            "label": config.COUNTRIES[country]["label"],
            "prose": config.COUNTRIES[country]["prose"],
            "metric": "tech_active" if tech else "all_active",
            "series": svc.series.daily_series(country, tech_only=bool(tech)),
            "forecast": svc.forecasts.get(country) if tech else None,
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
def jobs(country: str = Query(config.DEFAULT_COUNTRY), tech: int = 1, q: str = "",
         site: str = "", remote: str = "", sort: str = "date_posted",
         limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
    country = config.country_key(country)
    with services() as svc:
        out = svc.jobs.list(country, tech_only=bool(tech), q=q.strip()[:80],
                            site=site.strip()[:24], remote=remote, sort=sort,
                            limit=limit, offset=offset)
    out.update({"country": country, "limit": limit, "offset": offset,
                "currency": config.COUNTRIES[country]["currency"]})
    return JSONResponse(out, headers={"Cache-Control": "public, max-age=120"})


@app.get("/api/history")
def history(country: str = Query(config.DEFAULT_COUNTRY), limit: int = Query(60, ge=1, le=400)):
    country = config.country_key(country)
    with services() as svc:
        return {"country": country, "runs": svc.series.history(country, limit)}
