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


DB_BACKUP_TO_S3 = _bool("DB_BACKUP_TO_S3", True)


def _locs(env_name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(env_name, "")
    return [x.strip() for x in raw.split("|") if x.strip()] or default


def _sites(env_name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(env_name, "")
    return [x.strip().lower() for x in raw.split(",") if x.strip()] or default


COUNTRIES: dict[str, dict] = {
    "india": {
        "label": "India",

        "prose": "India",
        "indeed": "India",
        "currency": "INR",
        "locations": _locs("LOCATIONS_INDIA", ["India"]),

        # Naukri is deliberately absent: it answers 406 "recaptcha required"
        # from every IP tried, succeeding at the HTTP level while returning zero
        # rows. Add it back via SITES_INDIA once you have proxies.
        "sites": _sites("SITES_INDIA", ["indeed", "linkedin"]),
    },
    "usa": {
        "label": "United States",
        "prose": "the United States",
        "indeed": "USA",
        "currency": "USD",
        "locations": _locs("LOCATIONS_USA", ["United States"]),

        "sites": _sites("SITES_USA", ["indeed", "linkedin"]),
    },
}
DEFAULT_COUNTRY = os.environ.get("DEFAULT_COUNTRY", "india")


SEARCH_TERMS = [t.strip() for t in os.environ.get("SEARCH_TERMS", ",".join([
    "software engineer", "backend developer", "frontend developer",
    "full stack developer", "data engineer", "data scientist",
    "machine learning engineer", "devops engineer", "site reliability engineer",
    "qa automation engineer", "android developer", "ios developer",
    "cloud engineer", "security engineer",
])).split(",") if t.strip()]


TECH_TITLE_PATTERN = os.environ.get("TECH_TITLE_PATTERN", r"""
    software|developer|engineer|programmer|sde\b|swe\b|devops|sre\b|
    data\s*(scientist|engineer|analyst)|machine\s*learning|\bml\b|\bai\b|
    backend|back[-\s]?end|frontend|front[-\s]?end|full[-\s]?stack|
    android|ios\b|mobile\s*dev|web\s*dev|qa\b|test\s*automation|
    cloud|platform|infrastructure|security|cyber|network\s*engineer|
    architect|\bdba\b|database|python|java\b|javascript|typescript|golang|
    react|node\.?js|kubernetes|\baws\b|azure|\bgcp\b
""")

TECH_TITLE_EXCLUDE = os.environ.get("TECH_TITLE_EXCLUDE", r"""
    sales|recruit|talent\s*acquisition|business\s*development|marketing|
    account\s*(manager|executive)|customer\s*success|teacher|trainer|faculty|
    civil\s*engineer|mechanical\s*engineer|electrical\s*engineer|
    chemical\s*engineer|site\s*engineer|safety\s*engineer|nurse|driver
""")

RESULTS_WANTED = _int("RESULTS_WANTED", 60)          # per (site, term, location)
HOURS_OLD = _int("HOURS_OLD", 72)                    # only recent postings
SCRAPE_PAUSE_SEC = _float("SCRAPE_PAUSE_SEC", 3.0)   # politeness delay between calls
KEEP_DESCRIPTIONS = _bool("KEEP_DESCRIPTIONS", False)  # descriptions are ~90% of the bytes
PROXIES = [p.strip() for p in os.environ.get("PROXIES", "").split(",") if p.strip()] or None


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
