#!/usr/bin/env python3
"""Latency percentiles and error rates from Caddy's JSON access log.

Run on the server, piping in whatever window you want to look at:

    docker logs --since 24h job-doomers-caddy-1 2>&1 | python3 scripts/http_stats.py
    docker logs --since 1h  job-doomers-caddy-1 2>&1 | python3 scripts/http_stats.py

Standard library only, so it runs on the host's python3 with nothing installed.
Lines that are not access-log entries (Caddy's own startup and TLS messages)
are skipped.
"""

from __future__ import annotations

import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone


def group(uri: str) -> str:
    """Bucket requests so the API stands apart from static files."""
    path = uri.split("?", 1)[0]
    if path.startswith("/api/"):
        return "/api/" + path[5:].split("/", 1)[0]
    if path.startswith("/cap/"):
        return "/cap (captcha)"
    return "page + static"


def pct(sorted_ms: list[float], p: float) -> float:
    """Nearest-rank percentile: a value that actually occurred, never interpolated."""
    return sorted_ms[max(0, math.ceil(p / 100 * len(sorted_ms)) - 1)]


def main() -> int:
    durations: dict[str, list[float]] = defaultdict(list)
    status_by_group: dict[str, Counter] = defaultdict(Counter)
    errors_5xx: Counter = Counter()
    errors_4xx: Counter = Counter()
    first = last = None

    for line in sys.stdin:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not str(e.get("logger", "")).startswith("http.log.access"):
            continue
        req = e.get("request") or {}
        uri, status = req.get("uri", ""), int(e.get("status") or 0)
        g = group(uri)
        ms = float(e.get("duration") or 0) * 1000
        durations[g].append(ms)
        durations["ALL"].append(ms)
        status_by_group[g][status // 100] += 1
        path = uri.split("?", 1)[0]
        if status >= 500:
            errors_5xx[(status, path)] += 1
        elif status >= 400:
            errors_4xx[(status, path)] += 1
        ts = e.get("ts")
        if isinstance(ts, (int, float)):
            first = ts if first is None else min(first, ts)
            last = ts if last is None else max(last, ts)

    total = len(durations["ALL"])
    if not total:
        print("No access-log entries found. Is the Caddyfile deployed with the JSON")
        print("access log, and has the site had any traffic in this window?")
        return 1

    fmt_ts = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M")
    n5, n4 = sum(errors_5xx.values()), sum(errors_4xx.values())
    print(f"{total:,} requests   {fmt_ts(first)} -> {fmt_ts(last)} UTC")
    print(f"server errors (5xx): {n5:,} ({100 * n5 / total:.2f}%)   "
          f"client errors (4xx): {n4:,} ({100 * n4 / total:.2f}%)")
    print()

    print(f"{'latency (ms)':<22}{'count':>8}{'p50':>9}{'p95':>9}{'p99':>9}{'max':>9}{'5xx':>7}")
    order = ["ALL"] + sorted((g for g in durations if g != "ALL"),
                             key=lambda g: -len(durations[g]))
    for g in order:
        d = sorted(durations[g])
        fives = sum(status_by_group[x][5] for x in status_by_group) if g == "ALL" else status_by_group[g][5]
        print(f"{g:<22}{len(d):>8,}{pct(d, 50):>9.1f}{pct(d, 95):>9.1f}"
              f"{pct(d, 99):>9.1f}{d[-1]:>9.1f}{fives:>7}")

    # 5xx is what matters: the site failing. 4xx is mostly bots probing for
    # /wp-admin and the like, listed so a real broken link stands out.
    for title, c in (("Top server errors", errors_5xx), ("Top client errors", errors_4xx)):
        if c:
            print(f"\n{title}:")
            for (status, path), n in c.most_common(8):
                print(f"  {n:>6,}  {status}  {path[:70]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
