#!/usr/bin/env bash
#
# Evaluation execution for the fire-spread service: run the `base` and `tuned`
# pipeline modes on historical weather against real free-burning Catalan
# wildfires (scripts/fire_spread/eval_fires.json) and compare both with the
# observed DARP perimeters, timing included. Runs
# scripts/fire_spread/evaluate.py inside the API container (ELMFIRE + static
# tier + the DARP perimeters from scripts/setup_fire_data.sh); no database.
#
#   backend/scripts/evaluate_fire_spread.sh                        # both modes, 8 fires, 4 members (~30-60 min)
#   backend/scripts/evaluate_fire_spread.sh --limit 2 --members 2  # quick smoke run
#   backend/scripts/evaluate_fire_spread.sh --modes tuned --weather archive
#
# Results land in backend/data/fire_spread/hindcast/eval_<timestamp>/
# (base.csv, tuned.csv, summary.json, summary.md); paste the summary table into
# docs/elmfire-pipe-assessment.md.

set -euo pipefail

BACKEND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE=(docker compose -f "$BACKEND_DIR/docker-compose.yml")

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31merror\033[0m %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null || die "docker is not installed."
docker info >/dev/null 2>&1 || die "the Docker daemon is not running -- start Docker Desktop and re-run."
[[ -f "$BACKEND_DIR/data/fire_spread/catalonia/dem.tif" ]] || die "static tier missing -- run scripts/setup_fire_data.sh first."

info "Evaluating pipeline modes on historical fires (results: data/fire_spread/hindcast/eval_<timestamp>/)"
"${COMPOSE[@]}" run --rm --no-deps api python -m scripts.fire_spread.evaluate "$@"
