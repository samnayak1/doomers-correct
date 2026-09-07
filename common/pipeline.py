"""
1. scrape data
2. fit ARIMA model to active-listings series
3. store data and forecast in SQLite

"""

from __future__ import annotations

import time
from datetime import date, datetime, timezone

from . import config, db, s3store, service


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {msg}", flush=True)


def refresh_forecast(conn, country: str, *, until: str | None = None) -> dict | None:
    """Kept as a module-level helper because the CLI and the seeder both call it."""
    return service.build(conn).forecasts.refresh(country, until=until, log=log)


def run_country(country: str, *, run_day: date | None = None) -> dict:
    country = config.country_key(country)
    run_day = run_day or date.today()
    scrape_date = run_day.isoformat()
    started = time.time()

    conn = db.connect(config.DB_PATH)
    svc = service.build(conn)
    total_rows = 0
    new_total = 0
    by_site: dict[str, int] = {}
    ok = True
    note = ""

    try:
        from .scrape import scrape_country
        for site, rows in scrape_country(country, run_day, log=log):
            new, seen = svc.job_repo.upsert(country, rows, scrape_date)
            new_total += new
            total_rows += seen
            by_site[site] = by_site.get(site, 0) + seen
    except Exception as exc:
        ok = False
        note = f"{type(exc).__name__}: {exc}"
        log(f"[run] {country}: scrape aborted - {note}")

    if total_rows == 0 and not ok:
        svc.snapshot_repo.record(country, scrape_date, ran_at=datetime.now(timezone.utc).isoformat(),
                           duration_sec=round(time.time() - started, 1), rows_seen=0, ok=0, note=note)
        conn.close()
        return {"country": country, "ok": False, "note": note}

    series = svc.series.daily_series(country, today=scrape_date)
    active_total = series["values"][-1] if series["values"] else 0
    all_series = svc.series.daily_series(country, tech_only=False, today=scrape_date)
    remote_active = svc.job_repo.count_remote(country, seen_on_or_after=scrape_date)
    tech_seen = svc.job_repo.count_seen_on(country, scrape_date)

    svc.snapshot_repo.record(
        country, scrape_date,
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

    svc.forecasts.refresh(country, log=log)
    # Only the Lambda path backs the file up here; on EC2, Litestream has it.
    raw_key = None
    if config.DB_BACKUP_TO_S3:
        try:
            raw_key = s3store.backup_db(config.DB_PATH)
        except Exception as exc:
            log(f"[s3] {country}: backup FAILED {type(exc).__name__}: {exc}")
    if raw_key:
        svc.snapshot_repo.set_s3_key(country, scrape_date, raw_key)

    result = {"country": country, "ok": ok, "rows": total_rows, "new": new_total,
              "tech_active": active_total, "duration_sec": round(time.time() - started, 1)}
    conn.close()
    return result


def run_all() -> list[dict]:
    # A no-op under Litestream, which has already restored the file before this
    # container was allowed to start (see db-restore in docker-compose.yml).
    if config.DB_BACKUP_TO_S3:
        s3store.restore_db(config.DB_PATH)
    return [run_country(c) for c in config.COUNTRIES]


if __name__ == "__main__":
    import argparse, json as _json
    ap = argparse.ArgumentParser(description="Scrape job boards and update the dataset.")
    ap.add_argument("--country", default="all", help="india | usa | all")
    ap.add_argument("--forecast-only", action="store_true", help="re-fit models without scraping")
    a = ap.parse_args()

    if a.forecast_only:
        conn = db.connect(config.DB_PATH)
        out = {c: bool(refresh_forecast(conn, c)) for c in
               (config.COUNTRIES if a.country == "all" else [config.country_key(a.country)])}
        conn.close()
    else:
        out = run_all() if a.country == "all" else [run_country(a.country)]
    print(_json.dumps(out, indent=2, default=str))
