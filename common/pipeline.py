"""One end-to-end run: scrape -> SQLite -> forecast -> S3."""

from __future__ import annotations

import time
from datetime import date, datetime, timezone

from . import config, db, s3store


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {msg}", flush=True)


def refresh_forecast(conn, country: str, *, until: str | None = None) -> dict | None:
    """Re-fit the model on the observed history and cache the result.

    Only days we actually scraped are used.  The reconstructed pre-launch tail of
    the chart is survivorship-biased (older listings we never saw have already
    expired), so fitting on it would manufacture a growth trend that is not real.
    """
    series = db.daily_series(
        conn, country,
        active_window_days=config.ACTIVE_WINDOW_DAYS,
    )
    dates, values = db.fit_window(series, max_days=config.FORECAST_FIT_DAYS)
    if len(dates) < config.FORECAST_MIN_POINTS:
        log(f"[forecast] {country}: {len(dates)} observed day(s), need "
            f"{config.FORECAST_MIN_POINTS} - skipping")
        return None

    from .forecast import forecast_series  # lazy: pulls in numpy
    payload = forecast_series(
        dates, values,
        until or config.FORECAST_UNTIL,
        arima_min_points=config.ARIMA_MIN_POINTS,
        min_points=config.FORECAST_MIN_POINTS,
        log_space=config.FORECAST_LOG_SPACE,
        damping=config.FORECAST_DAMPING,
    )
    if payload:
        now = datetime.now(timezone.utc).isoformat()
        db.save_forecast(conn, country, "tech_active", payload, now)
        log(f"[forecast] {country}: {payload['model']} over {payload['horizon_days']}d "
            f"(fit on {payload['fitted_on']['points']} pts) -> "
            f"{payload['points'][-1]['yhat']:.0f} on {payload['points'][-1]['date']}")
    return payload


def publish(conn, country: str, scrape_date: str, rows_for_s3: list[dict] | None) -> str | None:
    """Push this run's artefacts to S3.  Never fatal - the site works without it."""
    if not s3store.enabled():
        return None
    raw_key = None
    try:
        if rows_for_s3 is not None:
            raw_key = s3store.key("raw", country, f"{scrape_date}.json.gz")
            s3store.put_json(raw_key, {
                "country": country, "scrape_date": scrape_date,
                "count": len(rows_for_s3), "jobs": rows_for_s3,
            }, cache_seconds=86400)

        series = db.daily_series(conn, country,
                                 active_window_days=config.ACTIVE_WINDOW_DAYS)
        s3store.put_json(s3store.key("series", f"{country}.json"), {
            "country": country,
            "series": series,
            "forecast": db.get_forecast(conn, country),
            "summary": db.summary(conn, country),
        })
        s3store.put_json(s3store.key("latest", f"{country}.json"),
                         db.list_jobs(conn, country, limit=1000))
        if config.DB_BACKUP_TO_S3:
            s3store.backup_db(config.DB_PATH)
        s3store.write_manifest({
            c: {
                "label": cfg["label"],
                "series": s3store.public_url(s3store.key("series", f"{c}.json")),
                "latest": s3store.public_url(s3store.key("latest", f"{c}.json")),
                "raw_prefix": s3store.public_url(s3store.key("raw", c)) + "/",
            }
            for c, cfg in config.COUNTRIES.items()
        })
        log(f"[s3] {country}: published (raw={raw_key})")
    except Exception as exc:
        log(f"[s3] {country}: publish FAILED {type(exc).__name__}: {exc}")
    return raw_key


