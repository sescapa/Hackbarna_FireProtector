"""Run the pipeline without HTTP.

    python -m fire_spread.cli --lat 41.59 --lon 1.83 --members 4 --out grid.json
    python -m fire_spread.cli --lat 41.59 --lon 1.83 --weather-fixture tests/fixtures/weather_west_30mph.json
    python -m fire_spread.cli --lat 41.59 --lon 1.83 --synthetic --elmfire-data-only
    python -m fire_spread.cli --lat 41.59 --lon 1.83 --mode base
    python -m fire_spread.cli --perimeter fire.geojson --hours 6     # active perimeter (GeoJSON geometry/Feature)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from .landscape import SyntheticLandscape
from .models import Ignition, PipelineError, SimulationRequest
from .modes import MODES
from .pipeline import Pipeline, RunDirOnly
from .settings import Settings
from .weather import ConstantProvider, FixtureProvider


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ELMFIRE arrival-grid pipeline")
    ap.add_argument("--lat", type=float, default=None, help="ignition / reference latitude")
    ap.add_argument("--lon", type=float, default=None)
    ap.add_argument("--perimeter", type=Path, default=None, help="GeoJSON file (geometry or Feature) of the burning area")
    ap.add_argument("--mode", choices=MODES, default=None, help="pipeline mode (default: PIPELINE_MODE)")
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--members", type=int, default=4)
    ap.add_argument("--start", type=datetime.fromisoformat, default=None)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--weather-fixture", type=Path, default=None, help="JSON fixture instead of Open-Meteo")
    ap.add_argument("--constant-wind", nargs=2, type=float, metavar=("WS_MS", "WD_DEG"), default=None)
    ap.add_argument("--synthetic", action="store_true", help="flat uniform GR2 landscape (no static data needed)")
    ap.add_argument("--elmfire-data-only", action="store_true", help="stop after writing the run directory")
    ap.add_argument("--out", type=Path, default=None, help="write the ArrivalGrid JSON here (default: stdout summary)")
    ap.add_argument("--domain-km", type=float, default=None)
    ap.add_argument("--spotting", action="store_true", help="enable ember transport")
    ap.add_argument("--fuels", choices=("scott_burgan", "mediterranean"), default=None, help="fuel model parameter set")
    ap.add_argument("--no-ensemble", action="store_true", help="perturb a deterministic forecast instead of NWP members")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    if a.perimeter is None and (a.lat is None or a.lon is None):
        ap.error("--lat/--lon or --perimeter is required")

    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    overrides = {"keep_runs": "all"}
    if a.domain_km:
        overrides["domain_size_m"] = a.domain_km * 1000
    if a.no_ensemble:
        overrides["weather_ensemble"] = False
    if a.fuels:
        overrides["fuel_model_set"] = a.fuels
    settings = Settings(**overrides)

    provider = None
    if a.weather_fixture:
        provider = FixtureProvider(a.weather_fixture)
    elif a.constant_wind:
        provider = ConstantProvider(ws_ms=a.constant_wind[0], wd_deg=a.constant_wind[1])
    landscape = SyntheticLandscape(cellsize=settings.cell_size_m or 50.0) if a.synthetic else None

    pipeline = Pipeline(settings=settings, weather_provider=provider, landscape=landscape, mode=a.mode)
    perimeter = None
    lat, lon = a.lat, a.lon
    if a.perimeter is not None:
        from shapely.geometry import shape

        doc = json.loads(a.perimeter.read_text(encoding="utf-8"))
        perimeter = doc.get("geometry", doc) if doc.get("type") == "Feature" else doc
        if lat is None or lon is None:
            c = shape(perimeter).centroid
            lat, lon = c.y, c.x
    req = SimulationRequest(
        ignition=Ignition(lat, lon, perimeter), duration_hours=a.hours, ensemble_members=a.members,
        start_time=a.start, seed=a.seed, spotting=a.spotting or None, mode=a.mode, debug=True,
    )
    try:
        grid = asyncio.run(pipeline.run(req, elmfire_data_only=a.elmfire_data_only))
    except RunDirOnly as e:
        print(f"run directory written: {e.run_dir}")
        return 0
    except PipelineError as e:
        print(f"error ({e.status_code}): {e}", file=sys.stderr)
        return 1

    doc = grid.model_dump(exclude_none=True)
    if a.out:
        a.out.write_text(json.dumps(doc))
        print(f"wrote {a.out}")
    rows, cols = len(grid.arrivalMinutes), len(grid.arrivalMinutes[0]) if grid.arrivalMinutes else 0
    burned = sum(1 for r in grid.burnProbability for v in r if v)
    print(f"grid {rows}x{cols} cells, {burned} cells with burnProbability>0, members={grid.ensembleMembers}, mode={pipeline.mode}")
    if grid.debug:
        print(f"run dir: {grid.debug.runDir}\ntimings: {grid.debug.timings}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
