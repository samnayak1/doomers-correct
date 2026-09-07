# Are doomers correct?

Active **tech job listings in India and the United States**, scraped
nightly from public job boards, charted, and extrapolated to 31 December 2027.

Inspired by [@usr_bin_roygbiv](https://x.com/usr_bin_roygbiv)'s
[site](https://doomersareretardedcommunists.com/), which tracks the United States.
Independent rebuild — its own collection, storage and model. No affiliation.

A worker scrapes via [JobSpy](https://github.com/speedyapply/JobSpy) into SQLite,
Litestream replicates that to S3, a read-only FastAPI serves it, and a static page
draws the chart. Sized for a 1 GB EC2 micro: caps total 752 MiB (dev) / 784 MiB
(prod), measured 181 MiB under an active scrape.

## Setup

```bash
sudo scripts/setup-swap.sh   # once per host; run.sh refuses to start without swap
cp .env.example .env         # then edit it
./run.sh dev                 # nginx on $HTTP_PORT (8080), no TLS
./run.sh prod                # Caddy on 80/443, automatic HTTPS
```

`run.sh` is only build-and-deploy. It exists to do three things you would
otherwise have to remember: build services **one at a time** (compose builds in
parallel by default, which OOM-kills a 1 GB box), require swap, and select the
right compose file pair. Driving compose yourself is fine — just build serially.

**For prod**, set `SITE_ADDRESS` to your domain and `ACME_EMAIL` to your address,
point the domain's A record at the instance, and open **both 80 and 443**. Caddy
answers the ACME challenge on 80, so certificates fail silently if it is closed.
Set `SITE_ADDRESS=:80` to skip TLS.

Leave `S3_BUCKET` empty to run entirely on local SQLite — publishing, replication
and restore all degrade to no-ops and everything else still works.

### Settings worth knowing

| Variable | Default | Notes |
|---|---|---|
| `SCRAPE_AT` / `TZ` | `00:00` / `Asia/Kolkata` | When the nightly run fires |
| `RUN_ON_START` | `true` | Scrape immediately on boot |
| `RESULTS_WANTED` | `60` | Per (site, term, location); main driver of runtime |
| `PROXIES` | — | Needed for Naukri; most boards block unproxied IPs |
| `SITES_INDIA` etc. | `indeed,linkedin` (+`naukri`) | Per-country board list |
| `LITESTREAM_SYNC_INTERVAL` | `5m` | Worst-case data loss window |
| `ARIMA_MIN_POINTS` | `30` | Linear regression below this, ARIMA above |
| `FORECAST_DAMPING` | `0.98` | `1.0` makes the 2-year forecast explode |

Full annotated list in `.env.example`.

## Common operations

```bash
C="docker compose -f docker-compose.yml -f docker-compose.dev.yml"   # or .prod.yml

$C ps                                                    # state
$C logs -f worker                                        # follow the scraper
docker stats --no-stream $($C ps -q)                     # memory vs the caps
$C exec worker python -m common.pipeline --country india # scrape now
$C exec worker python -m common.pipeline --forecast-only # refit models only
$C exec worker python /app/scripts/seed.py --days 150 --reset   # synthetic data
$C exec db-replicate litestream snapshots /data/jobs.db  # replication status
$C down                                                  # stop, keep data
$C down -v                                               # stop AND delete data
```

Tests run offline (boards stubbed): `pip install -r worker/requirements.txt`
then `python tests/test_core.py`.

Restore is automatic on a fresh instance. By hand: stop `worker` and `api`, then
`$C run --rm db-replicate litestream restore -o /data/jobs.db /data/jobs.db`,
optionally with `-timestamp 2026-09-05T00:00:00Z`.

## API

`/api/series?country=` · `/api/jobs?country=&q=&site=&remote=&sort=` ·
`/api/history?country=` · `/api/countries` · `/api/data` · `/api/health` ·
`/api/docs`

## Before you change things

Each of these is load-bearing and explained where it lives:

- **The worker is Debian, not Alpine.** JobSpy's `tls-client` cannot load under
  musl. Every workaround was tried — `worker/Dockerfile`.
- **Litestream is pinned to 0.3.13.** 0.5 rewrote the config format; bumping the
  tag without rewriting the file fails to start — `litestream/litestream.yml`.
- **Listing intervals cap against the end, not the start, and have no trailing
  grace.** Both alternatives silently bias the chart — `common/db.py`,
  `common/config.py`.
- **The forecast is damped and fitted in log space** on observed days only.
  Undamped it reached 2.5M listings — `common/forecast.py`.
- **ARIMA is ~340 lines of pure numpy, not statsmodels.** Not a resource limit:
  scipy costs +71 MB RSS and statsmodels +135 MB, both of which fit a 512 MB
  worker cap and a 16 GB volume. It buys no difference to the chart — the line
  is set by drift and damping, not by swapping Hannan-Rissanen for exact MLE.
  Where statsmodels *would* earn its place is seasonality: postings have a
  strong weekly cycle, and SARIMAX or STL would materially improve the
  next-30-days forecast. It does nothing for the 2027 number.
- **Board defaults are only what actually returns rows.** Of six JobSpy supports,
  four fail silently unproxied; the pipeline warns when one contributes nothing —
  `common/config.py`.
- **Changing `SEARCH_TERMS`, `LOCATIONS_*` or `RESULTS_WANTED` breaks
  comparability** and puts a step in the curve.

This is an extrapolation, not a prediction: it assumes the recent pattern
continues and knows nothing about hiring cycles, funding or layoffs.

## Layout

```
common/     config, SQLite, JobSpy wrapper, forecasting, S3, pipeline
worker/     nightly scheduler + image        api/     read-only JSON API + image
web/        static page; nginx + Caddy       caddy/   production TLS config
litestream/ replication config               lambda/  optional Lambda scheduler
scripts/    seed data, swap setup, deploy    tests/   offline suite
docker-compose{,.dev,.prod}.yml              run.sh   serial build + deploy
```
