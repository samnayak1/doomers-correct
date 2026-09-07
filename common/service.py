"""Business logic.

Services hold the rules — what "active" means, which days a model may see, how
a summary is assembled. They reach data only through repository methods, never
through a cursor, so a test can hand them a stub object and skip SQLite
entirely.

`build(conn)` wires the three services against one connection; routes and the
pipeline use that rather than constructing repositories by hand.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from . import config
from .repository import ForecastRepository, JobRepository, SnapshotRepository


class SeriesService:
    """Turns listing intervals into a daily active-listings curve."""

    def __init__(self, jobs, snapshots) -> None:
        self.jobs = jobs
        self.snapshots = snapshots

    def daily_series(self, country: str, *, tech_only: bool = True,
                     reconstruct_days: int | None = None, today: str | None = None) -> dict:
        """Reconstruct active-listing counts per day.

        Each listing contributes an interval:

            start = min(date_posted, first_seen)          # when the listing went live
            start = max(start, first_seen - reconstruct)  # bound the pre-launch tail
            end   = min(last_seen, today)                 # when we last saw it

        Counting overlaps per day gives the curve. There is deliberately no
        grace period past the last sighting: it would bias the recent end of the
        curve downward regardless of the market. The `start` clamp stops one
        stale posting date stretching the chart across years nobody observed.

        Days before the first completed scrape are survivorship-biased — only
        listings still live when we first looked can be reconstructed — so
        `observed_from` marks where real observation begins, and the model is
        never fitted before it.
        """
        reconstruct_days = config.RECONSTRUCT_DAYS if reconstruct_days is None else reconstruct_days
        today_d = date.fromisoformat(today) if today else date.today()

        counts: dict[int, int] = {}
        lo_ord = hi_ord = None
        for posted, first_seen, last_seen in self.jobs.intervals(country, tech_only=tech_only):
            try:
                fs = date.fromisoformat(first_seen)
            except (TypeError, ValueError):
                continue
            start = fs
            if posted:
                try:
                    pd_ = date.fromisoformat(str(posted)[:10])
                    if pd_ < start:
                        start = pd_
                except ValueError:
                    pass
            start = max(start, fs - timedelta(days=reconstruct_days))
            try:
                ls = date.fromisoformat(last_seen)
            except (TypeError, ValueError):
                ls = fs
            end = min(ls, today_d)
            if end < start:
                continue
            a, b = start.toordinal(), end.toordinal()
            # difference array: +1 at start, -1 just past the end
            counts[a] = counts.get(a, 0) + 1
            counts[b + 1] = counts.get(b + 1, 0) - 1
            lo_ord = a if lo_ord is None else min(lo_ord, a)
            hi_ord = b if hi_ord is None else max(hi_ord, b)

        if lo_ord is None:
            return {"dates": [], "values": [], "observed_from": None,
                    "reconstructed_until": None, "observed_days": 0}

        dates, values, running = [], [], 0
        for o in range(lo_ord, hi_ord + 1):
            running += counts.get(o, 0)
            dates.append(date.fromordinal(o).isoformat())
            values.append(running)

        observed = self.snapshots.observed_dates(country)
        observed_from = observed[0] if observed else None
        return {
            "dates": dates,
            "values": values,
            "observed_from": observed_from,
            "reconstructed_until": observed_from,
            "observed_days": len(observed),
        }

    @staticmethod
    def fit_window(series: dict, max_days: int | None = -1) -> tuple[list[str], list[float]]:
        """The slice a model may see: observed days only, optionally the last N.

        Pure function of the series — no data access, so it is trivially testable.
        """
        obs_from = series.get("observed_from")
        if not obs_from:
            return [], []
        dates, values = series["dates"], series["values"]
        try:
            i = dates.index(obs_from)
        except ValueError:
            return [], []
        if max_days == -1:
            max_days = config.FORECAST_FIT_DAYS
        d, v = dates[i:], [float(x) for x in values[i:]]
        if max_days and len(d) > max_days:
            d, v = d[-max_days:], v[-max_days:]
        return d, v

    def summary(self, country: str) -> dict:
        snap = self.snapshots.latest(country)
        prev = self.snapshots.latest(country, offset=1)

        # Before the first run finishes there is no snapshot, but there may
        # already be listings on disk. Reporting 0 next to a chart showing a
        # thousand of them is worse than reporting the live count and saying so.
        live = None
        if not snap:
            series = self.daily_series(country)
            live = series["values"][-1] if series["values"] else 0

        out = {
            "latest_scrape": snap["scrape_date"] if snap else None,
            "scrape_in_progress": snap is None,
            "tech_active": snap["tech_active"] if snap else live,
            "active_total": snap["active_total"] if snap else live,
            "remote_active": snap["remote_active"] if snap else self.jobs.count_remote(country),
            "new_jobs": snap["new_jobs"] if snap else 0,
            "scrapes": self.snapshots.count_ok(country),
            "tracked_total": self.jobs.count(country),
            "by_site": self.jobs.count_by_site(country),
            "s3_key": snap["s3_key"] if snap else None,
        }
        if snap and prev and prev["tech_active"]:
            out["delta_pct"] = round(
                100.0 * (snap["tech_active"] - prev["tech_active"]) / prev["tech_active"], 2)
        return out

    def history(self, country: str, limit: int = 60) -> list[dict]:
        """Past runs - the audit trail behind the chart."""
        return self.snapshots.recent(country, limit)


class JobService:
    def __init__(self, jobs) -> None:
        self.jobs = jobs

    def list(self, country: str, **filters) -> dict:
        total, rows = self.jobs.search(country, **filters)
        return {"total": total, "rows": rows}


class ForecastService:
    def __init__(self, series: SeriesService, forecasts) -> None:
        self.series = series
        self.forecasts = forecasts

    def get(self, country: str, metric: str = "tech_active") -> dict | None:
        return self.forecasts.get(country, metric)

    def refresh(self, country: str, *, until: str | None = None, log=print) -> dict | None:
        """Re-fit on the observed history and cache the result.

        Only days we actually scraped are used. The reconstructed pre-launch
        tail is survivorship-biased, so fitting on it would manufacture growth
        that never happened.
        """
        series = self.series.daily_series(country)
        dates, values = self.series.fit_window(series, max_days=config.FORECAST_FIT_DAYS)
        if len(dates) < config.FORECAST_MIN_POINTS:
            log(f"[forecast] {country}: {len(dates)} observed day(s), need "
                f"{config.FORECAST_MIN_POINTS} - skipping")
            return None

        from .forecast import forecast_series  # lazy: pulls in numpy
        payload = forecast_series(
            dates, values, until or config.FORECAST_UNTIL,
            arima_min_points=config.ARIMA_MIN_POINTS,
            min_points=config.FORECAST_MIN_POINTS,
            log_space=config.FORECAST_LOG_SPACE,
            damping=config.FORECAST_DAMPING,
        )
        if payload:
            self.forecasts.save(country, "tech_active", payload,
                                datetime.now(timezone.utc).isoformat())
            log(f"[forecast] {country}: {payload['model']} over {payload['horizon_days']}d "
                f"(fit on {payload['fitted_on']['points']} pts) -> "
                f"{payload['points'][-1]['yhat']:.0f} on {payload['points'][-1]['date']}")
        return payload


@dataclass
class Services:
    series: SeriesService
    jobs: JobService
    forecasts: ForecastService
    # repositories stay reachable for the pipeline's write path
    job_repo: JobRepository
    snapshot_repo: SnapshotRepository


def build(conn: sqlite3.Connection) -> Services:
    job_repo = JobRepository(conn)
    snapshot_repo = SnapshotRepository(conn)
    forecast_repo = ForecastRepository(conn)
    series = SeriesService(job_repo, snapshot_repo)
    return Services(
        series=series,
        jobs=JobService(job_repo),
        forecasts=ForecastService(series, forecast_repo),
        job_repo=job_repo,
        snapshot_repo=snapshot_repo,
    )
