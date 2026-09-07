#!/usr/bin/env python3
"""Self-contained checks for the parts that are easy to get quietly wrong.

Plain asserts, no pytest required:   python tests/test_core.py
(pytest works too if you have it:    pytest tests/)

Needs numpy; nothing else. Job boards are never contacted - the scraper is
stubbed, so this runs offline and deterministically.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="adc-tests-"))
os.environ.update(DATA_DIR=str(TMP), DB_PATH=str(TMP / "jobs.db"), SCRAPE_PAUSE_SEC="0", S3_BUCKET="")

import numpy as np  # noqa: E402

from common import config, db, service  # noqa: E402
from common.forecast import arima_forecast, forecast_series, linear_forecast  # noqa: E402

PASS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"{name}: {detail}")
    PASS.append(name)
    print(f"  ok  {name}" + (f"  ({detail})" if detail else ""))


def _dates(n: int, start=date(2026, 1, 1)) -> list[str]:
    return [(start + timedelta(days=i)).isoformat() for i in range(n)]


# ─── forecasting ──────────────────────────────────────────────────────────── #
def test_linear_recovers_a_known_slope():
    y = 1000 + 7.5 * np.arange(40)
    r = linear_forecast(y, 10)
    check("linear recovers slope", abs(r.params["slope_per_day"] - 7.5) < 1e-6,
          f"slope={r.params['slope_per_day']:.4f}")
    check("linear extrapolates exactly", abs(r.yhat[-1] - (1000 + 7.5 * 49)) < 1e-6)


def test_damping_flattens_the_trend():
    """A damped trend converges to last + slope * phi/(1 - phi), not to infinity.

    That limit is the whole point: at phi=0.98 the model can only ever add about
    49 more days of trend, however far out you ask it to go.
    """
    slope, phi, n, h = 5.0, 0.98, 60, 500
    y = 1000 + slope * np.arange(n)
    far = linear_forecast(y, h)
    damped = linear_forecast(y, h, damping=phi)

    limit = y[-1] + slope * phi / (1 - phi)
    check("damping flattens the line", damped.yhat[-1] < far.yhat[-1] / 2,
          f"{far.yhat[-1]:.0f} -> {damped.yhat[-1]:.0f}")
    check("damped trend converges to its analytic limit",
          abs(damped.yhat[-1] - limit) < 1.0, f"{damped.yhat[-1]:.1f} vs {limit:.1f}")
    check("damped forecast is monotone in the horizon",
          all(b >= a - 1e-9 for a, b in zip(damped.yhat, damped.yhat[1:])))


def test_prediction_interval_widens():
    rng = np.random.default_rng(0)
    y = np.cumsum(rng.normal(0, 1, 120)) + 500
    r = arima_forecast(y, 200)
    widths = np.array(r.hi) - np.array(r.lo)
    check("interval widens with horizon", widths[-1] > widths[0] > 0,
          f"{widths[0]:.2f} -> {widths[-1]:.2f}")


def test_arima_identifies_a_random_walk_with_drift():
    rng = np.random.default_rng(5)
    y = np.cumsum(rng.normal(2.0, 8.0, 300)) + 5000
    r = arima_forecast(y, 60)
    check("ARIMA picks d=1 for a random walk", r.params["d"] == 1, r.model)
    check("ARIMA follows the drift", r.yhat[-1] > y[-1], f"{y[-1]:.0f} -> {r.yhat[-1]:.0f}")


def test_model_switches_at_the_configured_threshold():
    rng = np.random.default_rng(1)
    make = lambda n: (2000 + 3 * np.arange(n) + rng.normal(0, 20, n)).tolist()  # noqa: E731
    short = forecast_series(_dates(20), make(20), "2027-12-31", arima_min_points=30)
    long = forecast_series(_dates(60), make(60), "2027-12-31", arima_min_points=30)
    check("under the threshold -> linear", short["model"] == "linear", short["model"])
    check("over the threshold -> ARIMA", long["model"].startswith("arima"), long["model"])


def test_no_forecast_without_enough_history():
    check("too little data -> no forecast",
          forecast_series(_dates(3), [1.0, 2.0, 3.0], "2027-12-31", min_points=7) is None)


def test_forecast_never_goes_negative():
    y = np.maximum(400 - 3.0 * np.arange(90), 1.0)
    r = forecast_series(_dates(90), y.tolist(), "2027-12-31", arima_min_points=30)
    check("forecast is non-negative", min(p["lo"] for p in r["points"]) >= 0)


def test_horizon_reaches_the_requested_end_date():
    r = forecast_series(_dates(60), (1000 + np.arange(60) * 1.0).tolist(), "2027-12-31")
    check("horizon ends on the target date", r["points"][-1]["date"] == "2027-12-31",
          f"{len(r['points'])} points")


# ─── the active-listings metric ───────────────────────────────────────────── #
def test_series_is_unbiased_at_the_right_edge():
    """N new listings a day, each living exactly L days, observed every day.

    The steady-state answer is N*L on every day - including today. This is the
    regression test for the trailing-grace bug, where the most recent days were
    counted on different terms from the rest and the curve sloped on its own.
    """
    dbp = TMP / "metric.db"
    dbp.unlink(missing_ok=True)
    conn = db.connect(dbp)
    N, L, DAYS = 40, 15, 50
    start = date(2026, 1, 1)
    today = start + timedelta(days=DAYS - 1)
    live: dict[int, date] = {}
    jid = 0
    for i in range(DAYS):
        day = start + timedelta(days=i)
        for _ in range(N):
            jid += 1
            live[jid] = day
        live = {k: v for k, v in live.items() if (day - v).days < L}
        service.build(conn).job_repo.upsert("india", [
            dict(id=str(k), site="indeed", title="Software Engineer", company="Example",
                 location="India", is_remote=0, job_type="fulltime", date_posted=v.isoformat(),
                 job_url=f"https://example.com/{k}", min_amount=None, max_amount=None,
                 currency="INR", pay_interval=None, is_tech=1, description=None)
            for k, v in live.items()], day.isoformat())
        service.build(conn).snapshot_repo.record("india", day.isoformat(), ran_at="t", ok=1)

    s = service.build(conn).series.daily_series("india", today=today.isoformat())
    tail = s["values"][-10:]
    check("steady state reads N*L at the edge", set(tail) == {N * L}, f"tail={tail[:4]}… expected {N * L}")
    conn.close()


def test_stale_posting_dates_do_not_stretch_the_chart():
    """A listing dated two years ago but seen today belongs to today.

    Live Indeed results routinely include postings over a year old. Capping a
    listing's lifetime against its *start* pushed those intervals entirely into
    the past - inventing activity on days we never observed, and omitting the
    listing from the day we actually saw it. Found by running a real scrape.
    """
    dbp = TMP / "stale.db"
    dbp.unlink(missing_ok=True)
    conn = db.connect(dbp)
    today = "2026-09-05"
    rows = [dict(id="ancient", site="indeed", title="Data Science Engineer", company="Example",
                 location="India", is_remote=0, job_type="fulltime", date_posted="2024-01-17",
                 job_url="https://example.com/1", min_amount=None, max_amount=None,
                 currency="INR", pay_interval=None, is_tech=1, description=None),
            dict(id="fresh", site="indeed", title="Software Engineer", company="Example",
                 location="India", is_remote=0, job_type="fulltime", date_posted=today,
                 job_url="https://example.com/2", min_amount=None, max_amount=None,
                 currency="INR", pay_interval=None, is_tech=1, description=None)]
    service.build(conn).job_repo.upsert("india", rows, today)
    service.build(conn).snapshot_repo.record("india", today, ran_at="t", ok=1)

    s = service.build(conn).series.daily_series("india", today=today)
    span = (date.fromisoformat(s["dates"][-1]) - date.fromisoformat(s["dates"][0])).days
    check("chart span is bounded by RECONSTRUCT_DAYS", span <= config.RECONSTRUCT_DAYS,
          f"span={span}d from a 2024 posting date")
    check("a listing seen today is counted today", s["values"][-1] == 2,
          f"today={s['values'][-1]}, expected both listings")
    check("series does not reach back to the posting year",
          not s["dates"][0].startswith("2024"), s["dates"][0])
    conn.close()


def test_series_service_runs_without_a_database():
    """The point of the repository split: rules testable without SQLite.

    SeriesService only ever calls `jobs.intervals()` and
    `snapshots.observed_dates()`, so a pair of stubs is enough to drive it. No
    file, no schema, no fixtures - just the interval arithmetic under test.
    """
    class StubJobs:
        def __init__(self, rows): self.rows = rows
        def intervals(self, country, *, tech_only=True): return iter(self.rows)

    class StubSnapshots:
        def __init__(self, days): self.days = days
        def observed_dates(self, country): return self.days

    # posted, first_seen, last_seen
    rows = [("2026-03-01", "2026-03-01", "2026-03-05"),   # spans 1-5
            ("2026-03-03", "2026-03-03", "2026-03-04")]   # spans 3-4
    svc = service.SeriesService(StubJobs(rows), StubSnapshots(["2026-03-03"]))
    out = svc.daily_series("india", today="2026-03-05")

    check("stub-driven series spans the union of intervals",
          out["dates"] == ["2026-03-01", "2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05"],
          str(out["dates"]))
    check("overlapping days are counted twice",
          out["values"] == [1, 1, 2, 2, 1], str(out["values"]))
    check("observed_from comes from the snapshot stub",
          out["observed_from"] == "2026-03-03", out["observed_from"])
    check("fit window starts at first observation, not first data",
          service.SeriesService.fit_window(out, max_days=None) ==
          (["2026-03-03", "2026-03-04", "2026-03-05"], [2.0, 2.0, 1.0]))


def test_first_seen_never_moves_backwards():
    dbp = TMP / "fs.db"
    dbp.unlink(missing_ok=True)
    conn = db.connect(dbp)
    row = dict(id="a", site="indeed", title="Software Engineer", company="Example",
               location="India", is_remote=0, job_type="fulltime", date_posted="2026-01-01",
               job_url="https://example.com/a", min_amount=None, max_amount=None,
               currency="INR", pay_interval=None, is_tech=1, description=None)
    service.build(conn).job_repo.upsert("india", [row], "2026-01-05")
    service.build(conn).job_repo.upsert("india", [row], "2026-01-09")
    got = conn.execute("SELECT first_seen, last_seen, seen_count FROM jobs").fetchone()
    check("first_seen is stable, last_seen advances",
          tuple(got) == ("2026-01-05", "2026-01-09", 2), str(tuple(got)))
    conn.close()


def test_fit_window_excludes_reconstructed_days():
    dbp = TMP / "fit.db"
    dbp.unlink(missing_ok=True)
    conn = db.connect(dbp)
    # Posted well before we ever ran, so the series starts before observation did.
    service.build(conn).job_repo.upsert("india", [
        dict(id=f"j{i}", site="indeed", title="Software Engineer", company="Example",
             location="India", is_remote=0, job_type="fulltime", date_posted="2026-01-01",
             job_url=f"https://example.com/{i}", min_amount=None, max_amount=None,
             currency="INR", pay_interval=None, is_tech=1, description=None)
        for i in range(5)], "2026-03-01")
    service.build(conn).snapshot_repo.record("india", "2026-03-01", ran_at="t", ok=1)
    s = service.build(conn).series.daily_series("india", today="2026-03-01")
    d, _ = service.SeriesService.fit_window(s, max_days=None)
    check("series includes reconstructed history", s["dates"][0] == "2026-01-01", s["dates"][0])
    check("fit window starts at first observation", d == ["2026-03-01"], str(d))
    conn.close()


def test_tech_classifier():
    from common.scrape import is_tech
    for title, want in [("Senior Software Engineer", True), ("SDE II", True),
                        ("Staff Data Scientist", True), ("Frontend Developer (React)", True),
                        ("Sales Executive", False), ("Civil Engineer", False),
                        ("Nurse", False), ("Technical Recruiter", False), ("", False)]:
        check(f"classifier: {title or '<empty>'!r}", is_tech(title) is want)


def test_every_run_issues_the_same_queries():
    from common.scrape import plan
    a = plan("india", date(2026, 5, 1))
    b = plan("india", date(2026, 9, 17))
    check("query plan is date-independent", a == b, f"{len(a)} requests/run")


# ─── pipeline, with the boards stubbed out ────────────────────────────────── #
def test_pipeline_end_to_end():
    import types

    import pandas as pd

    pool: dict[str, dict] = {}
    day_holder = {"d": date(2026, 1, 1)}

    def fake_scrape_jobs(**kw):
        site = kw["site_name"][0]
        day = day_holder["d"]
        for _ in range(6):
            i = len(pool) + 1
            pool[f"{site}-{i}"] = dict(
                id=f"{site}-{i}", site=site, job_url=f"https://example.com/{site}/{i}",
                title="Software Engineer", company="Example", location="India",
                date_posted=day.isoformat(), is_remote=False, job_type="fulltime",
                min_amount=900000.0, max_amount=1800000.0, currency="INR",
                interval="yearly", description="x" * 500)
        return pd.DataFrame([r for r in pool.values() if r["site"] == site])

    sys.modules["jobspy"] = types.SimpleNamespace(scrape_jobs=fake_scrape_jobs)
    dbp = TMP / "pipe.db"
    dbp.unlink(missing_ok=True)
    config.DB_PATH = dbp
    from common import pipeline

    start = date(2026, 1, 1)
    for i in range(9):
        day_holder["d"] = start + timedelta(days=i)
        res = pipeline.run_country("india", run_day=day_holder["d"])
    check("pipeline run succeeds", res["ok"] and res["rows"] > 0, str(res))

    conn = db.connect(dbp, read_only=True)
    check("snapshots recorded", len(service.build(conn).snapshot_repo.observed_dates("india")) == 9)
    check("descriptions dropped by default",
          conn.execute("SELECT COUNT(*) FROM jobs WHERE description IS NOT NULL").fetchone()[0] == 0)
    fc = service.build(conn).forecasts.get("india")
    check("forecast produced after 7+ days", fc is not None and fc["model"] == "linear",
          fc["model"] if fc else "none")
    summary = service.build(conn).series.summary("india")
    series = service.build(conn).series.daily_series("india", today=(start + timedelta(days=8)).isoformat())
    check("summary agrees with the series", summary["tech_active"] == series["values"][-1],
          f"{summary['tech_active']} vs {series['values'][-1]}")
    conn.close()


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = []
    for t in tests:
        print(f"\n{t.__name__}")
        try:
            t()
        except AssertionError as exc:
            failed.append(f"{t.__name__}: {exc}")
            print(f"  FAIL  {exc}")
    print(f"\n{len(PASS)} checks passed across {len(tests)} tests"
          + (f", {len(failed)} FAILED" if failed else ""))
    for f in failed:
        print(f"  - {f}")
    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
