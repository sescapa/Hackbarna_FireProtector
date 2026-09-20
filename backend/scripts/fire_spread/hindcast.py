#!/usr/bin/env python
"""Hindcast DARP fire perimeters with the pipeline and score them (calibration loop).

Run inside the container (needs ELMFIRE + ogr2ogr + the static tier), from ``backend/``:

    docker compose run --rm api python -m scripts.fire_spread.hindcast --min-ha 100 --limit 10
    ... hindcast --mode base --years 2019-2024        # stock ELMFIRE knobs (fire_spread/modes.py)
    ... hindcast --mode tuned --adj 0.8               # a mode plus one knob override

``scripts/fire_spread/evaluate.py`` drives this module for both modes on the curated
free-burning fire set and writes the comparison.

Per fire: perimeter + date from ``data/fire_spread/raw/incendis/<year>/*.shp`` (converted once to
``<year>/*.4326.geojson``) → historical weather at the centroid (past forecast runs, or ERA5 with
``--weather archive``) → ignition at
the most upwind burnable point of the perimeter (DARP has no ignition point or time; 12:00 UTC
assumed) → pipeline run (statistical wind perturbations: no past ensemble) → burn probability
≥ ``--pmin`` vs the observed perimeter: Jaccard, Sørensen, area bias. Results in
``data/fire_spread/hindcast/<tag>.csv`` and a summary line to paste into docs/elmfire-pipe-assessment.md.
"""

from __future__ import annotations

import argparse
import asyncio
import time
import csv
import json
import math
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from rasterio import features
from rasterio.transform import from_origin
from shapely.geometry import shape
from shapely.ops import transform as shp_transform

from fire_spread import landscape as ls, weather as wx
from fire_spread.models import Ignition, OutsideCoverage, PipelineError, SimulationRequest
from fire_spread.modes import MODES, apply_mode
from fire_spread.pipeline import Pipeline
from fire_spread.settings import Settings

# Relative to the cwd (backend/ on the host, /srv in the container), like fire_spread.settings.
RAW = Path("data/fire_spread/raw/incendis")
OUT = Path("data/fire_spread/hindcast")
IGNITION_HOUR_UTC = 12
INSET_M = 100.0  # ignition pulled this far inside the perimeter from the upwind edge
CANDIDATES = 8  # upwind-most vertices tried until one is on burnable fuel


def load_perimeters(years: range) -> list[dict]:
    """[{code, date, municipality, geom_lonlat (shapely), area_ha}] for the requested years."""
    shps = sorted(p for y in sorted(RAW.glob("20*")) for p in y.glob("*.shp"))
    if not shps:
        raise SystemExit(f"no DARP shapefiles under {RAW} (run prepare_static_data.py --steps burns)")
    feats = []
    for shp in shps:  # one GeoJSON per year: the driver drops fields when appending mixed schemas
        gj = shp.with_suffix(".4326.geojson")
        if not gj.exists():
            subprocess.run(["ogr2ogr", "-f", "GeoJSON", "-t_srs", "EPSG:4326", "-lco", "COORDINATE_PRECISION=6",
                            str(gj), str(shp)], check=True)
        feats += json.loads(gj.read_text(encoding="utf-8"))["features"]
    out = []
    for f in feats:
        props = {k.upper(): v for k, v in f["properties"].items()}
        code, date_s = str(props.get("CODI_FINAL", "")), str(props.get("DATA_INCEN", ""))
        try:
            d = datetime.strptime(date_s, "%d/%m/%Y" if len(date_s) == 10 else "%d/%m/%y")
        except ValueError:
            continue
        if d.year not in years:
            continue
        geom = shape(f["geometry"])
        if geom.is_empty:
            continue
        proj = shp_transform(lambda x, y, z=None: ls.lonlat_to_xy(x, y), geom)
        out.append({"code": code, "date": d, "municipality": str(props.get("MUNICIPI", "")).strip(),
                    "geom": geom, "geom_xy": proj, "area_ha": proj.area / 1e4})
    return out


