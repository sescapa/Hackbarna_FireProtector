# ELMFIRE pipeline assessment

Living document for `backend/fire_spread/`: what ELMFIRE takes, what we feed it, how good each
piece is, and what the hindcast says. Update the **Status** table and the **Hindcast log** when
something changes; keep the rest short. Namelist names are verbatim; "ours" is what the pipeline
writes into `elmfire.data` (see `fire_spread/elmfire_config.py`).

_Last updated 2026-09-20 · ELMFIRE main @ `cbf924a` · tier 50 m · modes `base` / `tuned`._

## 0. Pipeline modes

The service runs one of two knob bundles (`fire_spread/modes.py`; `PIPELINE_MODE`, default
`tuned`; `?mode=` per request). Both use every input in §1 unchanged; §2 describes `tuned`.
The exposed API is the one the Deepfire-backed service had: `GET /fire/arrival-grid?lat&lon`
answers the same five keys at 100 m (`detail=true` for the full ensemble output), and the
decision layer keeps calling `get_deepfire().run_simulation_detailed()` — `fire_spread/compat.py`
vectorises the ensemble grid into the hourly perimeters it expects.

| Knob | `base` (ELMFIRE namelist default) | `tuned` (ours) |
|---|---|---|
| `FUEL_MODEL_FILE` | Scott & Burgan 40 as shipped | `mediterranean` re-parameterisation |
| `USE_DIURNAL_ADJUSTMENT_FACTOR` / `OVERNIGHT_ADJUSTMENT_FACTOR` | F / 0.1 | T / 0.7 |
| `WIND_FLUCTUATIONS` (0.2 speed, 0.1 direction, 30 s) | F | T |
| `LH/LW/FOLIAR_MOISTURE_CONTENT` | 60 / 60 / 90 % constant | monthly Catalan climatology |
| `ADJ` | 1.0 | 1.4 (fitted on the free-burning set, §7) |
| `MAX_LOW` | 8 | 8 |

`scripts/evaluate_fire_spread.sh` scores both on the free-burning set in
`scripts/fire_spread/eval_fires.json` (§7). A tweak enters `tuned` only through that loop.

## 1. Inputs

Grades: **good** fit for purpose · **ok** usable, known bias · **weak** dominant error source.

| Input | ELMFIRE expects | We feed | Grade | Notes |
|---|---|---|---|---|
| `FBFM` fuel model | S&B 40 / Anderson 13 codes | ZAFM-DW 2026 (10 m → 50 m mode); DARP scars < 2 y → NB9, 2–6 y → GR2 | **weak** | Global Dynamic-World product, not field-validated. SH7 (chaparral) dominant shrub class; GR4 = *all* agriculture incl. irrigated orchards; 33 % of "shrub" cells have LiDAR canopy ≥ 40 %. |
| `DEM/SLP/ASP` | m, deg, deg | Copernicus GLO-30 → gdaldem | ok | Surface model: slope inflated at forest edges. ICGC 2 m DTM → fix + 30 m tier. |
| `CC/CH/CBH/CBD` | %, m×10, m×10, kg m⁻³×100 | ICGC/CREAF LiDAR 2016–17 | ok | Cover/height measured. CBH = 0.4·HM, CBD = biomass/(HM−CBH) are heuristics; 9–10 y old. |
| `ADJ` spread multiplier | Float32 | `ADJ_FACTOR` (1.4 in `tuned`, 1.0 in `base`), uniform | ok | Primary calibration surface; global scalar only so far. |
| `PHI` initial fire | < 0 burning | 1.0 (CSV ignition) | good | Perimeter ignition is one raster away. |
| `BARRIER` | width m | OSM roads + waterways by class | ok | Class-average widths; field margins / firebreak strips missing. |
| `WS/WD` | 20 ft mph (10 m accepted) | Open-Meteo best_match (AROME 1.3 km) 4×4 grid + ICON-EU-EPS members | **weak** | Not terrain-adjusted: no channelling / ridge speed-up (WindNinja). |
| `M1/M10/M100` | % | Simard EMC + 1/10/100 h lag, 48 h spin-up, rain | ok | No aspect/solar term. |
| `LH/LW/FMC` live | % | monthly Catalan climatology | ok | Coarse; satellite LFMC or GRAF sampling via rasters. |
| `FUEL_MODEL_FILE` | Rothermel table | `scott_burgan` (default) / `mediterranean` (provisional) | ok | Mediterranean set is literature-range, uncalibrated. |
| `IGNITIONS_CSV` | case, band, x, y | one row per case on its own NWP block | good | Only path giving per-case weather. |
| Pyromes, assets, SDI, WUI rasters | — | unused | — | ZHR zones ready as pyromes. |

## 2. Knobs (ELMFIRE default → ours)

