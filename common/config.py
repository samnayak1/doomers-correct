"""Central configuration.  Everything is overridable by environment variable."""

from __future__ import annotations

import os
from pathlib import Path

def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default

def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default

def _bool(name: str, default: bool = False) -> bool:
    return (os.environ.get(name, "") or str(default)).strip().lower() in {"1", "true", "yes", "on"}


# --- storage ---------------------------------------------------------------- #
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB_PATH = Path(os.environ.get("DB_PATH", str(DATA_DIR / "jobs.db")))

# --- AWS / S3 --------------------------------------------------------------- #
AWS_REGION = os.environ.get("AWS_REGION", "ap-south-1")
S3_BUCKET = os.environ.get("S3_BUCKET", "")
S3_PREFIX = os.environ.get("S3_PREFIX", "are-doomers-correct").strip("/")

# Gzip the whole SQLite file to S3 after every run. The compose stack sets this
# to false because Litestream replicates the same database continuously; running
# both uploads the data twice and leaves two restore paths that can disagree.
# Leave it true only if you are running the pipeline without Litestream.
DB_BACKUP_TO_S3 = _bool("DB_BACKUP_TO_S3", True)

# --- countries -------------------------------------------------------------- #
# `indeed` is the jobspy `country_indeed` value; `sites` are the boards to hit.
# `locations` defaults to the country as a whole, and every location is queried
# on every run. That consistency is not incidental - it is what makes the series
# comparable night to night. Querying a rotating subset would make the active
# count swing with the rotation rather than with the market.
#
# Adding metros multiplies the nightly request count (locations x terms x sites),
# so add them only if you have the runtime budget, and never change the set
# without expecting a step change in the curve.
def _locs(env_name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(env_name, "")
    return [x.strip() for x in raw.split("|") if x.strip()] or default


def _sites(env_name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(env_name, "")
    return [x.strip().lower() for x in raw.split(",") if x.strip()] or default


COUNTRIES: dict[str, dict] = {
    "india": {
        "label": "India",
        # `label` names the dropdown option; `prose` is the form that reads
        # correctly inside a sentence ("listings in the United States").
        "prose": "India",
        "indeed": "India",
        "currency": "INR",
        "locations": _locs("LOCATIONS_INDIA", ["India"]),
        # Naukri is the biggest India board but is aggressive about bot
        # detection: it answers 406 "recaptcha required" from most datacentre
        # and many residential IPs, returning zero rows without erroring. It is
        # kept in the defaults because it is the most valuable India source and
        # proxies fix it - watch the per-board warning in the run log.
        "sites": _sites("SITES_INDIA", ["indeed", "naukri", "linkedin"]),
    },
    "australia": {
        "label": "Australia",
        "prose": "Australia",
        "indeed": "Australia",
        "currency": "AUD",
        "locations": _locs("LOCATIONS_AUSTRALIA", ["Australia"]),
        "sites": _sites("SITES_AUSTRALIA", ["indeed", "linkedin"]),
    },
    "usa": {
        "label": "United States",
        "prose": "the United States",
        "indeed": "USA",
        "currency": "USD",
        "locations": _locs("LOCATIONS_USA", ["United States"]),
        # ZipRecruiter is the obvious extra US board and is deliberately not a
        # default: it answers 403 "forbidden" to unproxied traffic. Glassdoor and
        # Google are excluded everywhere for the same class of reason - see the
        # board table in the README. Re-enable any of them via SITES_USA.
        "sites": _sites("SITES_USA", ["indeed", "linkedin"]),
    },
}
DEFAULT_COUNTRY = os.environ.get("DEFAULT_COUNTRY", "india")

# --- what counts as a "tech" role ------------------------------------------- #
SEARCH_TERMS = [t.strip() for t in os.environ.get("SEARCH_TERMS", ",".join([
    "software engineer", "backend developer", "frontend developer",
    "full stack developer", "data engineer", "data scientist",
    "machine learning engineer", "devops engineer", "site reliability engineer",
    "qa automation engineer", "android developer", "ios developer",
    "cloud engineer", "security engineer",
])).split(",") if t.strip()]

# Titles matching this are counted in the "tech" series.  Kept as an explicit,
# auditable regex rather than a model, so the number on the chart is reproducible.
TECH_TITLE_PATTERN = os.environ.get("TECH_TITLE_PATTERN", r"""
    software|developer|engineer|programmer|sde\b|swe\b|devops|sre\b|
    data\s*(scientist|engineer|analyst)|machine\s*learning|\bml\b|\bai\b|
    backend|back[-\s]?end|frontend|front[-\s]?end|full[-\s]?stack|
    android|ios\b|mobile\s*dev|web\s*dev|qa\b|test\s*automation|
    cloud|platform|infrastructure|security|cyber|network\s*engineer|
    architect|\bdba\b|database|python|java\b|javascript|typescript|golang|
    react|node\.?js|kubernetes|\baws\b|azure|\bgcp\b
""")
# Explicitly excluded even if the title matches above (sales/recruiting/etc).
TECH_TITLE_EXCLUDE = os.environ.get("TECH_TITLE_EXCLUDE", r"""
    sales|recruit|talent\s*acquisition|business\s*development|marketing|
    account\s*(manager|executive)|customer\s*success|teacher|trainer|faculty|
    civil\s*engineer|mechanical\s*engineer|electrical\s*engineer|
    chemical\s*engineer|site\s*engineer|safety\s*engineer|nurse|driver
""")

# --- scrape behaviour ------------------------------------------------------- #
RESULTS_WANTED = _int("RESULTS_WANTED", 60)          # per (site, term, location)
HOURS_OLD = _int("HOURS_OLD", 72)                    # only recent postings
SCRAPE_PAUSE_SEC = _float("SCRAPE_PAUSE_SEC", 3.0)   # politeness delay between calls
KEEP_DESCRIPTIONS = _bool("KEEP_DESCRIPTIONS", False)  # descriptions are ~90% of the bytes
PROXIES = [p.strip() for p in os.environ.get("PROXIES", "").split(",") if p.strip()] or None

# --- series construction ---------------------------------------------------- #
# A listing counts as "active" on day t if t is inside its [start, end] window.
# A listing is counted as active on the days between the first and last run that
# saw it, capped at this many days in case a board leaves something up forever.
#
# There is deliberately NO trailing grace period. Extending each listing a few
# days past the last sighting looks harmless, but it biases the recent end of the
# curve: for any day t, a listing qualifies if last_seen + grace >= t, so older
# days always accumulate more qualifying listings than recent ones and the last
# `grace` days slope downward no matter what the market does. Gaps *between*
# sightings need no grace - an interval spanning them already covers them.
ACTIVE_WINDOW_DAYS = _int("ACTIVE_WINDOW_DAYS", 150)

# How far back a listing's posting date may pull the curve before we first saw
# it. Job boards carry listings with very old posting dates - live Indeed results
# routinely include postings over a year old - and without this bound a single
# stale posting stretches the x-axis across years of days we never observed.
RECONSTRUCT_DAYS = _int("RECONSTRUCT_DAYS", 90)

# --- forecast --------------------------------------------------------------- #
FORECAST_UNTIL = os.environ.get("FORECAST_UNTIL", "2027-12-31")
FORECAST_MIN_POINTS = _int("FORECAST_MIN_POINTS", 7)    # below this: no forecast at all
ARIMA_MIN_POINTS = _int("ARIMA_MIN_POINTS", 30)         # "first month" -> linear
# Fit ARIMA on log(1+y): the trend becomes multiplicative (% per month rather
# than N listings per day) and a negative forecast becomes structurally
# impossible. Applied to ARIMA only - compounding a slope estimated from two
# weeks of data over two years is nonsense, so the linear model stays in level
# space (see `log_space and use_arima` in forecast.forecast_series).
FORECAST_LOG_SPACE = _bool("FORECAST_LOG_SPACE", True)
# Damping (Gardner-McKenzie): the trend decays geometrically instead of running
# forever in a straight line.  Over a 2-year horizon this is the single most
# important knob - undamped drift compounds into absurd numbers.  1.0 disables it.
FORECAST_DAMPING = _float("FORECAST_DAMPING", 0.98)
# Fit on a trailing window only.  Early history is dominated by the cold-start
# ramp (we had not been collecting long enough for the active window to fill),
# which looks like explosive growth and is purely an artefact.
FORECAST_FIT_DAYS = _int("FORECAST_FIT_DAYS", 90)

# --- scheduling ------------------------------------------------------------- #
SCRAPE_AT = os.environ.get("SCRAPE_AT", "00:00")        # local time, HH:MM
TZ = os.environ.get("TZ", "Asia/Kolkata")
RUN_ON_START = _bool("RUN_ON_START", True)


def country_key(value: str | None) -> str:
    v = (value or "").strip().lower()
    return v if v in COUNTRIES else DEFAULT_COUNTRY
