#!/usr/bin/env python3
"""Populate the database with plausible synthetic data.

For local development and for eyeballing the UI before a real scrape has run.
Never invoked by the containers; run it by hand.  Rows are tagged so they are
obvious in the data: companies are named "Example ..." and URLs point at example.com.
"""
from __future__ import annotations

import argparse
import random
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import config, db  # noqa: E402
from common.pipeline import refresh_forecast  # noqa: E402

TITLES = ["Software Engineer", "Senior Backend Developer", "Data Engineer", "DevOps Engineer",
          "Frontend Developer", "Machine Learning Engineer", "Site Reliability Engineer",
          "Full Stack Developer", "Android Developer", "Cloud Engineer", "QA Automation Engineer",
          "Staff Software Engineer", "Security Engineer", "iOS Developer"]
CITIES = {"india": ["Bengaluru", "Hyderabad", "Pune", "Chennai", "Gurugram", "Mumbai", "Noida"],
          "australia": ["Sydney", "Melbourne", "Brisbane", "Perth", "Canberra", "Adelaide"],
          "usa": ["San Francisco, CA", "New York, NY", "Seattle, WA", "Austin, TX",
                  "Boston, MA", "Denver, CO"]}


def seed(country: str, days: int, base: int, drift: float, rng: random.Random) -> None:
    conn = db.connect(config.DB_PATH)
    today = date.today()
    start = today - timedelta(days=days - 1)
    cur = config.COUNTRIES[country]["currency"]
    jid = 0
    level = float(base)

    for i in range(days):
        day = start + timedelta(days=i)
        level = max(20.0, level * (1.0 + drift) + rng.gauss(0, level * 0.02))
        n_new = max(1, int(rng.gauss(level / 25.0, level / 90.0)))
        rows = []
        for _ in range(n_new):
            jid += 1
            city = rng.choice(CITIES[country])
            lo = rng.randrange(6, 40) * (100000 if country == "india" else 5000)
            rows.append({
                "id": f"seed-{country}-{jid}", "site": rng.choice(config.COUNTRIES[country]["sites"]),
                "title": rng.choice(TITLES), "company": f"Example {rng.choice('ABCDEFGHJK')}{rng.randrange(10,99)} Labs",
                "location": f"{city}, {config.COUNTRIES[country]['label']}",
                "is_remote": 1 if rng.random() < 0.18 else 0, "job_type": "fulltime",
                "date_posted": day.isoformat(), "job_url": f"https://example.com/job/{country}/{jid}",
                "min_amount": float(lo), "max_amount": float(lo) * rng.uniform(1.2, 1.9),
                "currency": cur, "pay_interval": "yearly", "is_tech": 1, "description": None,
            })
        db.upsert_jobs(conn, country, rows, day.isoformat())
        # Refresh a slice of older listings so last_seen advances realistically.
        conn.execute(
            "UPDATE jobs SET last_seen=? WHERE country=? AND last_seen>=? AND (rowid % 7) != 0",
            (day.isoformat(), country, (day - timedelta(days=45)).isoformat()))
        conn.commit()

        s = db.daily_series(conn, country, today=day.isoformat(),
                            active_window_days=config.ACTIVE_WINDOW_DAYS)
        db.record_snapshot(conn, country, day.isoformat(), ran_at=f"{day}T00:05:00Z",
                           duration_sec=rng.uniform(200, 500), rows_seen=n_new * 4,
                           new_jobs=n_new, tech_jobs=n_new,
                           active_total=int((s["values"][-1] if s["values"] else 0) * 1.35),
                           tech_active=s["values"][-1] if s["values"] else 0,
                           remote_active=int((s["values"][-1] if s["values"] else 0) * 0.17),
                           by_site={x: n_new for x in config.COUNTRIES[country]["sites"]}, ok=1)

    fc = refresh_forecast(conn, country)
    print(f"seeded {country}: {days} days, {jid} listings, model={fc['model'] if fc else 'none'}")
    conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--reset", action="store_true", help="delete the database first")
    a = ap.parse_args()
    if a.reset:
        for suffix in ("", "-wal", "-shm"):
            Path(str(config.DB_PATH) + suffix).unlink(missing_ok=True)
    rng = random.Random(a.seed)
    seed("india", a.days, base=2400, drift=-0.0015, rng=rng)
    seed("australia", a.days, base=650, drift=0.0009, rng=rng)
    seed("usa", a.days, base=5200, drift=-0.0008, rng=rng)
    print(f"database: {config.DB_PATH}")