def run_country(country: str, *, run_day: date | None = None, publish_s3: bool = True) -> dict:
    country = config.country_key(country)
    run_day = run_day or date.today()
    scrape_date = run_day.isoformat()
    started = time.time()

    conn = db.connect(config.DB_PATH)
    total_rows = 0
    new_total = 0
    by_site: dict[str, int] = {}
    collected: list[dict] = []
    ok = True
    note = ""

    try:
        from .scrape import scrape_country
        for site, rows in scrape_country(country, run_day, log=log):
            new, seen = db.upsert_jobs(conn, country, rows, scrape_date)
            new_total += new
            total_rows += seen
            by_site[site] = by_site.get(site, 0) + seen
            if s3store.enabled():
                collected.extend(rows)
    except Exception as exc:
        ok = False
        note = f"{type(exc).__name__}: {exc}"
        log(f"[run] {country}: scrape aborted - {note}")

    if total_rows == 0 and not ok:
        db.record_snapshot(conn, country, scrape_date, ran_at=datetime.now(timezone.utc).isoformat(),
                           duration_sec=round(time.time() - started, 1), rows_seen=0, ok=0, note=note)
        conn.close()
        return {"country": country, "ok": False, "note": note}

    series = db.daily_series(conn, country,
                            active_window_days=config.ACTIVE_WINDOW_DAYS,
                            today=scrape_date)
    active_total = series["values"][-1] if series["values"] else 0
    all_series = db.daily_series(conn, country, tech_only=False,
                                 active_window_days=config.ACTIVE_WINDOW_DAYS,
                                 today=scrape_date)
    remote_active = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE country=? AND is_tech=1 AND is_remote=1 AND last_seen>=?",
        (country, scrape_date),
    ).fetchone()[0]
    tech_seen = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE country=? AND is_tech=1 AND last_seen=?",
        (country, scrape_date),
    ).fetchone()[0]

    db.record_snapshot(
        conn, country, scrape_date,
        ran_at=datetime.now(timezone.utc).isoformat(),
        duration_sec=round(time.time() - started, 1),
        rows_seen=total_rows, new_jobs=new_total, tech_jobs=tech_seen,
        active_total=all_series["values"][-1] if all_series["values"] else 0,
        tech_active=active_total, remote_active=remote_active,
        by_site=by_site, ok=1 if ok else 0, note=note or None,
    )
    # A board that answers 200 with an empty body is invisible unless we say so.
    # Naukri does exactly this when it decides to challenge the IP (406 inside,
    # zero rows outside), and a silently dead source is worse than a loud one.
    dead = [s for s in config.COUNTRIES[country]["sites"] if by_site.get(s, 0) == 0]
    if dead:
        log(f"[run] {country}: WARNING no rows from {', '.join(dead)} - "
            f"blocked, rate-limited, or returning empty. Consider setting PROXIES, "
            f"or drop them via SITES_{country.upper()}.")
    log(f"[run] {country}: {total_rows} rows ({new_total} new) from "
        f"{{{', '.join(f'{k}={v}' for k, v in sorted(by_site.items()))}}}, "
        f"tech active = {active_total}")

    refresh_forecast(conn, country)
    raw_key = publish(conn, country, scrape_date, collected if publish_s3 else None)
    if raw_key:
        conn.execute("UPDATE snapshots SET s3_key=? WHERE country=? AND scrape_date=?",
                     (raw_key, country, scrape_date))
        conn.commit()

    result = {"country": country, "ok": ok, "rows": total_rows, "new": new_total,
              "tech_active": active_total, "duration_sec": round(time.time() - started, 1)}
    conn.close()
    return result


def run_all(publish_s3: bool = True) -> list[dict]:
    # A no-op under Litestream, which has already restored the file before this
    # container was allowed to start (see db-restore in docker-compose.yml).
    if config.DB_BACKUP_TO_S3:
        s3store.restore_db(config.DB_PATH)
    return [run_country(c, publish_s3=publish_s3) for c in config.COUNTRIES]


if __name__ == "__main__":
    import argparse, json as _json
    ap = argparse.ArgumentParser(description="Scrape job boards and update the dataset.")
    ap.add_argument("--country", default="all", help="india | australia | all")
    ap.add_argument("--no-s3", action="store_true")
    ap.add_argument("--forecast-only", action="store_true", help="re-fit models without scraping")
    a = ap.parse_args()

    if a.forecast_only:
        conn = db.connect(config.DB_PATH)
        out = {c: bool(refresh_forecast(conn, c)) for c in
               (config.COUNTRIES if a.country == "all" else [config.country_key(a.country)])}
        conn.close()
    else:
        out = run_all(publish_s3=not a.no_s3) if a.country == "all" \
            else [run_country(a.country, publish_s3=not a.no_s3)]
    print(_json.dumps(out, indent=2, default=str))
