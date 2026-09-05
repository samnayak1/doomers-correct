#!/usr/bin/env bash
# Are doomers correct? - operator script.
#
# Wraps docker compose with a hard memory budget check, because the target box
# is a 1 GB EC2 micro and an OOM there takes down sshd, not just the app.
set -Eeuo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

# Total the containers are allowed to reserve, in MiB. The rest of the gigabyte
# belongs to the kernel, dockerd and your shell.
MEM_BUDGET_MB="${MEM_BUDGET_MB:-800}"
COMPOSE_FILE=docker-compose.yml

c_red=$'\033[31m'; c_grn=$'\033[32m'; c_yel=$'\033[33m'; c_dim=$'\033[2m'; c_off=$'\033[0m'
info() { printf '%s==>%s %s\n' "$c_grn" "$c_off" "$*"; }
warn() { printf '%s[warn]%s %s\n' "$c_yel" "$c_off" "$*" >&2; }
die()  { printf '%s[fail]%s %s\n' "$c_red" "$c_off" "$*" >&2; exit 1; }

compose() {
  if docker compose version >/dev/null 2>&1; then docker compose "$@"
  elif command -v docker-compose >/dev/null 2>&1; then docker-compose "$@"
  else die "docker compose is not installed"; fi
}

require_env() {
  if [[ ! -f .env ]]; then
    [[ -f .env.example ]] || die "neither .env nor .env.example found"
    cp .env.example .env
    warn "created .env from .env.example - fill in your AWS values before the first scrape"
    return
  fi
  # An empty or truncated .env passes `-f` and then every service silently runs
  # on code defaults, which is a confusing way to lose an afternoon.
  [[ -s .env ]] || die ".env exists but is empty. Run: cp .env.example .env"
  local missing=()
  for key in HTTP_PORT SCRAPE_AT TZ FORECAST_UNTIL; do
    grep -qE "^${key}=" .env || missing+=("$key")
  done
  if (( ${#missing[@]} )); then
    warn ".env is missing ${missing[*]} - those will fall back to built-in defaults."
    warn "Compare against .env.example if that was not deliberate."
  fi
  if grep -qE '^S3_BUCKET=.+' .env; then
    grep -qE '^AWS_REGION=.+' .env || warn "S3_BUCKET is set but AWS_REGION is empty."
  else
    info "S3_BUCKET is empty - running on local SQLite only (no publishing or restore)."
  fi
}

# ── the budget check ──────────────────────────────────────────────────────── #
# Sums every mem_limit in the compose file and refuses to start if the total
# exceeds MEM_BUDGET_MB. Guards against someone bumping a limit and only finding
# out when the OOM killer arrives at 3am.
check_memory() {
  local total=0 line val unit mb
  while read -r line; do
    val="${line//[!0-9]/}"
    unit="${line//[0-9 ]/}"
    [[ -z "$val" ]] && continue
    case "${unit,,}" in
      *g*) mb=$(( val * 1024 )) ;;
      *m*) mb=$(( val )) ;;
      *k*) mb=$(( val / 1024 )) ;;
      *)   mb=$(( val / 1048576 )) ;;
    esac
    total=$(( total + mb ))
  done < <(grep -E '^\s*mem_limit:' "$COMPOSE_FILE" | sed 's/.*mem_limit:\s*//')

  info "container memory ceiling: ${total} MiB (budget ${MEM_BUDGET_MB} MiB)"
  (( total <= MEM_BUDGET_MB )) || die "mem_limit total ${total} MiB exceeds MEM_BUDGET_MB=${MEM_BUDGET_MB}. Lower a limit in ${COMPOSE_FILE}, or raise MEM_BUDGET_MB deliberately."

  local host_mb swap_mb
  host_mb=$(free -m 2>/dev/null | awk '/^Mem:/{print $2}' || echo 0)
  swap_mb=$(free -m 2>/dev/null | awk '/^Swap:/{print $2}' || echo 0)
  if (( host_mb > 0 )); then
    printf '%s    host RAM %s MiB, swap %s MiB%s\n' "$c_dim" "$host_mb" "$swap_mb" "$c_off"
    if (( host_mb < total + 200 )); then
      warn "host RAM (${host_mb} MiB) leaves under 200 MiB for the OS."
      (( swap_mb < 512 )) && warn "no meaningful swap configured. A scrape can spike; run './run.sh swap' to add a 2 GiB swapfile."
    fi
  fi

  # cgroup v2 is what actually enforces mem_limit; without it Docker silently ignores it.
  [[ -f /sys/fs/cgroup/cgroup.controllers ]] || warn "cgroup v2 not detected - mem_limit may not be enforced on this host."
}

