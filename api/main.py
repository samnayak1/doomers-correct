"""Read-only JSON API over the SQLite dataset.

The heavy lifting (scraping, model fitting) happens in the worker; this process
only reads, so it stays small and needs neither pandas nor numpy.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from common import config, db, s3store

app = FastAPI(title="Are doomers correct?", docs_url="/api/docs", openapi_url="/api/openapi.json")
app.add_middleware(GZipMiddleware, minimum_size=1024)

STARTED = datetime.now(timezone.utc)


def _conn() -> sqlite3.Connection:
    try:
        return db.connect(config.DB_PATH, read_only=True)
    except sqlite3.OperationalError:
        raise HTTPException(503, "dataset not built yet - the first scrape has not finished")


@app.get("/api/health")
def health():
    exists = config.DB_PATH.exists()
    return {"ok": True, "db": str(config.DB_PATH), "db_present": exists,
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
    conn = _conn()
    try:
        s = db.daily_series(conn, country, tech_only=bool(tech),
                            active_window_days=config.ACTIVE_WINDOW_DAYS)
        payload = {
            "country": country,
            "label": config.COUNTRIES[country]["label"],
        "prose": config.COUNTRIES[country]["prose"],
            "metric": "tech_active" if tech else "all_active",
            "series": s,
            "forecast": db.get_forecast(conn, country) if tech else None,
            "summary": db.summary(conn, country),
            "config": {
                "active_window_days": config.ACTIVE_WINDOW_DAYS,
                "forecast_until": config.FORECAST_UNTIL,
                "arima_min_points": config.ARIMA_MIN_POINTS,
                "forecast_min_points": config.FORECAST_MIN_POINTS,
                "forecast_fit_days": config.FORECAST_FIT_DAYS,
            },
        }
    finally:
        conn.close()
    return JSONResponse(payload, headers={"Cache-Control": "public, max-age=300"})


@app.get("/api/jobs")
def jobs(country: str = Query(config.DEFAULT_COUNTRY), tech: int = 1, q: str = "",
         site: str = "", remote: str = "", sort: str = "date_posted",
         limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
    country = config.country_key(country)
    conn = _conn()
    try:
        out = db.list_jobs(conn, country, tech_only=bool(tech), limit=limit, offset=offset,
                           q=q.strip()[:80], site=site.strip()[:24], remote=remote, sort=sort)
    finally:
        conn.close()
    out.update({"country": country, "limit": limit, "offset": offset,
                "currency": config.COUNTRIES[country]["currency"]})
    return JSONResponse(out, headers={"Cache-Control": "public, max-age=120"})


@app.get("/api/data")
def data_links():
    """Where to get the raw files, so the data is usable without this API."""
    if not s3store.enabled():
        return {"s3": False, "note": "S3 publishing is not configured on this deployment."}
    return {
        "s3": True,
        "bucket": config.S3_BUCKET,
        "manifest": s3store.public_url(s3store.key("manifest.json")),
        "countries": {
            c: {
                "series": s3store.public_url(s3store.key("series", f"{c}.json")),
                "latest": s3store.public_url(s3store.key("latest", f"{c}.json")),
                "raw_prefix": s3store.public_url(s3store.key("raw", c)) + "/",
            }
            for c in config.COUNTRIES
        },
    }


@app.get("/api/history")
def history(country: str = Query(config.DEFAULT_COUNTRY), limit: int = Query(60, ge=1, le=400)):
    """Past scrape runs - the audit trail behind the chart."""
    country = config.country_key(country)
    conn = _conn()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT scrape_date, ran_at, rows_seen, new_jobs, tech_active, active_total, "
            "remote_active, duration_sec, ok, s3_key FROM snapshots "
            "WHERE country=? ORDER BY scrape_date DESC LIMIT ?", (country, limit))]
    finally:
        conn.close()
    return {"country": country, "runs": rows}