**INPUTS** `DT_METEOROLOGY` 3600 · `WS_AT_10M` T · `DEAD_MC_IN_PERCENT` T · canopy unit switches T ·
`USE_BARRIERS` T if `barrier.tif` · `SURFACE_SPREAD_MODEL` ROTHERMEL · live moisture constant.

**TIME_CONTROL** `SIMULATION_TSTART` = minute offset into the ignition hour, `TSTOP` = +duration ·
`DT/DTMAX/CFL` 5/300/0.4 · `USE_DIURNAL_ADJUSTMENT_FACTOR` T, `OVERNIGHT_ADJUSTMENT_FACTOR` 0.7
(default 0.1; 0.4 double-counted the night with the hourly lagged moisture, §7), burn period 10 h / 0.667 · `FORECAST_START_HOUR`, `CURRENT_YEAR`, `HOUR_OF_YEAR` real
(UTC; sunrise/sunset computed by ELMFIRE at the domain corner).

**SIMULATOR** `NUM_IGNITIONS` 0 (CSV path) · `CROWN_FIRE_MODEL` 1 (Cruz), `CRITICAL_CANOPY_COVER`
0.39, `CROWN_RATIO` 1.0, `MAX_LOW` 8 (env) · `WIND_FLUCTUATIONS` T, speed 0.2, direction 0.1
(**fraction of 360°**, ±18°; docs wrongly say degrees), every 30 s · `WX_BILINEAR_INTERPOLATION` T
(grid padded past the NaN corner) · `MAX_RUNTIME` = timeout − 30 s · defaults: `PHIW_ADJ`,
`PHIS_ADJ`, `WSMFEFF_LOW_MULT`, `CROWN_FIRE_ADJ`, `CROWN_FIRE_SPREAD_RATE_LIMIT`, `BANDTHICKNESS`.

**MONTE_CARLO** `RANDOM_IGNITIONS` + `CSV_FIXED_IGNITION_LOCATIONS` T · `NUM_ENSEMBLE_MEMBERS` =
cases · `NUM_METEOROLOGY_TIMES` = bands per block (**1 freezes weather at band 1**) ·
`METEOROLOGY_BAND_START/STOP/SKIP_INTERVAL` = 1 / last block / block length · `SEED` from request ·
perturbations: `M1` GAUSSIAN σ 1.5 %, `ADJ` UNIFORM −0.2..+0.25, plus `WS`/`WD` GAUSSIAN σ from the
NWP spread only when no ensemble members are available.

**SPOTTING** off by default; `?spotting=1` → `PER-MW` generation, `EMPIRICAL` (Sardoy) distance,
`EULERIAN` accumulation, `DIRECT` ignition `PIGN` 100, crown 2 %, surface 0.5 % above 1000 kW/m.

**SUPPRESSION / WUI / CALIBRATION / SMOKE** unused. Grids are free-burning, no suppression, urban
cells are walls; pyrome tables empty.

**OUTPUTS** `DUMP_TIME_OF_ARRIVAL` only; flame length / intensity rasters off.

## 3. Strengths

- Real NWP ensemble member per case (wind, T, RH, rain co-vary) — the only code path that allows it.
- Weather advances hour by hour (frozen-band bug fixed); ignition minute honoured; lagged fuel
  moisture with 48 h history and rain.
- 4×4 spatial weather grid; OSM fire breaks (−⅓ area on the reference fire; upstream MPI bug patched).
- Night damping; burn-scar fuel remap; LiDAR canopy; monthly live/foliar moisture.
- Uncertainty from distinct members + model-error perturbations; reproducible seed; every run is an
  inspectable directory; 63 unit + 6 real-ELMFIRE tests.
- Every knob checked against the Fortran; five doc errors caught (fluctuation units, sunrise inputs,
  band semantics, bilinear corner, perturbation units).

## 4. Flaws, ranked by expected impact

1. **No calibration** — `ADJ = 1`, pyrome tables empty. First hindcast (18 fires): Jaccard 0.16–0.18, median area bias 2.8–4.6× — over-prediction is suppression-shaped, so calibrate on free-burning cases or add the extended-attack model first.
2. **Fuel map** — ZAFM-DW classes (see §1). Real fix: Catalan land-cover crosswalk (MCSC / Mapa Forestal).
3. **Wind not terrain-adjusted** — WindNinja downscaling.
4. **Spotting off by default**, untested on real fires.
5. **50 m cells on a DSM slope** — `--res 30` exists; needs a real DTM.
6. Live/foliar moisture monthly table; canopy CBH/CBD heuristics (2016–17).
7. ICON-EU-EPS lacks humidity (members share deterministic RH).
8. Night ignitions above the moisture of extinction end at t 0 (model behaviour; must be explained).
9. No suppression, no WUI spread (must be labelled).
10. Only arrival time output (flame length / intensity one flag away).

## 5. Status

