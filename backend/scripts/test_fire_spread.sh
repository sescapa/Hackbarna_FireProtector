#!/usr/bin/env bash
#
# Run the fire-spread tests that need the real ELMFIRE binary, inside the API
# container (the image has ELMFIRE and OpenMPI; tests/ and the dev
# dependencies are mounted and installed on the fly, never baked in).
#
#   backend/scripts/test_fire_spread.sh                 # -m elmfire (synthetic landscape, no static tier)
#   backend/scripts/test_fire_spread.sh tests/fire_spread   # the whole fire-spread suite in the container
#
# Needs the image (scripts/setup_db.sh or `docker compose build api`), not the database.

set -euo pipefail

BACKEND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Git Bash on Windows: hand Docker a C:/... path and stop MSYS rewriting the
# /srv/... halves of the -v arguments.
if command -v cygpath >/dev/null 2>&1; then
    BACKEND_DIR="$(cygpath -m "$BACKEND_DIR")"
    export MSYS_NO_PATHCONV=1
fi
COMPOSE=(docker compose -f "$BACKEND_DIR/docker-compose.yml")

[[ $# -gt 0 ]] || set -- -m elmfire tests/fire_spread

"${COMPOSE[@]}" run --rm --no-deps \
    -v "$BACKEND_DIR/tests:/srv/tests:ro" \
    -v "$BACKEND_DIR/pytest.ini:/srv/pytest.ini:ro" \
    -v "$BACKEND_DIR/requirements-dev.txt:/srv/requirements-dev.txt:ro" \
    -v "$BACKEND_DIR/scripts/requirements.txt:/srv/scripts/requirements.txt:ro" \
    api sh -c "pip install -q --user -r requirements-dev.txt && python -m pytest $*"
