"""AWS Lambda entry point for the nightly scrape.

An alternative to the in-container scheduler, not an addition to it. Lambda has
no persistent disk, so the flow is:

    S3 (jobs.db.gz)  ->  /tmp/jobs.db  ->  scrape  ->  S3 (db + JSON snapshots)

Run this OR the EC2 worker, never both: they would each write the same SQLite
file back to S3 and the later upload would silently discard the other's work.
Set RUN_ON_START=false (or scale the worker to 0) if you switch to Lambda.

Two constraints to size for:
  * 15 minute hard timeout. Measured against live boards, ONE country takes
    about 13 minutes - dominated by LinkedIn, which spends ~43s per request on
    its own rate limiting regardless of SCRAPE_PAUSE_SEC. So invoke this once
    per country (which is what deploy_lambda.sh sets up); country="all" will
    run out of time. If it is still tight, drop LinkedIn via SITES_<COUNTRY>
    or lower RESULTS_WANTED.
  * Job boards rate-limit by IP, and Lambda egress IPs are shared and heavily
    used. Expect a higher failure rate than a stable EC2 IP, and configure
    PROXIES if the boards start refusing.
"""

from __future__ import annotations

import json
import os

# Lambda's filesystem is read-only apart from /tmp.
os.environ.setdefault("DATA_DIR", "/tmp")
os.environ.setdefault("DB_PATH", "/tmp/jobs.db")

from common import config, pipeline, s3store  # noqa: E402


def handler(event, context):
    event = event or {}
    country = str(event.get("country", "all")).lower()
    remaining = (context.get_remaining_time_in_millis() / 1000.0) if context else None

    if not s3store.enabled():
        return {"statusCode": 500,
                "body": json.dumps({"error": "S3_BUCKET is required in Lambda - /tmp is not durable"})}

    restored = s3store.restore_db(config.DB_PATH)
    print(f"[lambda] country={country} restored_db={restored} "
          f"budget={remaining if remaining is None else round(remaining)}s")

    targets = list(config.COUNTRIES) if country == "all" else [config.country_key(country)]
    results = [pipeline.run_country(c) for c in targets]

    # run_country already backs up the database, but do it once more so a partial
    # failure on the last country still persists what the earlier ones collected.
    s3store.backup_db(config.DB_PATH)
    print(f"[lambda] done: {results}")
    return {"statusCode": 200, "body": json.dumps({"results": results}, default=str)}
