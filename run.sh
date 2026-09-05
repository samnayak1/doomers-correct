#!/usr/bin/env bash
# Build + deploy on a 1 GiB instance.
#
# `docker compose build` builds services in PARALLEL by default, which on this
# box means several pip installs at once — the usual cause of an OOM-killed
# deploy. Build one at a time instead.
set -euo pipefail
cd "$(dirname "$0")"

MODE="${1:-prod}"
case "$MODE" in
  dev)  FILES=(-f docker-compose.yml -f docker-compose.dev.yml);  BUILD=(worker api web)   ;;
  prod) FILES=(-f docker-compose.yml -f docker-compose.prod.yml); BUILD=(worker api caddy) ;;
  *)    echo "usage: ./run.sh [dev|prod]   (default: prod)" >&2; exit 1 ;;
esac

[[ -f .env ]] || { cp .env.example .env; echo "Created .env from .env.example — fill it in, then re-run." >&2; exit 1; }
[[ -s .env ]] || { echo ".env is empty. Run: cp .env.example .env" >&2; exit 1; }

swapon --show | grep -q . || {
  echo "No swap active. Run scripts/setup-swap.sh first." >&2; exit 1; }

for svc in "${BUILD[@]}"; do
  echo "==> Building $svc"
  docker compose "${FILES[@]}" build "$svc"
done

echo "==> Starting ($MODE)"
docker compose "${FILES[@]}" up -d
echo "==> Reclaiming build layer space"
docker image prune -f
docker compose "${FILES[@]}" ps
