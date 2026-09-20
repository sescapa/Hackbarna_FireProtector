#!/usr/bin/env bash
#
# Container-side setup for the FireProtector stack: same result as setup_db.sh
# (Postgres + schema + the building register + the forests + the API), but the
# only thing the host needs is Docker -- the INSPIRE download, parsing and COPY
# all run inside the API image (scripts/load_register.py), so it behaves the
# same on Linux, macOS and Windows.
#
#   backend/scripts/setup_db_container.sh                load everything still missing
#   backend/scripts/setup_db_container.sh --limit 10     only the 10 smallest pending
#   backend/scripts/setup_db_container.sh --only olot    named municipalities only
#   backend/scripts/setup_db_container.sh --workers 8    parallelism (default 4)
#   backend/scripts/setup_db_container.sh --forests-only refresh the forests, no buildings
#   backend/scripts/setup_db_container.sh --no-forests   buildings only
#
# Safe to re-run: municipalities already loaded are skipped. The full register
# (947 municipalities, ~12 GB of GML streamed, never stored) takes hours; the
# forests take seconds. To start over: docker compose down -v, then re-run.

set -euo pipefail

BACKEND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE=(docker compose -f "$BACKEND_DIR/docker-compose.yml")

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31merror\033[0m %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null || die "docker is not installed."
docker info >/dev/null 2>&1 || die "the Docker daemon is not running -- start Docker Desktop and re-run."

info "Building the API image (ELMFIRE stage is cached after the first build)"
"${COMPOSE[@]}" build api

info "Starting Postgres"
"${COMPOSE[@]}" up -d --wait db

info "Loading the register inside the API container"
"${COMPOSE[@]}" run --rm --no-deps api python -m scripts.load_register "$@" || die "the load reported failures (see above); re-run to retry them."

info "Starting the API"
"${COMPOSE[@]}" up -d --wait api
echo
echo "Everything is up."
echo "    Health    http://localhost:${API_PORT:-5102}/health"
echo "    Assets    http://localhost:${API_PORT:-5102}/assets?bbox=1.0,41.6,1.6,42.0"
echo "    Spread    http://localhost:${API_PORT:-5102}/fire/arrival-grid?lat=41.59&lon=1.83   (needs scripts/setup_fire_data.sh)"
echo "    API docs  http://localhost:${API_PORT:-5102}/docs"
