#!/bin/bash
# Run the same real ignition with both fuel-model sets and print burned extent. From backend/:
#   docker compose run --rm api bash scripts/fire_spread/compare_fuels.sh [lat lon start]
cd /srv
LAT=${1:-41.59}; LON=${2:-1.83}; START=${3:-2026-09-20T14:20:00+00:00}
for f in scott_burgan mediterranean; do
  python -m fire_spread.cli --lat "$LAT" --lon "$LON" --hours 24 --members 8 --seed 1 \
      --start "$START" --fuels "$f" 2>&1 | grep -v 'Warn\|HTTP Request' | grep 'grid \|error' | sed "s/^/$f: /"
  d=$(ls -d data/fire_spread/runs/2026* | tail -1)
  grep -ci 'error\|segm' "$d/elmfire.out" | sed 's/^/  elmfire errors: /'
  awk -F, 'NR>1 {s+=$7} END {printf "  mean fire area per case: %.0f acres\n", s/(NR-1)}' "$d/outputs/fire_size_stats.csv"
done
