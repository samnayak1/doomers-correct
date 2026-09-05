# Are doomers correct?

Active **tech job listings in India, Australia and the United States**, scraped
nightly, charted, and extrapolated to 31 December 2027.

Inspired by [@usr_bin_roygbiv](https://x.com/usr_bin_roygbiv)'s
[site](https://doomersareretardedcommunists.com/), which tracks the United States.
This is an independent rebuild — its own collection, its own storage, its own model.
No affiliation.

Sized to run on a **1 GB EC2 micro**: three containers with hard memory caps totalling
736 MiB, and a launcher that refuses to start if that arithmetic ever stops adding up.
Measured on a live scrape: **worker 131 MiB, api 39 MiB, web 10 MiB** — about 181 MiB
in total, well inside the caps.

---

## Quick start

Two stacks, sharing a base compose file:

| | Web server | TLS | Litestream prefix |
|---|---|---|---|
| `./run.sh dev` | nginx on `$HTTP_PORT` (8080) | none | `LITESTREAM_DEV_S3_PREFIX` |
| `./run.sh prod` | Caddy on 80/443 | automatic (Let's Encrypt) | `LITESTREAM_S3_PREFIX` |

```bash
sudo scripts/setup-swap.sh   # once per host; run.sh refuses to start without swap
cp .env.example .env         # fill in AWS_REGION / S3_BUCKET (or leave S3 blank)
./run.sh dev                 # or: ./run.sh prod
docker compose -f docker-compose.yml -f docker-compose.dev.yml logs -f worker
```

Dev serves on <http://localhost:8080>. `RUN_ON_START=true` means the first scrape
begins immediately rather than waiting for midnight.

For prod, set `SITE_ADDRESS` to your domain and `ACME_EMAIL` to your address.
Caddy issues a certificate on first request, which needs **ports 80 and 443 both
reachable from the internet** — 80 is not optional, it is how the ACME challenge
is answered. Set `SITE_ADDRESS=:80` to skip TLS entirely (e.g. behind a load
balancer that already terminates it).

### Is run.sh mandatory?

No. It is a convenience wrapper around three things that are easy to forget, and
you can always drive compose directly:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
```

What you give up by doing that:

1. **Sequential builds.** `docker compose build` builds services in *parallel* by
   default. On a 1 GiB box that means several `pip install` runs at once, which
   is the usual cause of an OOM-killed deploy. `run.sh` builds one at a time.
2. **The swap check.** Without swap, an OOM takes whatever is largest — often
   sshd, which locks you out of the instance you were deploying to.
3. **Picking the right compose file pair**, so dev never replicates over the
   production Litestream prefix.

None of that is magic; if you would rather run the compose command yourself, do
the builds one service at a time.

### Without Docker

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r worker/requirements.txt -r api/requirements.txt
python -m common.pipeline --country india --no-s3   # one scrape
python scripts/dev_server.py                        # http://127.0.0.1:8080
```

To see the UI before any real data exists:
`python scripts/seed.py --days 150 --reset` fills the database with synthetic rows
(companies named `Example …`, URLs on `example.com`).

## Common operations

`run.sh` deliberately does only build-and-deploy. Everything else is a plain
compose command — set `C` once and the rest are short:

```bash
C="docker compose -f docker-compose.yml -f docker-compose.dev.yml"   # or .prod.yml

$C ps                                             # state
$C logs -f worker                                 # follow the scraper
docker stats --no-stream $($C ps -q)              # live memory vs the caps
$C exec worker python -m common.pipeline --country india   # scrape now
$C exec worker python -m common.pipeline --forecast-only   # refit models only
$C exec worker python /app/scripts/seed.py --days 150 --reset   # synthetic data
$C exec db-replicate litestream snapshots /data/jobs.db    # replication status
$C down                                           # stop (keeps the data volume)
$C down -v                                        # stop AND delete all data
```

`python tests/test_core.py` runs the test suite offline — job boards are stubbed.

## Architecture

```
                    ┌────── worker (Debian slim, 512 MiB) ──────┐
  job boards ──────▶│ scheduler → JobSpy → SQLite → ARIMA → S3  │
  indeed            └───────────────────┬──────────────────────-┘
  linkedin                              │ /data/jobs.db  (docker volume)
  naukri              ┌─────────────────┼─────────────────┐
                      ▼                                   ▼
       ┌────── api (Alpine, 160 MiB) ─────┐   ┌── db-replicate (48 MiB) ──┐
       │ FastAPI, read-only.              │   │ Litestream → S3, every    │
       │ No pandas, no numpy.             │   │ LITESTREAM_SYNC_INTERVAL  │
       └─────────────────┬────────────────┘   └───────────────────────────┘
                         ▼
     dev  ┌── web (nginx, Alpine, 32 MiB) ──┐   plain HTTP on $HTTP_PORT
     prod ┌── caddy (Alpine, 64 MiB) ───────┐   automatic HTTPS on 80/443
          │ static page + /api reverse proxy │
          └──────────────────────────────────┘
```

`docker-compose.yml` holds everything above except the web server; the dev and prod
override files each add one. They are never used alone — `run.sh` picks the pair.

The worker owns the database; the API only reads it (`file:…?mode=ro`). The scheduler
runs the pipeline as a **child process** so pandas' arena memory is returned to the OS
on exit — the container idles around 20 MiB between runs instead of holding the peak.

A fourth container, `db-restore`, runs once at startup and exits. `api` and `worker`
both wait on it via `service_completed_successfully`, so nothing opens the database
until Litestream has had the chance to pull a replica down. That ordering is what
makes a replaced instance come back with its history rather than an empty chart.

### Why the memory numbers are what they are

| Service | Cap | Measured | Why |
|---|---|---|---|
| `worker` | 512 MiB | 131 MiB | During an active scrape. Idles far below it. |
| `api` | 160 MiB | 39 MiB | FastAPI + uvicorn, one process, no scientific stack. |
| `db-replicate` | 48 MiB | — | Litestream tailing the WAL. |
| `web` (dev) | 32 MiB | 12 MiB | nginx serving four static files. |
| `caddy` (prod) | 64 MiB | — | nginx plus TLS termination and ACME. |
| **dev total** | **752 MiB** | | Leaves ~270 MiB on a 1 GB box. |
| **prod total** | **784 MiB** | | Leaves ~240 MiB. |

`db-restore` is capped at 128 MiB but exits before the rest are running, so it does
not count toward either total. Measured figures are from a live scrape; the ones
marked — are the containers added after that run and are not yet measured.

Image sizes: worker 410 MB, api 166 MB, web 74 MB.

### Why the worker is not Alpine

`api` and `web` are Alpine. The worker is Debian slim, and it has to be.

JobSpy depends on `tls-client`, a Go library shipped as a prebuilt c-shared object and
loaded with `ctypes` at import time. Go's c-shared builds use the **initial-exec TLS
model**, and musl's loader cannot relocate initial-exec TLS in a library opened with
`dlopen` after start-up. Every route was tried against a real Alpine build:

| Attempt | Result |
|---|---|
| The wheel's musl build (`tls-client-amd64.so`) | `OSError: free: initial-exec TLS resolves to dynamic definition` |
| The wheel's glibc build + `gcompat` | `OSError: seteuid: initial-exec TLS resolves to dynamic definition` |
| `LD_PRELOAD`, loading at start-up instead of via `dlopen` | segfault |

(The wheel does ship a musl build, and `platform.machine()` on x86-64 contains `"x86"`
so the loader reaches for the glibc `tls-client-x86.so` regardless — but fixing the
file selection only changes *which* TLS error you get.)

The one remaining option would be stubbing `tls_client` out and forcing JobSpy onto
plain `requests`, which loses the TLS fingerprinting Indeed and Glassdoor check —
trading a working scraper for a smaller base image. Not worth it.

The base image is a disk-size question, not a memory one: the measured RSS difference
is a few MB, and the `mem_limit` in `docker-compose.yml` is what actually protects the
box. `worker/Dockerfile` runs `import jobspy` as a build step, so this fails loudly at
build time rather than at midnight in production.

## Data model

SQLite, three tables that matter:

- **`jobs`** — one row per listing per country, with `first_seen` / `last_seen`.
  `first_seen` never moves backwards, so history stays stable as data accumulates.
- **`snapshots`** — one row per country per nightly run. The audit trail behind the
  chart, exposed at `/api/history`.
- **`forecasts`** — the cached model output, refit after every scrape.

### What "active" means

A listing is counted as active on every day between the first and last run that saw
it:

```
start = min(date_posted, first_seen)             # when the listing went live
start = max(start, first_seen - RECONSTRUCT_DAYS) # bound the pre-launch tail
end   = min(last_seen, today)                    # when we last saw it
start = max(start, end - ACTIVE_WINDOW_DAYS)     # cap a listing's lifetime
```

The last two lines are load-bearing. Boards carry postings with very old dates — a
live Indeed page routinely returns listings over a year old, and one real scrape found
a still-live posting dated January 2024. Capping the window against `start` ends that
listing's interval in mid-2024: it invents activity on days nobody observed and omits
the listing from today, when we actually saw it. The cap belongs on the start, so an
interval always ends where the evidence does. `RECONSTRUCT_DAYS` separately bounds how
far a posting date may stretch the x-axis — without it, that one 2024 row made the
chart 963 days wide on the first day of collection.

There is deliberately **no trailing grace period**. Keeping a listing counted for a
few days past its last sighting sounds harmless and is not: for any day `t` a listing
qualifies if `last_seen + grace >= t`, so days further in the past always accumulate
more qualifying listings than recent ones. Under partial observation the last `grace`
days then slope downward on their own, regardless of the market — and the model,
refit nightly on exactly that stretch, learns a decline that was never there. Gaps
*between* sightings need no grace: an interval spanning them already covers them.
`tests/test_core.py::test_series_is_unbiased_at_the_right_edge` pins this down.

For the same reason **every run issues the same set of queries**. Sampling a rotating
subset of metros would be cheaper, but the active count would then move with the
rotation rather than with the market. If you change `LOCATIONS_*`, expect a step
change in the level of the curve.

### The shaded region on the chart

Days before the first scrape are reconstructed from `date_posted`, so the chart has
shape from day one instead of a single dot. Only listings that were **still live**
when we first looked can be reconstructed, so that stretch understates the past and
slopes upward artificially. It is drawn shaded, labelled, and **never used to fit the
model** — the fit window starts at the first real observation.

## The forecast

| History collected | Model |
|---|---|
| < 7 days | none — the chart shows observed history only |
| 7 – 29 days | ordinary least squares on time, with a prediction interval |
| ≥ 30 days | non-seasonal ARIMA(p, d, q) |

ARIMA is estimated by **Hannan–Rissanen** (a long AR fit for the residual proxy, then
a regression on lagged values and lagged residuals) and selected over `p, q ∈ 0..3` by
**AICc**; `d` is chosen by a lag-1 autocorrelation rule with an over-differencing
guard. Prediction intervals come from the MA(∞) weights of the integrated process:
`var(h) = σ² · Σψ²`. It is implemented in ~280 lines of **pure numpy** — statsmodels
and scipy would add roughly 120 MB to an image with a 512 MiB budget, for a small
slice of their functionality.

Two choices matter more than the model family over a 482-day horizon:

- **Log space** (`FORECAST_LOG_SPACE=true`). ARIMA is fitted on `log(1+y)`, so the
  trend is multiplicative (a % per month, not N listings per day) and a negative
  forecast is structurally impossible rather than merely clipped. The *linear* model
  is deliberately left in level space — "simple linear regression" should draw a
  straight line, and compounding a slope estimated from two weeks of data over two
  years produces nonsense.
- **Damping** (`FORECAST_DAMPING=0.98`). The trend decays geometrically instead of
  running forever, converging to `last + slope · φ/(1−φ)` — about 49 further days of
  trend at φ=0.98, whatever horizon you ask for. Undamped, an early cold-start ramp
  compounded to **2.5 million** listings by the end of 2027 from a base of 7,900.
  That number was the reason this knob exists. Set `1.0` to disable.

The model is also fitted on a **trailing window** (`FORECAST_FIT_DAYS=90`), because
early history is dominated by the cold-start ramp — the active window had not yet
filled — which looks like explosive growth and is purely an artefact.

### Is ARIMA the right choice here?

Honestly: it is a reasonable one, and it is not the one I would pick first.

Over a two-year horizon the model family barely matters — the forecast is dominated
by the drift term and how fast you damp it, which is why damping moved the answer by
two orders of magnitude and the choice of `p` and `q` moved it by a few percent.
Given that, ranked:

1. **Damped-trend ETS (Holt's linear method with φ)** — this is what the M3/M4
   competitions found hardest to beat at long horizons, it is simpler than ARIMA, and
   damping is *intrinsic* to it rather than bolted on. Roughly 40 lines here.
2. **ARIMA on log scale with damping** — what is implemented. Comparable accuracy;
   more machinery, and more ways to pick a silly order on short, noisy data.
3. **Theta** — near-identical to damped SES with drift, trivial to implement, and
   very hard to beat on monthly-ish business series.

Where a seasonal model would genuinely win is the *short* horizon: job postings have a
strong weekly cycle (Monday floods, weekend droughts) plus quarterly hiring rhythm.
If you care about the next 30 days more than about December 2027, the upgrade worth
making is **STL decomposition + a damped trend on the seasonally-adjusted series**,
not a better long-run extrapolator. Seasonality was excluded here because the brief
asked for non-seasonal ARIMA and because a weekly cycle contributes essentially
nothing to where the line lands in 2027.

And the caveat that outranks all of this: **this is an extrapolation, not a
prediction.** It assumes the recent pattern continues, knows nothing about funding,
policy, layoffs or hiring cycles, and its interval covers only the model's own noise —
not the chance that the model is wrong.

## Storage and S3

Local SQLite is the working store. If `S3_BUCKET` is set, every run also publishes
the derived JSON, and Litestream replicates the database itself continuously:

```
s3://$S3_BUCKET/$S3_PREFIX/
  manifest.json                 index of everything below
  series/{country}.json         daily series + forecast + summary
  latest/{country}.json         current listings
  raw/{country}/YYYY-MM-DD.json.gz   that night's full scrape

s3://$S3_BUCKET/$LITESTREAM_S3_PREFIX/      # prod: the live database replica
s3://$S3_BUCKET/$LITESTREAM_DEV_S3_PREFIX/  # dev
```

With `S3_BUCKET` empty the whole S3 layer degrades to no-ops — `db-restore` exits
cleanly, `db-replicate` idles, and everything still works on local SQLite.

### Backups: Litestream

`db-replicate` runs [Litestream](https://litestream.io), which tails the SQLite WAL
and ships frames to S3 every `LITESTREAM_SYNC_INTERVAL` (5m). Worst-case data loss
is one interval, rather than one night with a nightly file copy. A full snapshot is
taken daily and a week is retained, so a restore can land at any point in that window.

This **replaces** the nightly gzip-the-whole-file backup that `s3store.backup_db()`
does. The compose files set `DB_BACKUP_TO_S3=false` on the worker for that reason:
running both uploads the same data twice and leaves two restore paths that can
disagree about which is current. Set it `true` only if you run the pipeline
standalone, without the `db-replicate` container.

**Restoring by hand** (the automatic path is `db-restore`, which only fires when
there is no local database):

```bash
C="docker compose -f docker-compose.yml -f docker-compose.prod.yml"
$C stop worker api                       # nothing may write during a restore
$C run --rm db-replicate litestream restore -o /data/jobs.db /data/jobs.db
$C start api worker
```

Add `-timestamp 2026-09-05T00:00:00Z` to restore to a point in time.
`litestream snapshots /data/jobs.db` lists what is available.

**Dev and prod must not share a prefix.** They are separate keys in `.env`, and the
compose override files wire the right one in. Pointing both at the same path means
two Litestream instances replicating different databases over each other.

**Version pin.** The image is pinned to `litestream:0.3.13` and `litestream/litestream.yml`
is written in 0.3 syntax. Litestream 0.5 rewrote the config — `replicas:` (list) became
`replica:` (single), `bucket`/`path`/`region` collapsed into one `url:`, and
`sync-interval` was replaced by compaction `levels:`. Bumping the tag without
rewriting that file will fail to start.

**Credentials.** On EC2, attach an instance role and leave `AWS_ACCESS_KEY_ID` /
`AWS_SECRET_ACCESS_KEY` blank — both boto3 and Litestream pick the role up
automatically. Keys in `.env` are for local development. `.env` is gitignored;
`.env.example` is the template.

The bucket policy needs `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject` (Litestream
expires old WAL segments) on `arn:aws:s3:::BUCKET/*`, plus `s3:ListBucket` on the
bucket. Making the derived JSON publicly readable is optional — it is what lets the
page's "raw data" links work for visitors. **Do not make the Litestream prefix
public**; it is your database.

## Scheduling

**Default: in the worker container.** `worker/scheduler.py` sleeps until `SCRAPE_AT`
in `TZ` (00:00 Asia/Kolkata by default) and runs the pipeline. No AWS wiring, no
timeout ceiling, and a stable egress IP — which job boards treat far better than a
shared one.

Measured pace per request on a live run: **Indeed ~5s, Naukri ~3s (while blocked),
LinkedIn ~43s.** LinkedIn dominates, and its delay is its own rate limiting rather
than `SCRAPE_PAUSE_SEC`, so lowering the politeness delay will not speed things up
much. India (three boards) measured 759s; Australia and the US run two boards each.
Budget roughly 10–13 minutes per country, so about 35 minutes for all three.

**Alternative: Lambda.** `scripts/deploy_lambda.sh` builds a container image, pushes
it to ECR, creates the function with a scoped execution role, and adds an EventBridge
rule at 18:30 UTC (= 00:00 IST):

```bash
FUNCTION=are-doomers-correct-scraper ./scripts/deploy_lambda.sh
```

It creates **one rule per country**, staggered 30 minutes apart — not a single
`country=all` invocation. Measured against live boards, one country takes 10–13
minutes, so all three in one invocation would blow Lambda's 15-minute ceiling;
staggering also stops invocations racing to write the same SQLite file back to S3.

Run **one or the other, never both** — they would each write the same file back and
the later upload would silently discard the other's work. If you switch to Lambda,
`docker compose stop worker`. Also note Lambda's shared egress IPs get challenged by
job boards far more often than a stable EC2 address does.

## Configuration

Everything is environment variables; see `.env.example` for the annotated list.
The ones worth knowing:

| Variable | Default | Notes |
|---|---|---|
| `SCRAPE_AT` / `TZ` | `00:00` / `Asia/Kolkata` | When the nightly run fires |
| `RESULTS_WANTED` | `60` | Per (site, term, location). Main driver of runtime |
| `LOCATIONS_INDIA` | `India` | `\|`-separated. More locations = more requests |
| `SITES_USA` | `indeed,linkedin` | Per-country board list; `SITES_INDIA` etc. |
| `HOURS_OLD` | `72` | Only postings newer than this |
| `SCRAPE_PAUSE_SEC` | `3` | Politeness delay between requests |
| `KEEP_DESCRIPTIONS` | `false` | Descriptions are ~90% of the bytes |
| `PROXIES` | — | Comma-separated. Add these if boards start refusing |
| `ACTIVE_WINDOW_DAYS` | `150` | Cap on how long a listing counts |
| `RECONSTRUCT_DAYS` | `90` | How far back a posting date may stretch the chart |
| `ARIMA_MIN_POINTS` | `30` | Linear below this, ARIMA above |
| `FORECAST_DAMPING` | `0.98` | Read the forecast section before changing |

`SEARCH_TERMS` and `TECH_TITLE_PATTERN` define what counts as a tech role. The
classifier is an explicit, auditable regex rather than a model, so the number on the
chart is reproducible — `tests/test_core.py` pins its behaviour on known titles.

## API

| Endpoint | Returns |
|---|---|
| `GET /api/countries` | Available countries for the dropdown |
| `GET /api/series?country=&tech=` | Daily series, forecast, summary |
| `GET /api/jobs?country=&q=&site=&remote=&sort=&limit=&offset=` | Listings table |
| `GET /api/history?country=` | Past scrape runs |
| `GET /api/data` | S3 links for the raw files |
| `GET /api/health` | Liveness |
| `GET /api/docs` | OpenAPI browser |

## Deploying on EC2 micro

```bash
sudo yum install -y docker git && sudo systemctl enable --now docker
sudo usermod -aG docker ec2-user && newgrp docker
git clone https://github.com/samnayak1/doomers-correct.git && cd doomers-correct
sudo scripts/setup-swap.sh            # required: run.sh refuses to start without swap
cp .env.example .env && $EDITOR .env  # set SITE_ADDRESS, ACME_EMAIL, S3_BUCKET
./run.sh prod
```

Open **80 and 443** in the security group. Both are needed: 443 serves the site, 80
answers Caddy's ACME challenge, so certificate issuance fails silently if it is shut.
Point your domain's A record at the instance before the first `./run.sh prod` — Caddy
requests a certificate on the first request for `SITE_ADDRESS`, and Let's Encrypt
rate-limits repeated failures.

Attach an instance role with the S3 permissions above rather than putting keys in
`.env`. The `caddy_data` volume holds the issued certificates; do not prune it.

## Layout note

`run.sh` is intentionally a build-and-deploy script and nothing else — see
[Common operations](#common-operations) for the compose one-liners that replaced its
old subcommands.

## Caveats

- **Coverage is a sample, not a census.** `RESULTS_WANTED` caps each query, boards
  paginate differently, and a run can partially fail. The series tracks a consistent
  sample over time, which is what makes the *shape* meaningful — the absolute level is
  not "all tech jobs in India".
- **Cross-board duplicates** are deduplicated by JobSpy's `id`, which is per-board. The
  same role posted to Naukri and LinkedIn counts twice.
- **Changing `SEARCH_TERMS`, `LOCATIONS_*` or `RESULTS_WANTED` breaks comparability**
  and puts a step in the curve. Change them early or not at all.
- **Scraping is subject to each board's terms.** The politeness delay defaults to 3s;
  use proxies rather than lowering it.
- **Boards block, and they block quietly.** Measured on a live run from one
  residential IP:

  Every one of the six boards JobSpy supports here was tested live from one
  unproxied residential IP:

  | Board | Result | In defaults? |
  |---|---|---|
  | Indeed | 60 rows on every query, ~5s each | yes |
  | LinkedIn | 60 rows on every query, ~43s each (its own rate limiting) | yes |
  | Naukri | **0 rows** — HTTP 406 `recaptcha required` | yes, India only |
  | Google | **0 rows** — JobSpy `initial cursor not found` | no |
  | Glassdoor | **0 rows** — HTTP 400 `location not parsed`, at every location tried | no |
  | ZipRecruiter | **0 rows** — HTTP 403 `forbidden` | no |

  **Only two of six actually work unproxied.** All four failures return *success* at
  the call level and simply hand back nothing, which is why the pipeline logs an
  explicit `WARNING no rows from …` line for any board that contributes nothing — a
  silently dead source is worse than a loud one.

  Naukri is IP reputation, so proxies fix it, and it is the most valuable India source
  — it stays in the defaults so the warning keeps reminding you. The other three are
  excluded from the defaults rather than shipped broken: Google and Glassdoor are
  JobSpy scrapers failing to parse the page (config cannot fix those), ZipRecruiter is
  a hard IP block. Re-enable any of them with `SITES_INDIA` / `SITES_AUSTRALIA` /
  `SITES_USA` if they behave better from your network. **Budget for proxies.**

## Troubleshooting

**`error getting credentials - err: exit status 1` during a build (WSL).**
Docker Desktop sets `"credsStore": "desktop.exe"` in `~/.docker/config.json`, and that
helper is not reliably reachable from inside a WSL distro. Build with a config that
does not use it:

```bash
mkdir -p /tmp/dockercfg && echo '{}' > /tmp/dockercfg/config.json
DOCKER_CONFIG=/tmp/dockercfg ./run.sh dev
```

**`docker: command not found`, or the daemon is unreachable in WSL.**
Docker Desktop → Settings → Resources → WSL Integration → enable your distro →
Apply & Restart. If the socket still does not appear, `wsl --shutdown` from PowerShell
and reopen the terminal. You want `/var/run/docker.sock` owned by `root:docker`.

**A board returns nothing.** Look for `WARNING no rows from …` in the worker logs
(`$C logs -f worker`). Naukri in particular answers HTTP 406 `recaptcha required` from most
datacentre and many residential IPs — it succeeds at the HTTP level and hands back
zero rows. Set `PROXIES`, or drop the board with `SITES_INDIA` / `SITES_AUSTRALIA`.

**The chart is a hockey stick on day one.** Expected, and it is labelled. Until the
first scrape completes the entire curve is reconstructed from posting dates, and we
can only reconstruct listings that were still live when we first looked — so the past
is understated and the curve ramps into today. The shaded `RECONSTRUCTED` region marks
it, and none of it is ever fed to the model.

**The first run takes ~35 minutes for all three countries.** Almost all of it is LinkedIn
(~43s per request, its own rate limiting). Set `RUN_ON_START=false` if you would
rather wait for midnight.

## Layout

```
common/                    config, SQLite, JobSpy wrapper, forecasting, S3, pipeline
worker/                    nightly scheduler + Debian-slim image (see "Why the worker is not Alpine")
api/                       FastAPI read-only JSON API + Alpine image
web/                       static page (hand-rolled SVG chart); nginx + Caddy images
caddy/Caddyfile            production TLS front door
litestream/litestream.yml  continuous SQLite replication to S3
lambda/                    optional Lambda handler + image
scripts/                   seed data, dev server, swap setup, Lambda deploy
tests/                     offline test suite
docker-compose.yml         base stack (worker, api, litestream)
docker-compose.dev.yml     + nginx, dev replication prefix
docker-compose.prod.yml    + Caddy, prod replication prefix
run.sh                     build (one service at a time) + deploy
```
