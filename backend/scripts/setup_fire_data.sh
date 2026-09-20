#!/usr/bin/env bash
#
# One-time download + build of the ELMFIRE static tier for Catalonia (DEM, slope,
# aspect, fuel model, canopy, burn scars, agriculture, barriers), used by
# GET /fire/arrival-grid. Runs scripts/fire_spread/prepare_static_data.py inside
# the API container, so it needs the image from scripts/setup_db.sh (or
# `docker compose build api`) but not a running database.
#
#   backend/scripts/setup_fire_data.sh                  everything
#   backend/scripts/setup_fire_data.sh --steps dem,fuel  selected steps only
#   backend/scripts/setup_fire_data.sh --res 30          30 m tier instead of 50 m
#
# Downloads about 2 GB of sources into backend/data/fire_spread/raw/ and writes
# the tier to backend/data/fire_spread/catalonia/ (~100 MB at 50 m, ~300 MB at
# 30 m). Expect 10-30 minutes depending on bandwidth. Safe to re-run: steps
# reuse downloads already on disk. Until the tier exists, GET /health reports
# fire_spread "no data" and /fire/arrival-grid answers 503.

set -euo pipefail

BACKEND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE=(docker compose -f "$BACKEND_DIR/docker-compose.yml")

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31merror\033[0m %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null || die "docker is not installed."
docker info >/dev/null 2>&1 || die "the Docker daemon is not running -- start Docker Desktop and re-run."

mkdir -p "$BACKEND_DIR/data/fire_spread"

info "Building the static tier into data/fire_spread/catalonia (this takes a while)"
"${COMPOSE[@]}" run --rm --no-deps api python -m scripts.fire_spread.prepare_static_data "$@"

info "Done. Restart the API so /health picks the tier up: ${COMPOSE[*]} up -d api"
