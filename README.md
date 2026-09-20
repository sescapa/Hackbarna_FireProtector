# Hackbarna_FireProtector

Wildfire values-at-risk tooling for Catalonia, built for the HackBarna 2026
Norrsken wildfire challenge. `backend/` is the asset register, the fire-spread
forecast and the decision layer that ranks one against the other. `frontend/`
is the command-centre UI.

This file is the map. [backend/README.md](backend/README.md) is the detail, and
you should read it before changing anything under `backend/`.
[CONTEXT.md](CONTEXT.md) is the glossary — what a _scenario_, a _reached set_
and _confidence_ mean here, and which words not to use.

## Team

- Simon Escapa
- Nil Macià
- Peter de Ruiter

`frontend/`, `backend/app/engine/` and `backend/app/briefing/` are ported from
[p-deruiter/fireprotector-decision-layer](https://github.com/p-deruiter/fireprotector-decision-layer)
at `abf23a3`, written by p-deruiter for this same team and hackathon.

![The command centre: ranked assets, arrival contours over the ICGC basemap,
scoring controls and ranking sensitivity](docs/screenshot.png)

## What this system does

A fire spread forecast describes where a fire will be in N hours. The other
half of the question is **what is in that area that we care about.** This repo
holds both halves and the judgement that joins them:

- an _asset register_ — fixed things with a location, an importance and a
  susceptibility to fire — from Postgres (`GET /assets`);
- a _fire spread forecast_ — `GET /fire/arrival-grid` runs a self-hosted
  ELMFIRE ensemble from an ignition point and returns the hour the fire reaches
  each 100 m cell;
- a _decision layer_ under `/api` — takes an ignition scenario, works out which
  assets the fire reaches and when, ranks them by risk, says how much that
  ranking can be trusted, and writes a briefing about it in three languages.

**An ignition scenario is a hypothetical, not a detected fire.** Nothing here
watches for real ignitions: every forecast starts from a coordinate somebody
chose, which is why a scenario has no detection time and no containment status.
Calling them fires would be the single easiest way to mislead someone reading
the screen.

## The one thing to know first

`GET /assets` implements a **contract shared with other people's code**: the
frontend and the decision layer are written against it. Its request and response
shapes are not ours to change unilaterally, and several of its rules fail
_silently_ rather than loudly when broken. Those rules are listed under
[Contract invariants](backend/README.md#contract-invariants) — read them before
touching `backend/app/routers/assets.py` or `backend/app/schemas.py`.

## Current state

| Piece                                       | State                                                                                                                           |
| ------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `GET /assets`                               | Working. Serves 4,269,286 point assets. Points only — forests are not included                                                  |
| `GET /building_specs`, `POST /add_building` | Working. Internal, not part of the contract                                                                                     |
| `GET /fire/arrival-grid`                    | Working. Self-hosted ELMFIRE (no credentials); needs the static tier from `backend/scripts/setup_fire_data.sh`. Not part of the contract |
| `/api/*` (decision layer)                   | Working. Scenarios, spread contours, scoring, sensitivity, briefings. Serves recorded bundles when the fire spread is unavailable |
| `frontend/`                                 | Working. React + MapLibre, talks only to `/api`                                                                                 |
| `protection.asset_specs`                    | Loaded — the INSPIRE building register for Catalonia                                                                            |
| `protection.forest_areas`                   | Loaded — the INSPIRE public forests of Catalonia. **Not served by any endpoint**; query it directly                             |
| `value` (importance)                        | **Real.** Assigned from `asset_type` by `db/init/03_asset_values.sql`                                                           |
| `vulnerability`                             | **Real** on `asset_specs`, from the same table. Still `random()` on `forest_areas`, which no endpoint serves                    |
| Asset names                                 | **Still a placeholder.** Every register row is `"residential"` — the source register has no names                               |

### What still is not real

**Nothing in the register has a name.** All 4,269,286 rows are called
`"residential"`, including the one row typed `hospital`. `value` and
`vulnerability` now come from the asset's type, so a ranking of the register is
honest — but it can only ever say _how many_ homes and storage tanks a fire
reaches, never _which_ ones.

Named assets come from OpenStreetMap instead, queried live per scenario. That
is the only source of a hospital or a school by name, and it is the one hard
dependency on a third party at demo time. When it fails, the ranked list still
renders from register rows and `/api/.../score` reports it:

```json
"named_layer": { "available": false, "count": 0, "error": "..." }
```

⚠️ **Only ever point `OVERPASS_URLS` at a planet-wide mirror.** A regional
mirror answers a Catalonia query with HTTP 200 and zero results, which is
indistinguishable from "there is no hospital here" — it does not fail, it lies.

The INSPIRE register also publishes ~25,800 _named_ facilities (`US.Health`,
`US.Education`, `US.SocialService`, `US.PublicOrderAndSafety`,
`PF.ProductionFacility`) that nothing loads yet. Loading them is the way to
drop the OpenStreetMap dependency — see [Replacing the
placeholders](backend/README.md#replacing-the-placeholders).

## Getting it running

Needs **Docker**, **Python 3.11+** on the host for the loader, and **Node 20+**
for the UI.

From a fresh clone, in two terminals:

```bash
# 1. backend — Postgres, the schema, both datasets, then the API on :5102
cp backend/.env.example backend/.env     # optional; see below
./backend/scripts/setup_db.sh            # or setup_db_container.sh: Docker only, no host Python

# 2. frontend — the UI on :5173, proxying to :5102
cd frontend && npm install && npm run dev
```

Then open **http://localhost:5173**.

`backend/.env` is optional, and so is the fire-spread static tier: without it
the app still runs, serving the scenario bundles recorded under
`backend/data/bundles/`, which are committed. To simulate new ignitions (and
`GET /fire/arrival-grid`), build the tier once with
`./backend/scripts/setup_fire_data.sh` (~3 GB of open Catalan data; see
`backend/README.md`). No credentials are needed: the fire spread is a
self-hosted ELMFIRE pipeline.

### The backend, in more detail

`setup_db.sh` is one command: it starts Postgres, applies the schema, loads
both datasets (~11 minutes for the assets, ~10 seconds for the forests), then
builds and starts the API. It is safe to re-run — already-loaded municipalities
are skipped, so a second run takes seconds.

When it finishes:

|             |                                                                                                  |
| ----------- | ------------------------------------------------------------------------------------------------ |
| Assets      | http://localhost:5102/assets?bbox=1.0,41.6,1.6,42.0                                              |
| Fire spread | http://localhost:5102/fire/arrival-grid?lat=42.42&lon=2.87 (~20 s — it waits for the simulation) |
| Scenarios   | http://localhost:5102/api/scenarios                                                              |
| API docs    | http://localhost:5102/docs                                                                       |
| Postgres    | `postgresql://fireprotector:fireprotector@localhost:5432/fireprotector`                          |

All bound to loopback. **Port 5102 is fixed by the contract** — the frontend
looks for the API there.

Already set up? `cd backend && docker compose up -d`.

### The UI, in more detail

`npm run dev` serves on **5173** and proxies `/api` to the backend on **5102**.
Set `BACKEND_URL` to point somewhere else — the API running straight on the
host on another port, say.

|                 |                                              |
| --------------- | -------------------------------------------- |
| `npm run dev`   | Vite dev server                              |
| `npm run build` | typecheck + production build                 |
| `npm test`      | vitest                                       |
| `npm run e2e`   | Playwright smoke test (needs both halves up) |
| `npm run lint`  | eslint                                       |

The first request for a scenario with no recorded bundle runs a live ELMFIRE
ensemble in-process (about a minute; it needs the static tier from
`backend/scripts/setup_fire_data.sh`). After that it is instant, and the result
is written to `backend/data/bundles/`. Those recordings are committed, so **the
UI works without the static tier at all**; delete one to force a fresh
simulation.

### Running the tests

The backend suite needs a virtualenv of its own — `setup_db.sh` only builds the
loader's, under `backend/scripts/.venv`:

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q                    # 181 tests, no database, no network

cd ../frontend && npm test -- --run    # 8 tests
```

## Repo map

```
backend/
├── app/
│   ├── routers/assets.py       # GET /assets — THE CONTRACT ENDPOINT
│   ├── routers/decision.py     # everything under /api
│   ├── decision/               # scenarios, spread adapter, reached set, bundles
│   ├── engine/                 # exposure, scoring, sensitivity  (ported)
│   ├── briefing/               # LLM + grounding validator        (ported)
│   ├── providers/osm_assets.py # named facilities from Overpass
│   └── data/                   # scenarios.json, asset_types.yaml
├── fire_spread/                # self-hosted ELMFIRE pipeline (modes, weather, arrival grid)
├── db/init/*.sql               # schema; applied on every setup_db.sh run
├── data/bundles/*.json         # recorded scenarios — the app runs off these
├── extract_buildings.py        # INSPIRE building GML  -> CSV
├── extract_forests.py          # INSPIRE forest GeoJSON -> CSV
├── scripts/setup_db.sh         # one command: containers + schema + data
└── tests/                      # 195 tests, no database and no network needed
frontend/src/
├── App.tsx                     # the command-centre layout
├── api/                        # client + react-query hooks
├── map/MapView.tsx             # MapLibre: contours, assets by tier
└── components/                 # ranked list, controls, charts, briefing
CONTEXT.md                      # the glossary
DECISIONS.md                    # what was decided and how to change it
docs/deepfire-api.md            # the Deepfire API (replaced by fire_spread/; kept for reference)
docs/elmfire-pipe-assessment.md # the ELMFIRE pipeline: inputs, tuning, base-vs-tuned evaluation
archive/docs/SPEC.md            # the original hackathon brief
```

## Conventions that hold across this repo

- **No PostGIS.** Coordinates are plain numeric columns; polygons are GeoJSON in
  a `jsonb` column. Bounding-box filtering happens in SQL, exact geometry work
  happens in Python with `shapely`. This is a deliberate, measured choice —
  [the reasoning is in backend/README.md](backend/README.md#why-no-postgis), and
  it is not an oversight to be fixed.
- **The database is the source of truth for shape.** Several code paths read the
  table's real columns from the Postgres catalog rather than hard-coding a
  schema, so adding a column does not require touching the API.
- **Loaders stream.** Source data is piped straight from the open-data portal
  into a parser and then `COPY`d; 12 GB of source GML never lands on disk.
- **Everything is re-runnable.** `setup_db.sh`, the schema files and both
  loaders can be run repeatedly without duplicating or corrupting data.
- **Secrets stay out of git.** `backend/.env` is the one file holding
  credentials, it is gitignored, and `backend/.env.example` lists what belongs
  in it. `docker-compose.yml` passes them into the container as environment
  variables rather than baking a `.env` into the image.
- **The root is the contract; `/api` is ours.** `GET /assets` and the routes
  beside it are written against by other people's code and do not move. The
  decision layer sits under `/api` and changes with the UI it serves.
- **Missing data is reported, never inferred.** A confidence figure invented
  from elapsed time, or a mirror's empty answer treated as "nothing here", is
  worse than an absent number: both survive review because they look like
  results. Where something cannot be known, the response says so.