def upwind_candidates(geom_xy, downwind: tuple[float, float] | None) -> list[tuple[float, float]]:
    """Perimeter vertices sorted from most upwind (min projection on the downwind vector),
    each pulled INSET_M toward the centroid. Calm wind: centroid first."""
    c = geom_xy.centroid
    pts = []
    polys = geom_xy.geoms if hasattr(geom_xy, "geoms") else [geom_xy]
    for poly in polys:
        pts.extend(dict.fromkeys(poly.exterior.segmentize(INSET_M).coords))  # densified, deduped
    if downwind is None:
        order = sorted(pts, key=lambda p: (p[0] - c.x) ** 2 + (p[1] - c.y) ** 2)
    else:
        ux, uy = downwind
        order = sorted(pts, key=lambda p: p[0] * ux + p[1] * uy)
    out = [(c.x, c.y)] if downwind is None else []
    for x, y in order[: CANDIDATES * 3]:
        d = math.hypot(c.x - x, c.y - y)
        if d > 0:
            x, y = x + (c.x - x) / d * min(INSET_M, d), y + (c.y - y) / d * min(INSET_M, d)
        out.append((x, y))
    return out[:CANDIDATES]


def score(grid, geom_lonlat, obs_area_m2: float, pmin: float) -> dict:
    """Compare burn probability ≥ pmin with the observed perimeter. Areas in ha."""
    prob = np.array([[np.nan if v is None else v for v in row] for row in grid.burnProbability])
    prob = np.flipud(np.nan_to_num(prob, nan=0.0))  # rows N -> S for rasterio
    rows, cols = prob.shape
    tr = from_origin(grid.originLon, grid.originLat + rows * grid.cellDegLat, grid.cellDegLon, grid.cellDegLat)
    obs = features.rasterize([(geom_lonlat, 1)], out_shape=(rows, cols), transform=tr, fill=0, dtype="uint8").astype(bool)
    pred = prob >= pmin
    cell = grid.cellSizeM ** 2 / 1e4
    tp, fp = float((pred & obs).sum() * cell), float((pred & ~obs).sum() * cell)
    obs_ha = obs_area_m2 / 1e4
    fn = max(obs_ha - tp, 0.0)
    pred_ha = float(pred.sum() * cell)
    return {
        "pred_ha": round(pred_ha, 1), "obs_ha": round(obs_ha, 1), "tp_ha": round(tp, 1),
        "jaccard": round(tp / (tp + fp + fn), 3) if tp + fp + fn > 0 else 0.0,
        "sorensen": round(2 * tp / (2 * tp + fp + fn), 3) if tp + fp + fn > 0 else 0.0,
        "bias": round(pred_ha / obs_ha, 2) if obs_ha > 0 else float("nan"),
        "recall": round(tp / obs_ha, 3) if obs_ha > 0 else 0.0,  # low = spread too slow / wrong axis
        "precision": round(tp / pred_ha, 3) if pred_ha > 0 else 0.0,  # low = burned where it was stopped
        "obs_in_grid_ha": round(float(obs.sum() * cell), 1),
    }


