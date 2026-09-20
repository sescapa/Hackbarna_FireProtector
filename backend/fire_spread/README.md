# Fire-spread service (self-hosted ELMFIRE for Catalonia)

Given an initial fire state — a point ignition or an active perimeter — runs a Monte Carlo
ensemble of [ELMFIRE](https://github.com/lautenberger/elmfire) simulations on open Catalan data
and live weather, and returns a lat/lon grid with **minute-level arrival times** and **per-cell
uncertainty** (burn probability, P10/P90 arrival).

```
GET  /fire/arrival-grid?lat=41.59&lon=1.83[&durationHours=24][&ensembleMembers=16][&startTime=ISO][&seed=N][&spotting=1][&mode=base|tuned][&debug=1]
POST /fire/arrival-grid  {"ignition": {"lat", "lon"} and/or "perimeter": <GeoJSON Polygon|MultiPolygon>, "durationHours", "ensembleMembers", "startTime", "seed", "spotting", "mode", "debug"}
```

One request = one simulation, synchronous (10–60 s). Full contract in [`openapi.yaml`](openapi.yaml).

Two **pipeline modes** share every input and differ only in model knobs (`modes.py`):
`base` is stock ELMFIRE physics (S&B 40 fuel table, no night damping, no wind gustiness,
constant live moistures — the namelist defaults of the pinned commit), `tuned` is our
Mediterranean/Catalan bundle (Mediterranean fuel table, ADJ 1.4, overnight factor 0.7, wind
fluctuations, monthly live-moisture climatology — fitted on the free-burning evaluation set to
over-burn slightly rather than under-burn). `PIPELINE_MODE` (default `tuned`) picks the
one served; `mode=` overrides per request; the response reports it under `physics.mode` /
`physics.modeKnobs`. A knob set explicitly in the environment (`ADJ_FACTOR=0.8`) is kept in
both modes. The [evaluation execution](#evaluation-base-vs-tuned-on-historical-fires) decides
whether a tweak stays in `tuned`.

This package is mounted at `/fire` by the FireProtector API (`backend/app/main.py`); it never
touches the database and reads its own settings (`fire_spread/settings.py`) from the environment
/ `backend/.env`, so it stays mountable on its own. Everything below is run from `backend/`.

## How it works

Two data tiers:

* **Static (built once)** – `scripts/setup_fire_data.sh` (→ `scripts/fire_spread/prepare_static_data.py`)
  produces Catalonia-wide Int16 COGs in `data/fire_spread/catalonia/` (EPSG:25831, 50 m by default, `--res 30` for ELMFIRE's usual
  30 m; the pipeline follows the tier's resolution): `dem, slp, asp` (Copernicus GLO-30),
  `fbfm40` (ZAFM-DW 2026 Scott & Burgan fuel map), `cc, ch, cbh, cbd` (ICGC/CREAF canopy),
  `burnyear.tif` (DARP fire perimeters 2012–2024), `barrier.tif` (OSM roads/waterways as
  fire-break width in m), plus `zhr.gpkg/.geojson` (Bombers fire-regime zones) and
  `manifest.json` (exposed at `GET /fire/data-info`). ~1 GB, git-ignored; share as a
  volume/release asset.
* **Dynamic (per request)** – Open-Meteo hourly forecast (`best_match` → AROME 1.3 km) at a
  `WEATHER_GRID_N`² grid of points over the domain (ELMFIRE interpolates bilinearly), plus 48 h
  of history to spin up the time-lagged 1/10/100-h dead fuel moistures (Simard EMC + exponential
  time-lag, rain wetting), and the Open-Meteo Ensemble API (`OPEN_METEO_ENSEMBLE_MODEL`,
  ICON-EU-EPS by default) whose members become the weather streams of the ELMFIRE cases.

Per request (`fire_spread/pipeline.py`): point forecast at the reference point (the ignition,
or the perimeter centroid) → 40×40 km domain with that point ⅓ of the way from the upwind edge
(fires run downwind), snapped to the static grid → window-read the layers, remapping fuel inside
DARP perimeters burned < 6 years ago (`burnyear.tif`) → weather grid: one block of hourly bands
per NWP ensemble member, stacked into coarse `ws, wd, m1, m10, m100` rasters → `elmfire.data` +
`inputs/ignitions.csv` (one case per row: same ignition, its own starting weather band;
live/foliar fuel moisture, night damping, wind gustiness and the fuel table per the mode;
optional spotting) → `mpirun elmfire` → stack `time_of_arrival_*.tif` over cases (block offsets
removed) → burn probability / median / P10 / P90 → reproject to a lat/lon grid (reference point
at a cell centre, rows S→N, cols W→E, cropped to the burned extent) → JSON.

An **active perimeter** cannot go through ELMFIRE's `PHI` raster: on the per-case-weather path
(`RANDOM_IGNITIONS`) the level set is never seeded from it. It is written instead as up to 100
fixed ignition points (`X_IGN/Y_IGN/T_IGN`, ELMFIRE's cap) on the outer ring of burnable cells
inside the polygon, evenly spaced along the boundary and lit at `SIMULATION_TSTART` in every
case; cells inside the polygon are stamped arrival 0 / probability 1 in the response, and the
perimeter must fit the domain (≈ 10 km across at the upwind position) and touch burnable fuel.

Ensemble design: every case runs on a *physically consistent* NWP member (wind, temperature,
humidity and rain co-vary) plus small Gaussian perturbations of 1-h fuel moisture (σ 1.5 %) and
a uniform spread-rate adjustment factor (0.8–1.25) for model error. Without an ensemble
(fixture weather, API failure) all cases share the deterministic stream and wind speed/direction
are perturbed with the ensemble's σ (or defaults) instead.

## Run

```sh
cd backend
scripts/setup_db.sh                  # Postgres + API image (builds ELMFIRE from a pinned main commit, +2 upstream patches; see Dockerfile)
scripts/setup_fire_data.sh           # once: downloads ~2 GB of sources, writes the static tier (10-30 min)
docker compose up -d api             # http://localhost:5102/fire/arrival-grid?lat=41.59&lon=1.83
```

`docker compose build api` is enough if you only want the image. Until the tier exists
`GET /health` reports `"fire_spread": "no data"` and `/fire/arrival-grid` answers 503; with it,
`"ready"`. The compose service mounts `app/`, `fire_spread/` and `scripts/fire_spread/` with
`--reload` (only those directories are watched, so run directories do not restart the server)
and `data/fire_spread/` read-write, and gives ELMFIRE the 2 GB `/dev/shm` it needs. On a Linux
host `data/fire_spread/` must be writable by uid 10001 (the container's `api` user).

Environment (see `../.env.example`, `fire_spread/settings.py` for all): `ELMFIRE_NPROC` (MPI
ranks, one per case, default 4), `DATA_DIR`, `RUNS_DIR`, `KEEP_RUNS=all|failed|none`,
`MAX_CONCURRENT_RUNS`, `DOMAIN_SIZE_M`, `CELL_SIZE_M` (default: the tier's), `IGNITION_FRAC`
(0.5 = centred), `WEATHER_GRID_N` (4 → 10 km weather points on a 40 km domain; 1 = uniform),
`WEATHER_ENSEMBLE` (false → perturb the deterministic forecast), `OPEN_METEO_ENSEMBLE_MODEL`
(`icon_eu_eps` 13 km/40 members but no humidity → members reuse the deterministic RH;
`ecmwf_ifs025` 25 km/50 members, all variables), `WEATHER_HISTORY_HOURS`,
`LH_MOISTURE_PCT`/`LW_MOISTURE_PCT`/`FOLIAR_MOISTURE_PCT` (override the monthly tables),
`PIPELINE_MODE` (`tuned` | `base`), and the mode-controlled knobs — `ADJ_FACTOR` (global
spread-rate multiplier; 1.4 tuned / 1.0 base), `DIURNAL_ADJUSTMENT` + `OVERNIGHT_ADJUSTMENT_FACTOR` (0.7 tuned),
`MAX_LOW` (fire ellipse length/width cap, 8), `WIND_FLUCTUATIONS`, `FUEL_MODEL_SET`
(`scott_burgan` | `mediterranean`, see below) — which pin that knob in both modes when set;
`HINDCAST`, `USE_BARRIERS`, `CROWN_RATIO`, `SPOTTING_DEFAULT`, `PERIMETER_MAX_IGNITIONS` (100),
`OPEN_METEO_BASE_URL`,
`OPEN_METEO_ENSEMBLE_BASE_URL`, `OPEN_METEO_API_KEY` (commercial key → `customer-*` hosts; the
free tier is 10 000 weighted calls/day per IP and one simulation costs ~50–100),
`WEATHER_CACHE_TTL_S` (identical live requests within 10 min reuse the answer), `WEATHER_CACHE_DIR`
(past weather for hindcasts/evaluation is kept on disk for good under `data/fire_spread/weather_cache/`,
so reruns cost no quota), `WEATHER_FIXTURE` (JSON
file instead of Open-Meteo). Open-Meteo 429/5xx answers are retried (5 s, 15 s on the live route; batch scripts wait out the minutely
limit with 15/65/65 s). ELMFIRE needs a large `/dev/shm` (`shm_size: 2gb` in compose; `--shm-size=2g` with plain `docker run`).

All sources download automatically (Copernicus DEM from AWS, ZAFM fuel from Zenodo, ICGC canopy
from `datacloud.icgc.cat`, ZHR from `interior.gencat.cat`, fire perimeters from
`agricultura.gencat.cat`, OSM from Geofabrik), ~2 GB into `data/fire_spread/raw/`.

## Debugging

Every run is a directory `data/fire_spread/runs/<run_id>/` with `request.json`, `inputs/*.tif`,
`inputs/ignitions.csv`, `weather/*.tif`, `elmfire.data`, `elmfire.out`,
`outputs/time_of_arrival_*.tif`, `timings.json`.
Open anything in QGIS; re-run by hand with `cd data/fire_spread/runs/<id> && elmfire elmfire.data`.
`?debug=1` returns the run id/dir, per-step timings and the tail of ELMFIRE's stdout.

CLI (no HTTP; prefix with `docker compose run --rm api` to use the container's ELMFIRE):

```sh
python -m fire_spread.cli --lat 41.59 --lon 1.83 --members 4 --out grid.json
python -m fire_spread.cli --lat 41.59 --lon 1.83 --weather-fixture tests/fire_spread/fixtures/weather_west_30mph.json
python -m fire_spread.cli --lat 41.59 --lon 1.83 --synthetic --constant-wind 9 270   # no static data needed
python -m fire_spread.cli --lat 41.59 --lon 1.83 --elmfire-data-only                  # stop after writing the run dir
python -m fire_spread.cli --lat 41.59 --lon 1.83 --spotting --no-ensemble --fuels mediterranean
python -m fire_spread.cli --lat 41.59 --lon 1.83 --mode base                          # stock ELMFIRE knobs
python -m fire_spread.cli --perimeter fire.geojson --hours 6                          # active perimeter (geometry or Feature)
```

Results are deterministic for a given `startTime` (the minute offset inside the ignition hour is
part of the run): the ELMFIRE `SEED` is derived from lat/lon/startTime (or `?seed=`).

## Tests

```sh
cd backend
.venv/bin/pytest tests/fire_spread     # unit tests, run anywhere (no ELMFIRE, no data)
scripts/test_fire_spread.sh            # real ELMFIRE on a synthetic landscape, inside the api container
```

Unit tests cover the modes (`test_modes.py`: knob bundles, env precedence, what reaches
`elmfire.data`), the perimeter fire state (`test_perimeter.py`), the router (GET/POST, `mode=`),
and the evaluation report (`test_evaluate.py`); the container tests run real ELMFIRE for a point,
a perimeter, both modes, ensembles, barriers and spotting.

The image carries neither `tests/` nor the dev dependencies; the script mounts and installs them
on the fly. `pytest.ini` at `backend/` registers the `elmfire` marker; those tests skip when the
binary is absent.

## Response

```json
{
  "originLat": 41.58, "originLon": 1.82, "cellDegLat": 0.000449, "cellDegLon": 0.000601,
  "cellSizeM": 50, "durationMinutes": 1440, "ensembleMembers": 16,
  "arrivalHours":      [[null, 1, ...], ...],
  "arrivalMinutes":    [[null, 42.5, ...], ...],
  "arrivalMinutesP10": [[null, 35.0, ...], ...],
  "arrivalMinutesP90": [[null, 61.0, ...], ...],
  "burnProbability":   [[0.0, 0.94, ...], ...],
  "weather": {"source": "open-meteo:best_match+icon_eu_eps", "windSpeedAvgMs": 6.1, "windDirectionAvg": 281,
              "fuelMoisture1hAvgPct": 7.2, "fuelMoisture100hAvgPct": 12.1, "weatherMembers": 16, ...},
  "physics": {"mode": "tuned", "modeKnobs": {"fuel_model_set": "mediterranean", "adj_factor": 1.0, ...},
              "spotting": false, "barriers": true, "diurnalAdjustment": true, "sunriseUtc": 5.6,
              "sunsetUtc": 17.9, "weatherGrid": 4, "cellSizeM": 50, "ignitionOffsetS": 1200,
              "fuelModelSet": "mediterranean", "adjFactor": 1.0, "perimeterIgnitions": 0},
  "zone": {"name": "...", "dominantFireType": "...", "designFires": {}, "properties": {...}}
}
```

Cell `(row, col)` covers `[originLon + col·cellDegLon, +cellDegLon) × [originLat + row·cellDegLat,
+cellDegLat)`; row 0 is the southernmost row, col 0 the westernmost. `null` = no member reached the
cell within the horizon (or outside data coverage). The ignition cell holds `0`.
`arrivalHours = ceil(arrivalMinutes / 60)` is the field of the original Deepfire-based contract:
`GET` without `detail=true` answers only `originLat, originLon, cellDegLat, cellDegLon, arrivalHours`
at `cellSizeM=100` (the previous route's resolution), so its consumers are unaffected. `POST`
always answers the full model. `compat.py` gives the decision layer (`app/decision`) the
`get_deepfire().run_simulation_detailed()` interface it was written against: per hour, one WGS84
perimeter per probability level 0.1–1.0 (the cells at least that fraction of the ensemble had
reached by then, from `burnProbability` and the P10/median/P90 arrival), the shape Deepfire produced.

Errors: 422 ignition outside Catalonia coverage or on a non-burnable cell, perimeter with no
burnable cell or not fitting the domain (**400** through the FireProtector API, which also
wraps every error as `{"error": {"code", "message"}}`) · 502
weather provider failure · 503 all simulation slots busy, or static tier not built · 504
ELMFIRE timeout.

## Fuel model sets

ELMFIRE's Rothermel parameters come from a CSV (`FUEL_MODEL_FILE`); the pipeline writes
`inputs/fuel_models.csv` per run from `fire_spread/data/`:

* `scott_burgan` (`base` mode) – ELMFIRE's own table (Anderson 13 + Scott & Burgan 40), i.e. the
  US parameterisation the ZAFM fuel map's codes refer to.
* `mediterranean` (`tuned` mode) – `fuel_models_mediterranean.csv`: the codes ZAFM assigns in Catalonia
  (GR4, SH2/5/7/8/9, TU1/2/3/5) re-parameterised in metric units for garriga/maquia, *Pinus
  halepensis* stands and cereal stubble — lighter and shallower shrub beds than the chaparral
  originals (SH5/SH7 are 1.8 m deep, 20–30 t/ha), finer shrub foliage (SAV 50–60 /cm),
  dead moisture of extinction 25–30 % as observed in live-shrub-dominated Mediterranean beds.
  Same code numbers, so the fuel raster is untouched. On the reference ignition it burns
  ~30 % less area than the US set. **Provisional**: literature-range central estimates
  (Baeza et al. 2002, Mitsopoulos & Dimitrakopoulos 2007, Fernandes 2009, Duce et al. 2012),
  not hindcast-calibrated — treat as the starting point for the calibration loop below, not
  as validated. Edit the metric CSV; `fuels.py` converts to ELMFIRE's lb/ft², 1/ft, ft, BTU/lb.

## Input quality (assessed 2026-09-20)

What each input is, how good it is, and what would improve it:

| Input | Source / vintage | Assessment |
|---|---|---|
| Fuel map `fbfm40` | ZAFM-DW 2026, 10 m → 50 m mode | Global Dynamic-World-derived product, not a field-validated Catalan map. Histogram: SH7 (very-high-load chaparral) is the dominant shrub class (2.6 M cells) and TU1/TU5 the forest classes; GR4 (2.3 M cells) is *all* agriculture, including irrigated orchards that should be non-burnable in summer. 33 % of "shrub" cells have LiDAR canopy cover ≥ 40 % (open woodland classified as shrub). Biggest single source of bias → the `mediterranean` set + hindcast calibration; longer term the CREAF/ICGC land-cover map (MCSC) or the Mapa Forestal for a Catalan fuel map. |
| Canopy `cc, ch, cbh, cbd` | ICGC/CREAF LiDAR biophysical variables 2016–17, 20 m | Good cover and height (FCC, HM measured). CBH = 0.4·HM and CBD = biomass/(HM−CBH) are heuristics; CBD median 0.06 kg/m³ in timber is in the *P. halepensis* range (0.05–0.15). 9–10 years old: post-2017 fires are handled by `burnyear.tif`, growth/thinning is not. |
| Topography | Copernicus GLO-30 (30 m, resampled to 50 m) | Fine for Rothermel slope; it is a surface model (includes canopy), so slope is inflated at forest edges. ICGC's 2 m LiDAR DTM would fix that and support a 30 m tier (`--res 30`). |
| Burn scars | DARP perimeters 2012–2024 | Good; remap rules (< 2 y bare, 2–6 y GR2) are assumptions. |
| Barriers | OSM roads + waterways, width by class | Complete for roads; widths are class averages, not measured; agricultural field margins and firebreak strips are not in OSM. |
| Weather | Open-Meteo `best_match` (AROME 1.3 km) on a 4×4 grid; ICON-EU-EPS members | Hourly, spatially varying at ~10 km. Wind is model-grid 10 m wind, **not terrain-adjusted** to the 50 m cells — valley channelling and ridge acceleration are missing (the Dogrib validation shows this is the dominant error source in complex terrain). WindNinja on the DEM is the standard fix. ICON-EU-EPS has no humidity for members (reuses the deterministic RH). |
| Dead fuel moisture | Simard EMC + 1/10/100-h exponential lag, 48 h spin-up, rain wetting | Standard NFDRS-style; no solar-radiation/shading term, so south-facing slopes are not drier than north-facing. |
| Live / foliar moisture | Monthly climatology | Coarse. Satellite LFMC (e.g. MODIS/Sentinel-2 products) or Bombers/GRAF field sampling via `MLH/MLW/FMC` rasters would replace it. |

Coverage: the static tier covers 41 % of its bounding box (Catalonia), 10 % of which is
non-burnable; no nodata holes inside coverage.

## ELMFIRE notes worth knowing

Verified against the source (`build/source/*.f90`) of the pinned commit; several contradict the
user guide at elmfire.io.

* **`NUM_METEOROLOGY_TIMES` must equal the number of hourly bands a case uses**; with the
  default 1 the weather band index is only evaluated on the first time step and the whole run
  uses band 1 (`elmfire_level_set.f90`).
* Per-case weather streams need the fixed-ignitions CSV path (`RANDOM_IGNITIONS` +
  `CSV_FIXED_IGNITION_LOCATIONS`, rows `icase, iband, x, y, astop, tstop`); with plain point
  ignitions every case starts at `METEOROLOGY_BAND_START`. Simulation time and arrival times are
  then absolute (band 1 = 0 s), so the pipeline subtracts `(iband-1)·3600 + TSTART`.
* `SIMULATION_TSTART` = seconds into the ignition hour (band 1 = that hour), as the guide's
  "14:20 → 1200" rule; `TSTOP = TSTART + duration`.
* Wind fluctuation intensities are **both fractions**: speed factor `1 + I·(r−0.5)`, direction
  offset `I·(r−0.5)·360°` (one global draw per `DT_WIND_FLUCTUATIONS`). The guide's "degrees"
  is wrong: 15.0 makes the direction random and the fire circular. 0.1 → ±18°.
* Perturbations are additive; `GAUSSIAN` (`PDF_MEAN/PDF_SIGMA`) and `LOGNORMAL` exist on main
  (only `UNIFORM` on the 2025.0717 tag). Units: `WS` mph, `WD` deg, `ADJ` dimensionless, dead
  fuel moistures in **percent** when `DEAD_MC_IN_PERCENT` (fractions on the 2025.0717 tag).
* Weather rasters may be coarser than the fuels; with `WX_BILINEAR_INTERPOLATION` ELMFIRE rounds
  to the *upper* weather cell and clamps, so the last 1.5 cells on the right/top are degenerate
  and the corner divides by zero (a 1×1 grid → NaN wind, no spread). The pipeline pads the
  weather grid by two edge-replicated cells beyond the domain and disables bilinear for 1×1.
* Overnight damping (`USE_DIURNAL_ADJUSTMENT_FACTOR`) compares `FORECAST_START_HOUR + T/3600`
  (UTC) with sunrise/sunset computed from `CURRENT_YEAR`/`HOUR_OF_YEAR` at the domain corner;
  `SUNRISE_HOUR/SUNSET_HOUR` are no longer inputs on main. With several weather blocks the
  block length must be a multiple of 24 h so the clock stays aligned (the pipeline pads).
* A fire that cannot spread at ignition (e.g. night, dead fuel above the moisture of extinction)
  ends the case immediately ("stalled front") — a night-time request can legitimately return
  only the ignition cell even if the afternoon would burn.
* `WS_AT_10M = .TRUE.` (ELMFIRE scales 10 m wind to 20 ft by 0.87). `DT_METEOROLOGY = 3600`.
* Barriers: Float32 raster of break width (m); surface spread stops where
  `1.5 × flame length ≤ width`; forces `BANDTHICKNESS = 1`; ignored by spotting.
* Spotting (opt-in, slower): UMD stack `PER-MW` generation, `EMPIRICAL` (Sardoy) landing pdf,
  `EULERIAN` accumulation, `DIRECT` ignition with `PIGN = 100` and low generation percentages
  (same statistics, far cheaper than tracking embers that never ignite).
* Upstream bugs patched in the Dockerfile: (1) `elmfire_level_set.f90` deallocates a pointer into
  the burned-cell list (`free(): invalid pointer` under glibc 2.39) → `NULLIFY(C)`; (2) the barrier
  raster is read by one MPI rank and only its header broadcast, so `USE_BARRIERS` segfaults with
  `-np > 1` → the data is broadcast after the header.

## Evaluation: base vs tuned on historical fires

`scripts/evaluate_fire_spread.sh` (→ `scripts/fire_spread/evaluate.py`) is the evaluation
execution. It runs both modes through the same pipeline as the API, swapping live weather for
Open-Meteo's **historical forecast** archive (past runs of the same high-resolution models;
`--weather archive` = ERA5) and ignoring the ignition-year burn scar (`HINDCAST=true`), on the
fires in `scripts/fire_spread/eval_fires.json`: Catalan wildfires that burned free for long
enough that a no-suppression simulation is comparable with the final DARP perimeter. Each entry
gives the ignition time and free-burning horizon (approximate, from public briefings — edit and
rerun); the ignition point is the upwind-most burnable perimeter vertex unless `ignition` is
given. Both modes get the same fires, points, weather and seeds. Past weather is fetched once
and cached on disk (`data/fire_spread/weather_cache/`), so only the first run of a fire set
spends Open-Meteo quota (the free tier is 10 000 weighted calls/day per IP; one fire is ~50-100).

```sh
cd backend
scripts/evaluate_fire_spread.sh                          # 8 fires x 2 modes, 4 members: 30-60 min
scripts/evaluate_fire_spread.sh --limit 2 --members 2    # smoke run
scripts/evaluate_fire_spread.sh --modes tuned --fires 2022250092,2022250084 --weather archive
```

Knob experiments without editing `modes.py`: an explicitly set environment variable pins the knob,
e.g. `docker compose run --rm -e ADJ_FACTOR=1.3 -e OVERNIGHT_ADJUSTMENT_FACTOR=0.7 --no-deps api
python -m scripts.fire_spread.evaluate --modes tuned --out data/fire_spread/hindcast/eval_adj13`.

Output under `data/fire_spread/hindcast/eval_<timestamp>/`: `base.csv`, `tuned.csv` (per fire:
Jaccard, Sørensen, area bias, recall, precision, `elmfire_s`, `prep_s`, `wall_s`),
`summary.json` and `summary.md` — paired per-fire table, per-mode aggregates (mean/median
Jaccard, Sørensen, bias quartiles, recall/precision) and timing (median/max wall, ELMFIRE and
preparation seconds; the same work in both modes, reported so a knob that shrinks ELMFIRE's time
step shows up), plus a "Jaccard wins" count. Paste the table into
[`docs/elmfire-pipe-assessment.md`](../../docs/elmfire-pipe-assessment.md).

## Calibration: the hindcast loop

`scripts/fire_spread/hindcast.py` is the general form of the above: replays *any* DARP fire
perimeters (`--years`, `--min-ha`, `--limit`, `--fires`) with one mode and optional knob
overrides. Per fire: perimeter + date (DARP has no ignition point, time or duration) → historical
weather (`--weather historical|archive`; statistical wind perturbations since there is no past
ensemble) → ignition at the most upwind burnable point of the perimeter at `--ign-hour` UTC →
`--hours` run → burn probability ≥ `--pmin` vs the observed perimeter: Jaccard, Sørensen, area
bias.

```sh
cd backend
docker compose run --rm api python -m scripts.fire_spread.hindcast --min-ha 100 --limit 10
# --mode base|tuned, then overrides: --fuels mediterranean  --adj 0.8  --max-low 6  --hours 36  --spotting  --no-barriers  --members 8
docker compose run -d --name hindcast api bash scripts/fire_spread/hindcast_batch.sh "--mode base" "--mode tuned"
```

Results land in `data/fire_spread/hindcast/<tag>.csv`; the summary line is formatted for the log in
[`docs/elmfire-pipe-assessment.md`](../../docs/elmfire-pipe-assessment.md), which also holds the
input/knob audit. Knobs in the order the docs treat them as calibration coefficients: `ADJ_FACTOR`
(global), then pyrome tables (`USE_PYROMES` + `ADJUSTMENT_FACTORS_FILENAME`: multiplier per
fire-regime zone × fuel model — the Bombers ZHR layer is a ready-made pyrome map),
`PHIW_ADJ`/`PHIS_ADJ`, `MAX_LOW`, `CROWN_FIRE_ADJ`, `OVERNIGHT_ADJUSTMENT_FACTOR`, spotting
ranges. Reference: CloudFire's CONUS validation gets mean Jaccard 0.18 (FARSITE 0.18).
Multi-day fires read as under-prediction at 24 h and contained fires as over-prediction (no
suppression) — read the bias column with the fire's story in mind.
