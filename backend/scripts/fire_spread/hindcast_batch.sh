#!/bin/bash
# Hindcast several settings back to back; log to data/fire_spread/hindcast/batch.log. Run detached
# from backend/ (the compose service mounts scripts/ and data/fire_spread/):
#   docker compose run -d --name hindcast api bash scripts/fire_spread/hindcast_batch.sh \
#       "--mode base" "--mode tuned" "--mode tuned --spotting"
cd /srv
HC=data/fire_spread/hindcast
mkdir -p "$HC"
[ -f data/fire_spread/catalonia/agri.tif ] || python -m scripts.fire_spread.prepare_static_data --steps agri >> "$HC/batch.log" 2>&1
{
  for args in "$@"; do
    echo "== $args $(date -u +%FT%TZ)"
    python -m scripts.fire_spread.hindcast --min-ha 100 --members 4 $args 2>&1 | grep --line-buffered -v 'Warn\|HTTP Request\|being appended'
  done
  echo "== done $(date -u +%FT%TZ)"
} >> "$HC/batch.log" 2>&1