async def run_case(pipe: Pipeline, provider: wx.OpenMeteoProvider, fire: dict, hours: int, members: int, spotting: bool,
                   pmin: float, ign_hour: int = IGNITION_HOUR_UTC, start: datetime | None = None,
                   ignition: tuple[float, float] | None = None) -> dict:
    """One fire through the pipeline. ``start`` (UTC) and ``ignition`` (lat, lon) override the
    DARP-derived defaults (date at ``ign_hour``, upwind-most burnable vertex) when known."""
    start = start or fire["date"].replace(hour=ign_hour, tzinfo=timezone.utc)
    c = fire["geom"].centroid
    wxr = await provider.fetch([(c.y, c.x)], start, 9)
    downwind = wx.mean_downwind_unit(wxr.forecast())
    last: Exception | None = None
    candidates = [ls.lonlat_to_xy(ignition[1], ignition[0])] if ignition else upwind_candidates(fire["geom_xy"], downwind)
    for x, y in candidates:
        lon, lat = ls.xy_to_lonlat(x, y)
        req = SimulationRequest(Ignition(lat, lon), duration_hours=hours, ensemble_members=members,
                                start_time=start, seed=int(fire["code"][-6:]) or 1, spotting=spotting, debug=True)
        t0 = time.perf_counter()
        try:
            grid = await pipe.run(req)
        except OutsideCoverage as e:
            last = e
            continue
        res = {"code": fire["code"], "date": fire["date"].date().isoformat(), "municipality": fire["municipality"],
               "mode": pipe.mode, "start_utc": start.isoformat(), "hours": hours,
               "ign_lat": round(lat, 5), "ign_lon": round(lon, 5), "wind_ms": round(grid.weather.windSpeedAvgMs, 1),
               "wind_dir": round(grid.weather.windDirectionAvg), "m1_pct": round(grid.weather.fuelMoisture1hAvgPct or 0, 1),
               **score(grid, fire["geom"], fire["geom_xy"].area, pmin),
               "elmfire_s": round(grid.debug.timings.get("elmfire_s", 0.0), 1),
               "prep_s": round(sum(v for k, v in grid.debug.timings.items() if k != "elmfire_s"), 1),
               "wall_s": round(time.perf_counter() - t0, 1)}
        return res
    raise last or OutsideCoverage("no burnable ignition candidate")


def build_settings(mode: str | None, *, fuels: str | None = None, adj: float | None = None,
                   max_low: float | None = None, **fixed) -> Settings:
    """Hindcast settings: no run dirs kept, statistical wind perturbations (no NWP ensemble
    in the past), ignition-year burn scars ignored; the mode's knobs, then explicit
    overrides (which ``apply_mode`` keeps because they count as explicitly set)."""
    explicit = {k: v for k, v in (("fuel_model_set", fuels), ("adj_factor", adj), ("max_low", max_low)) if v is not None}
    return apply_mode(Settings(keep_runs="none", weather_ensemble=False, hindcast=True, **fixed, **explicit), mode)


