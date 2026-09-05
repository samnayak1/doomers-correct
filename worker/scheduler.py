"""Nightly scheduler.

Runs the pipeline as a *subprocess* rather than in-process. pandas and numpy
hold on to a lot of arena memory after a big scrape; letting the child exit
returns every byte to the OS, so this container idles at ~20 MB between runs
instead of carrying the peak all day. On a 1 GB box that difference matters.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, "/app")

from common import config  # noqa: E402

_stop = False


def _handle(signum, _frame):
    global _stop
    _stop = True
    log(f"received signal {signum}; will exit after the current run")


def log(msg: str) -> None:
    print(f"{datetime.now().isoformat(timespec='seconds')} [scheduler] {msg}", flush=True)


def tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(config.TZ)
    except Exception:
        log(f"unknown timezone {config.TZ!r}; falling back to system local time")
        return None


def next_run(now: datetime) -> datetime:
    try:
        hh, mm = (int(x) for x in config.SCRAPE_AT.split(":", 1))
    except ValueError:
        hh, mm = 0, 0
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def run_once() -> int:
    log("starting scrape run")
    started = time.time()
    proc = subprocess.run(
        [sys.executable, "-u", "-m", "common.pipeline", "--country", "all"],
        cwd="/app", env={**os.environ, "PYTHONPATH": "/app"},
    )
    log(f"run finished rc={proc.returncode} in {time.time() - started:.0f}s")
    return proc.returncode


def main() -> None:
    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    zone = tz()
    log(f"schedule {config.SCRAPE_AT} {config.TZ} · db={config.DB_PATH} · "
        f"s3={'on' if config.S3_BUCKET else 'off'}")

    if config.RUN_ON_START:
        # A fresh box should have something to show without waiting for midnight.
        run_once()

    while not _stop:
        now = datetime.now(zone)
        target = next_run(now)
        wait = (target - now).total_seconds()
        log(f"next run at {target.isoformat(timespec='seconds')} (in {wait / 3600:.1f}h)")
        # Sleep in slices so a SIGTERM is noticed promptly.
        while wait > 0 and not _stop:
            time.sleep(min(60.0, wait))
            wait -= 60.0
        if _stop:
            break
        run_once()

    log("exiting")


if __name__ == "__main__":
    main()