| Item | State | Since |
|---|---|---|
| Hindcast loop (`scripts/fire_spread/hindcast.py`) | built; DARP perimeters, historical-forecast or ERA5 weather, Jaccard/Sørensen/bias, `--mode` | 2026-09-20 |
| Evaluation execution (`scripts/fire_spread/evaluate.py`) | built; base vs tuned, paired per fire, timing; first run in §7 | 2026-09-20 |
| Active-perimeter fire state (`POST /fire/arrival-grid`) | built; ≤100 fixed boundary ignitions (PHI raster is ignored on the random-ignition path) | 2026-09-20 |
| Global `ADJ` calibration | done on the free-burning set: `tuned` ADJ 1.4 (§7); a preference for slight over-burn | 2026-09-20 |
| Pyrome × fuel tables | not started | |
| Fuel set decision (`scott_burgan` vs `mediterranean`) | `mediterranean` is the `tuned` default: J 0.177 vs 0.155 on the 18-fire batch, and `tuned` beats `base` 6/8 on the free-burning set (§7) | 2026-09-20 |
| WindNinja | not started | |
| Catalan fuel map crosswalk | not started | |
| Spotting validation | not started | |
| Satellite LFMC / ICGC DTM | not started | |

Hindcast caveats: DARP gives perimeter, date and municipality only — no ignition point, time or
duration. The loop places the ignition at the most upwind cell of the perimeter (mean ERA5 wind
over the fire afternoon), ignites at 12:00 UTC, runs 24 h (`--hours`) and scores against the final
perimeter, so multi-day fires read as under-prediction. FIRMS hotspots would give real ignition
points and timing.

## 6. Hindcast log

| Date | Cases | Settings | Jaccard mean/median | Sørensen | Area bias median (p25–p75) | Notes |
|---|---|---|---|---|---|---|
| 2026-09-20 | 18 (2019–2024, ≥100 ha; 2 skipped at the coverage edge) | `scott_burgan_adj1_h24`, 4 members, pmin 0.5, barriers on | 0.155 / 0.150 | 0.252 | 4.62 (0.77–5.87) | ERA5 weather, 24 h, no suppression |
| 2026-09-20 | same 18 | `mediterranean_adj1_h24` | **0.177** / 0.138 | **0.278** | **2.82** (0.56–6.52) | better on 11/18 fires; on par with CloudFire's CONUS mean |

Per-fire results: `backend/data/fire_spread/hindcast/<tag>.csv`, log in `batch.log`.

What the first batch says:

- **Over-prediction dominates** (median bias 2.8–4.6): most 100–500 ha fires were contained
  by Bombers within hours; the model burns freely for 24 h. Do **not** tune `ADJ` on this bias —
  it would be compensating for suppression, not spread rate. Calibrate on the free-burning
  fires (Artesa de Segre 2022: bias 0.97–1.05, J 0.40–0.42; Castellar de la Ribera: J 0.53
  Mediterranean) and on the first hours of the large ones, or add the extended-attack model.
- **Two catastrophic over-predictions** — Ciutadilla 2024 (234 ha → 43 000 ha S&B, 10 800 ha
  Mediterranean) and Cabacés 2024 (36×): both in the Lleida/Priorat cereal mosaic in July/September,
  where ZAFM's GR4 "agriculture" burns as continuous cured grass. This is the fuel-map flaw (§4.2)
  measured; harvested/irrigated agriculture needs an NB or seasonal class.