def weather_provider(kind: str) -> wx.OpenMeteoProvider:
    """``historical`` = past runs of the high-resolution forecast models (what the live API
    would have seen), ``archive`` = ERA5 reanalysis."""
    # Batches hit Open-Meteo's minutely limit: wait it out rather than skip the fire. A
    # commercial key (OPEN_METEO_API_KEY) lifts the daily limit a batch otherwise exhausts.
    # Past weather never changes, so responses are cached on disk for good (WEATHER_CACHE_DIR).
    waits = (15.0, 65.0, 65.0)
    s = Settings()
    kw = dict(retry_waits_s=waits, api_key=s.open_meteo_api_key, cache_dir=s.weather_cache_dir)
    if kind == "archive":
        return wx.OpenMeteoProvider("https://archive-api.open-meteo.com", archive=True, **kw)
    return wx.OpenMeteoProvider("https://historical-forecast-api.open-meteo.com", historical=True, **kw)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="DARP perimeter hindcast")
    ap.add_argument("--years", default="2019-2024")
    ap.add_argument("--min-ha", type=float, default=100.0)
    ap.add_argument("--limit", type=int, default=0, help="largest N fires only (0 = all)")
    ap.add_argument("--fires", default="", help="comma-separated CODI_FINAL codes")
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--members", type=int, default=4)
    ap.add_argument("--pmin", type=float, default=0.5, help="burn probability counted as burned")
    ap.add_argument("--mode", choices=MODES, default=None, help="knob bundle from fire_spread/modes.py (default: PIPELINE_MODE)")
    ap.add_argument("--fuels", choices=("scott_burgan", "mediterranean"), default=None, help="override the mode's fuel set")
    ap.add_argument("--adj", type=float, default=None, help="override the mode's ADJ spread-rate multiplier")
    ap.add_argument("--spotting", action="store_true")
    ap.add_argument("--no-barriers", action="store_true")
    ap.add_argument("--weather", choices=("historical", "archive"), default="historical",
                    help="historical = past high-res forecast runs (default), archive = ERA5 reanalysis")
    ap.add_argument("--ign-hour", type=int, default=IGNITION_HOUR_UTC, help="assumed ignition hour (UTC)")
    ap.add_argument("--max-low", type=float, default=None, help="override the mode's fire ellipse length/width cap")
    ap.add_argument("--tag", default=None, help="results file name (default from settings)")
    a = ap.parse_args(argv)

    y0, y1 = (int(v) for v in a.years.split("-")) if "-" in a.years else (int(a.years),) * 2
    fires = [f for f in load_perimeters(range(y0, y1 + 1)) if f["area_ha"] >= a.min_ha]
    if a.fires:
        want = set(a.fires.split(","))
        fires = [f for f in fires if f["code"] in want]
    fires.sort(key=lambda f: -f["area_ha"])
    if a.limit:
        fires = fires[: a.limit]
    if not fires:
        raise SystemExit("no fires selected")

    settings = build_settings(a.mode, fuels=a.fuels, adj=a.adj, max_low=a.max_low,
                              use_barriers=not a.no_barriers, spotting_default=a.spotting)
    provider = weather_provider(a.weather)
    pipe = Pipeline(settings=settings, weather_provider=provider)
    s = pipe.settings
    tag = a.tag or (f"{s.pipeline_mode}_{s.fuel_model_set}_adj{s.adj_factor:g}_h{a.hours}_{a.weather}_ign{a.ign_hour}_low{s.max_low:g}"
                    f"{'_spot' if a.spotting else ''}{'_nobar' if a.no_barriers else ''}")
    OUT.mkdir(parents=True, exist_ok=True)
    out_csv = OUT / f"{tag}.csv"

    rows = []
    for i, f in enumerate(fires, 1):
        print(f"[{i}/{len(fires)}] {f['code']} {f['municipality']} {f['date'].date()} {f['area_ha']:.0f} ha ...", end=" ", flush=True)
        try:
            r = asyncio.run(run_case(pipe, provider, f, a.hours, a.members, a.spotting, a.pmin, a.ign_hour))
        except PipelineError as e:
            print(f"skipped ({e})")
            continue
        rows.append(r)
        print(f"pred {r['pred_ha']:.0f} ha  J {r['jaccard']:.2f}  S {r['sorensen']:.2f}  bias {r['bias']:.2f}  {r['elmfire_s']:.0f}s elmfire / {r['wall_s']:.0f}s")
        with out_csv.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader(); w.writerows(rows)
    if not rows:
        return 1
    j = np.array([r["jaccard"] for r in rows]); s_ = np.array([r["sorensen"] for r in rows])
    b = np.array([r["bias"] for r in rows])
    rc = np.array([r["recall"] for r in rows]); pr = np.array([r["precision"] for r in rows])
    el = np.array([r["elmfire_s"] for r in rows]); wl = np.array([r["wall_s"] for r in rows])
    print(f"\n{tag}: n={len(rows)}  Jaccard mean {j.mean():.3f} / median {np.median(j):.3f}  "
          f"Sørensen {s_.mean():.3f}  bias median {np.median(b):.2f} (p25 {np.percentile(b, 25):.2f}, p75 {np.percentile(b, 75):.2f})  "
          f"recall {rc.mean():.2f}  precision {pr.mean():.2f}  "
          f"time/fire median {np.median(wl):.0f}s (elmfire {np.median(el):.0f}s, max {wl.max():.0f}s)")
    print(f"| {datetime.now(timezone.utc).date()} | {len(rows)} ({a.years}, ≥{a.min_ha:g} ha) | {tag}, {a.members} members, pmin {a.pmin} "
          f"| {j.mean():.3f} / {np.median(j):.3f} | {s_.mean():.3f} | {np.median(b):.2f} | {rc.mean():.2f} / {pr.mean():.2f} | {np.median(wl):.0f} s | |")
    print(f"results: {out_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
