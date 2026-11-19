"""Moderate predictions by hand.

    C="docker compose -f docker-compose.yml -f docker-compose.prod.yml"
    $C exec worker python /app/scripts/moderate.py reports        # most-reported first
    $C exec worker python /app/scripts/moderate.py show 42
    $C exec worker python /app/scripts/moderate.py hide 42
    $C exec worker python /app/scripts/moderate.py unhide 42

Hiding takes a prediction off the public list and stops new votes and
reports. The row is kept, and its author's reminder email still goes out.
There is deliberately no HTTP admin endpoint: nothing to log in to, so
nothing to break into.
"""

from __future__ import annotations

import argparse
import sys

sys.path.insert(0, "/app")

from common import config, predictions  # noqa: E402
from common.appdb import connect_app  # noqa: E402
from common.appmodels import Outbox, Prediction, Report, User  # noqa: E402


def show(pid: int) -> int:
    p = Prediction.get_or_none(Prediction.id == pid)
    if not p:
        print(f"no prediction #{pid}")
        return 1
    u = User.get_or_none(User.sub == p.user_sub)
    print(f"#{p.id}  [{p.status}]  by {u.handle if u else '?'}  for {p.target_date}  made {p.created_at}")
    print(f"    \"{p.text}\"")
    print(f"    votes +{p.up} / -{p.down}   reports {p.report_count}")
    for r in Report.select().where(Report.prediction_id == pid).order_by(Report.created_at):
        print(f"    report {r.created_at}  {r.reason or '(no reason)'}")
    o = Outbox.get_or_none(Outbox.prediction_id == pid)
    if o:
        print(f"    reminder {o.status}, due {o.due_at}" + (f" ({o.last_error})" if o.last_error else ""))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Hide or restore predictions.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("show", "hide", "unhide"):
        sub.add_parser(name).add_argument("id", type=int)
    sub.add_parser("reports")
    args = ap.parse_args()

    db = connect_app(config.APP_DB_PATH)
    with db.connection_context():
        if args.cmd == "reports":
            rows = predictions.most_reported()
            if not rows:
                print("no reported predictions")
            for p in rows:
                print(f"#{p.id:<6} {p.report_count:>3} reports  [{p.status}]  {p.text[:90]}")
            return 0
        if args.cmd == "show":
            return show(args.id)
        status = "hidden" if args.cmd == "hide" else "active"
        if not predictions.set_status(args.id, status):
            print(f"no prediction #{args.id}")
            return 1
        print(f"#{args.id} is now {status}")
        return show(args.id)


if __name__ == "__main__":
    sys.exit(main())