- **Under-prediction of the big multi-day fires** as expected at 24 h (Ribera d'Ebre 2019: 0.43–0.55).
  El Pont de Vilomara 2022 (1580 ha in one afternoon) at 0.01–0.09 is not explained by duration:
  ignition placement or ERA5 wind (real fire was driven by a local W-NW wind and spotting)
  — a case for WindNinja + spotting.
- **Mediterranean fuel set** lowers bias and raises Jaccard on average without a hindcast-fitted
  parameter; keep it provisional but it is now the better-supported default candidate.
- Winter/spring fires (Roses Feb 2022, Portbou Apr 2023, Naut Aran Jan 2019: bias 3–7) run on
  the July-style live-moisture table only through the month lookup; Tramuntana fires spot.

Reference for expectations: CloudFire's CONUS validation (WildfireAV) reports mean Jaccard 0.178,
Sørensen 0.278 for ELMFIRE (FARSITE 0.176 / 0.274).

## 7. Evaluation log: base vs tuned (free-burning fires)

`scripts/evaluate_fire_spread.sh` — same pipeline as the API on Open-Meteo historical
forecasts, ignition-year scar ignored, per-fire ignition time and free-burning horizon from
`eval_fires.json` (approximate), same seeds in both modes. Full tables in
`backend/data/fire_spread/hindcast/eval_<ts>/summary.md`.

| Date | Fires | Mode | Jaccard mean / median | Sørensen | Bias median (p25–p75) | Recall / precision | Wall s median (max) | ELMFIRE s median |
|---|---|---|---|---|---|---|---|---|
| 2026-09-20 | 8, 4 members, pmin 0.5, historical wx | `base` | 0.113 / 0.092 | 0.192 | 3.90 (2.20–10.21) | 0.65 / 0.20 | 34 (111) | 29 |
| 2026-09-20 | same | `tuned` v1: med fuels, night 0.4, ADJ 1.0 | 0.199 / 0.208 | 0.308 | 0.53 (0.20–1.42) | 0.39 / 0.45 | 23 (45) | 17 |
| 2026-09-20 | same | tuned, night 0.7 | 0.201 / 0.197 | 0.310 | 0.60 (0.22–1.73) | 0.41 / 0.44 | 21 (59) | 16 |
| 2026-09-20 | same | tuned, no night damping | 0.201 / 0.190 | 0.310 | 0.69 (0.23–2.04) | 0.44 / 0.42 | 23 (46) | 16 |
| 2026-09-20 | same | tuned, night 0.7 + ADJ 1.3 | 0.206 / 0.174 | 0.316 | 0.88 (0.27–2.82) | 0.49 / 0.40 | 27 (64) | 22 |
| 2026-09-20 | same | **`tuned` v2: night 0.7 + ADJ 1.4** (current) | **0.204** / 0.175 | **0.314** | **1.12** (0.36–3.27) | 0.52 / 0.37 | 26 (116) | 21 |
| 2026-09-20 | same | tuned, night 0.7 + ADJ 1.5 | 0.189 / 0.151 | 0.296 | 1.49 (0.40–3.96) | 0.55 / 0.34 | 24 (79) | 18 |

Jaccard wins (v2 vs base): tuned 6, base 2. Per fire (pred ha / J / bias, base → tuned v2):
Ribera d'Ebre 2019 36 h 17 406 / 0.22 / 2.86 → 17 243 / 0.21 / 2.84; Baldomar 2022 30 h
33 580 / 0.08 / 12.5 → 12 257 / 0.22 / 4.6; Castellar de la Ribera 2022 3 066 / 0.10 / 9.5 →
485 / **0.43** / 1.49; Corbera d'Ebre 2022 5 686 / 0.07 / 15.2 → 2 717 / 0.14 / 7.3; Portbou
2023 1 334 / 0.28 / 2.8 → 367 / **0.44** / 0.76; Santa Coloma de Queralt 2021 8 393 / 0.13 /
4.9 → 732 / 0.04 / 0.43; El Pont de Vilomara 2022 9 / 0.00 → 251 / 0.12 / 0.16; Batea 2024
242 / 0.02 / 0.56 → 52 / 0.03 / 0.12. Per-variant tables: `eval_v_*/summary.md`.

What it says:

- **`base` over-predicts by 4× median**: without night damping the fire keeps running through
  the night at daytime rates, and the chaparral shrub loads burn Catalan garriga far too hot.
- **`tuned` v1 over-corrected** (bias 0.53, recall 0.39). The night factor was not the cause:
  0.7 or off moved the median bias only to 0.60–0.69, because the hourly RH-driven lagged dead
  fuel moisture already recovers at night — ELMFIRE's factor was written for users without
  hourly weather, so 0.4 damped twice. The Mediterranean table's lighter, shallower shrub beds
  are the real cut (its higher moisture of extinction works the *other* way: less damping).
- **ADJ is the lever, and 1.4 is the knee**: 1.3 → bias 0.88 / J 0.206, 1.4 → 1.12 / 0.204,
  1.5 → 1.49 / 0.189. We prefer a little over-burn to under-burn, so v2 = night 0.7 + ADJ 1.4:
  recall 0.39 → 0.52 for ~the same Jaccard. Note that the ensemble's ADJ perturbation
  (−0.2..+0.25) sits on top: cases run at 1.2–1.65.
- **The recall floor is direction, not speed**: Santa Coloma, Batea and Vilomara predict
  hundreds of ha with J ≤ 0.12 — the simulated fire runs the wrong way (wind not
  terrain-adjusted, ignition vertex guessed, spotting off). No scalar fixes those; WindNinja
  and a spotting run are the next candidates, judged on this same table. Baldomar (30 h) and
  Corbera (18 h) over-predict in every variant: their "free-burning" horizons in
  `eval_fires.json` probably include hours of effective attack — shorten them if Bombers logs
  say so.
- **Timing**: both modes do the same work; ELMFIRE time follows burned area (base 34 s, tuned
  21 s median). Preparation (weather + rasters) is ~6 s in both. No knob changes the time step.
- The set is 8 fires with approximate ignition times; the signal (6/8, bias 3.9 → 1.1) is far
  larger than that noise, but do not read the third decimal.