usage() {
  cat <<'USAGE'
Usage: ./run.sh <command>

  up            Build if needed, verify the memory budget, start everything
  down          Stop and remove the containers (the data volume is kept)
  restart       Restart all services
  build         Rebuild images without starting
  logs [svc]    Follow logs (all services, or one)
  status        Show container state and live memory use
  scrape [c]    Run one scrape immediately (c = india | australia | all)
  forecast      Re-fit the models without scraping
  seed [days]   Fill the database with synthetic data for a UI preview
  backup        Push the SQLite file to S3 now
  shell [svc]   Open a shell in a running container (default: worker)
  swap          Create a 2 GiB swapfile on the host (asks first)
  nuke          Remove containers AND the data volume (asks first)
USAGE
}

cmd="${1:-up}"; shift || true

case "$cmd" in
  up)
    require_env; check_memory
    info "building images"
    compose -f "$COMPOSE_FILE" build
    info "starting"
    compose -f "$COMPOSE_FILE" up -d
    compose -f "$COMPOSE_FILE" ps
    port="$(grep -E '^HTTP_PORT=' .env | cut -d= -f2)"; port="${port:-80}"
    info "site: http://localhost:${port}  ·  api: http://localhost:${port}/api/health"
    info "the first scrape starts immediately (RUN_ON_START=true); follow it with './run.sh logs worker'"
    ;;
  down)    compose -f "$COMPOSE_FILE" down ;;
  restart) compose -f "$COMPOSE_FILE" restart ;;
  build)   require_env; check_memory; compose -f "$COMPOSE_FILE" build ;;
  logs)    compose -f "$COMPOSE_FILE" logs -f --tail=120 "$@" ;;
  status)
    compose -f "$COMPOSE_FILE" ps
    echo
    docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.CPUPerc}}' \
      $(compose -f "$COMPOSE_FILE" ps -q) 2>/dev/null || true
    ;;
  scrape)
    target="${1:-all}"
    info "running a scrape for '${target}' inside the worker container"
    compose -f "$COMPOSE_FILE" exec -T worker python -u -m common.pipeline --country "$target"
    ;;
  forecast)
    compose -f "$COMPOSE_FILE" exec -T worker python -u -m common.pipeline --forecast-only
    ;;
  seed)
    days="${1:-120}"
    warn "seeding writes synthetic rows (companies named 'Example ...') into the live database"
    compose -f "$COMPOSE_FILE" exec -T worker python -u /app/scripts/seed.py --days "$days" --reset
    ;;
  backup)
    compose -f "$COMPOSE_FILE" exec -T worker python -c \
      "from common import s3store, config; print(s3store.backup_db(config.DB_PATH) or 'S3 not configured')"
    ;;
  shell)   compose -f "$COMPOSE_FILE" exec "${1:-worker}" sh ;;
  swap)
    [[ -f /swapfile ]] && die "/swapfile already exists"
    warn "this modifies the HOST: creates /swapfile (2 GiB) and appends a line to /etc/fstab."
    read -r -p "Continue? [y/N] " reply
    [[ "$reply" =~ ^[Yy]$ ]] || { info "aborted"; exit 0; }
    sudo fallocate -l 2G /swapfile || sudo dd if=/dev/zero of=/swapfile bs=1M count=2048
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile
    sudo swapon /swapfile
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
    info "swap enabled"; free -h
    ;;
  nuke)
    warn "this DELETES the data volume - every scraped listing and all history."
    read -r -p "Type the word 'delete' to confirm: " reply
    [[ "$reply" == "delete" ]] || { info "aborted"; exit 0; }
    compose -f "$COMPOSE_FILE" down -v
    ;;
  -h|--help|help) usage ;;
  *) usage; die "unknown command: $cmd" ;;
esac
