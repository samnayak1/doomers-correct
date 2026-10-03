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

# The series starts here, ignoring anything scraped before it. Set this whenever
# a scrape-volume knob changes: RESULTS_WANTED, SEARCH_TERMS, sites or locations
# all shift the LEVEL of the curve without the market having moved, and a model
# fitted across that step reads the instrument change as a trend. Data before
# this date stays in the database, it is just not a comparable measurement.
SERIES_FROM = os.environ.get("SERIES_FROM", "").strip() or None

RESULTS_WANTED = _int("RESULTS_WANTED", 300)         # per (site, term, location)
# 60 saturated every single query - 14 terms as different in size as "software
# engineer" and "iOS developer" all returned exactly 60, which is a ceiling, not
# a measurement. A cap the market cannot fall below makes the whole series
# insensitive to decline, which is the one thing this site exists to show.
# Per-site overrides, RESULTS_WANTED_<SITE>. Indeed answered exactly 300 for the
# biggest terms (software, backend, data, ML, cloud, security) on essentially
# every night while LinkedIn never came close, so one shared ceiling was still a
# ceiling on half the data. Indeed pages are cheap (~7s a query), LinkedIn's are
# not, so only Indeed gets the deeper fetch.
_RESULTS_WANTED_SITE = {"indeed": 1000}


def results_wanted(site: str) -> int:
    return _int(f"RESULTS_WANTED_{site.upper()}",
                _RESULTS_WANTED_SITE.get(site, RESULTS_WANTED))


HOURS_OLD = _int("HOURS_OLD", 72)                    # only recent postings
SCRAPE_PAUSE_SEC = _float("SCRAPE_PAUSE_SEC", 3.0)   # politeness delay between calls
KEEP_DESCRIPTIONS = _bool("KEEP_DESCRIPTIONS", False)  # descriptions are ~90% of the bytes
PROXIES = [p.strip() for p in os.environ.get("PROXIES", "").split(",") if p.strip()] or None


# How far back a listing's posting date may pull the curve before we first saw
# it. Job boards carry listings with very old posting dates - live Indeed results
# routinely include postings over a year old - and without this bound a single
# stale posting stretches the x-axis across years of days we never observed.
RECONSTRUCT_DAYS = _int("RECONSTRUCT_DAYS", 90)

# --- role classification (Gemini) -------------------------------------------- #
# Titles are classified once, by an LLM, and the answer is stored on the row.
# Only rows with no role yet are ever sent, so nightly cost stays flat as the
# table grows rather than scaling with it.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
CLASSIFY_BATCH = _int("CLASSIFY_BATCH", 50)          # titles per request
CLASSIFY_MAX_PER_RUN = _int("CLASSIFY_MAX_PER_RUN", 1500)  # bounds a backfill
CLASSIFY_TIMEOUT = _int("CLASSIFY_TIMEOUT", 60)

# The model is given exactly these and cannot return anything else — they are
# an enum in the response schema. `other` exists so a title that fits none is
# labelled honestly instead of being forced into the nearest wrong bucket.
ROLE_CATEGORIES = [
    "backend", "frontend", "full_stack", "mobile", "web_developer",
    "devops", "sre", "system_engineer", "security", "it_support",
    "data_engineer", "data_scientist", "aiml",
    "embedded_engineer", "firmware_engineer", "electronics_engineer",
    "game_developer", "qa", "forward_deployed_engineer", "architect",
    "product_management", "sales_engineer", "other",
]

# --- contact form ----------------------------------------------------------- #
# A message is accepted only with a valid Cap token (self-hosted proof-of-work
# CAPTCHA), then delivered to you as a Telegram message from your own bot - no
# address on the page, and no email deliverability to fight. The form switches
# itself off unless every value below is set.
CAP_URL = os.environ.get("CAP_URL", "http://cap:3000").rstrip("/")   # internal only
CAP_SITE_KEY = os.environ.get("CAP_SITE_KEY", "").strip()
CAP_SECRET = os.environ.get("CAP_SECRET", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
TELEGRAM_API = os.environ.get("TELEGRAM_API", "https://api.telegram.org").rstrip("/")
CONTACT_PER_IP_HOUR = _int("CONTACT_PER_IP_HOUR", 5)
# Ceiling across everyone, so rotating IPs cannot turn the form into a mailer.
CONTACT_PER_DAY = _int("CONTACT_PER_DAY", 50)

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
