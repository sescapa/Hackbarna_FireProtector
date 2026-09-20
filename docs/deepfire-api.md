# Deepfire API notes

> Superseded in this backend: `/fire/arrival-grid` and the decision layer now run the self-hosted
> ELMFIRE pipeline in `backend/fire_spread/` (see `docs/elmfire-pipe-assessment.md`). Kept as
> reference for other users of the Deepfire API; the arrival-grid contract described at the end
> is what the new route still serves by default.

Working notes from exploring `docs.deepfire.co` + live testing against the real API.
Credentials go in `backend/.env` as `DEEPFIRE_CLIENT_ID` / `DEEPFIRE_CLIENT_SECRET`
(gitignored; see `backend/.env.example`).

Base URLs:
- OGC Features: `https://api.deepfire.co/ogc/features/v1/collections/<collection>/items`
- Token: `POST https://api.deepfire.co/v1/token` (client_id/client_secret -> access_token, bearer auth)
- Fire spread: `https://api.deepfire.co/v1/fire-spread/simulations`

Collections: `deepfire:hotspots`, `deepfire:clusters`, `deepfire:satellite-perimeters`, `deepfire:static-heat-sources`, `deepfire:data-sources`.
Coming soon (per docs nav, not live): `values-at-risk`, `official-incidents`, `ml-detections`.

## Hotspots (`deepfire:hotspots`) — GeoJSON Point

Per-detection satellite fire pixel. History since Jan 2025.

Fields: `id`, `cluster_id`, `observed_at` (ISO8601, indexed), `source`, `confidence` (LOW/MEDIUM/HIGH), `fire_radiative_power` (MW, nullable), `country` (ISO 3166-1 alpha-2, nullable), `active` (bool — false 24h after owning cluster goes quiet).

**Coordinates are raw lat/lon, no grid snapping.** Point density/spacing in a region depends entirely on which sensor(s) currently cover it — see measured nearest-neighbor spacing by `source` (from a live 2000-point / 1025-cluster sample):

| source | type | min spacing seen | typical (p10) |
|---|---|---|---|
| `MTG_I1`, `HIMAWARI_9` | geostationary | ~0m | 2–2.6 km |
| `METEOSAT_9`/`10` | geostationary | ~3km | 8–10 km |
| `VIIRS_NOAA20/21_NRT`, `VIIRS_SNPP_NRT` | polar, high-res | 0.4–5.8 km | 13–20 km |
| `MODIS_NRT` | polar | 23 km | 35 km |
| `SENTINEL_3A/B` | polar | 24–75 km | 56–79 km |
| `GOES_19`, `LANDSAT_NRT`, `METOP_B/C` | sparse in this sample | 42–420 km | — |

Geostationary sensors (MTG, Meteosat, Himawari) revisit every few minutes → flood a burning region with points. VIIRS has ~375m native pixels but fewer overpasses/day. MODIS/Sentinel-3 coarser and rarer.

## Clusters (`deepfire:clusters`) — GeoJSON Point

"Hotspots grouped in space and time into candidate fires." Fields: `id`, `first_observed`, `last_observed`, `active`. Geometry is a single representative point (not the extent).

**Do not treat `cluster_id` as "one physical fire."** Verified case: a cluster active for 16 days (Sept 3–19) whose member hotspots were scattered across ~500×700 km (southern Africa biomass-burning season) — clearly many distinct fires merged by the clustering logic. Algorithm/thresholds are undocumented. For per-fire granularity, re-cluster raw hotspots yourself (e.g. DBSCAN, tight radius ~5–10km + short time gap) rather than trusting this collection.

## Satellite perimeters (`deepfire:satellite-perimeters`) — GeoJSON MultiPolygon

Note: doc path is `/api/satellite-perimeters`, **not** `/api/perimeters`.

"Timestamped fire-perimeter polygons estimated from hotspot clusters." History since June 2026. New row per cluster only when geometry *materially changes* (not fixed interval); old snapshots retained.

Fields: `id`, `cluster_id`, `computed_at`, `observed_watermark` (latest hotspot feeding the polygon, nullable), `n_hotspots`, `area_m2` (nullable), `perimeter_m` (nullable), `algo_version` (e.g. `circle-union-v2`), `active`.

Explicitly disclaimed: **not official surveyed fire boundaries** — don't use for safety/legal decisions.

## Fire spread simulation (`/v1/fire-spread/simulations`)

POST to queue, GET `/{id}` to poll (recommended every ~10s), timeout to `FAILED` after 60 min.

Request: `clusterId` OR `latitude`+`longitude` (mutually exclusive-ish — `sources`/`lookbackHours` only valid with `clusterId`), `durationHours` (1–24, required), `model` (`elmfire` default | `forefire`), `ensembleMembers` (1–50, default 1), `lookbackHours` (1–168, default 24, clusterId only — controls which hotspots seed ignition).

**Geographic coverage is restricted to continental US, Europe, and Hawaii** — not documented on the endpoint page, discovered via a live 422/FAILED: an ignition point in Zambia failed with `"Ignition point (...) is outside the supported simulation areas (continental US, Europe, Hawaii)."` Perimeters/hotspots/clusters have no such restriction — only fire-spread does.

Response: one MultiPolygon feature per simulated hour (`result` FeatureCollection), each with `hour`/`elapsed_seconds`. `summary`: `burnedAreaM2`, `edgeReached`, `windSpeedAvgMs`, `windDirectionAvg` (deg). `ignition`: FeatureCollection of seed points.

**Spatial resolution is undocumented — measured empirically.** 6h sim near Santa Barbara, CA: output polygon edges have minimum segment length ~24m, bulk in 25–90m range → consistent with a **~30m underlying raster grid** (plausibly LANDFIRE fuel data, which ELMFIRE typically consumes at 30m in the US). Treat spread precision as ~30m cells, not finer. Not yet verified for Europe.

**Two distinct ignition modes — only one of them uses hotspot data. This part IS documented** (`/api/fire-spread`, `/quickstart`): `clusterId` → *"the fire is seeded from the cluster's hotspots"*; lat/lon → *"a single-point ignition instead of a cluster."* The response `sources` field means "source codes that were eligible to seed the run."

**Ignition points are capped at 100 — NOT documented, verified live (2026-09-19).** `clusterId` on a cluster with ~10k hotspots and `lookbackHours=168` returned `ignitionPointCount: 100` and exactly 100 `ignition` features; the API picks which 100. The cap is visible in the POST response before the sim runs. Docs page only shows `ignitionPointCount` as a response field (example value 52), no limit stated.

**No polygon / custom multi-point input.** Verified live: any extra body field is rejected with `Unknown field '...' Accepted fields: clusterId, latitude, longitude, durationHours, model, ensembleMembers, sources, lookbackHours` (tried `geometry`, `polygon`, `ignition`, `ignitionPoints`). Ignition is strictly one lat/lon point or a `clusterId`.

- **`latitude`/`longitude` (hypothetical/manual)**: exactly 1 synthetic ignition point, `properties: {}` (no hotspot data). Spread is driven purely by **cartographic (fuel/land-cover/terrain) + weather** at that point — nothing from the hotspots/clusters/perimeters collections feeds in. This is *why* `sources`/`lookbackHours` are rejected (422) for this mode. (The specific data providers — which weather model, which fuel/land-cover dataset — are NOT named anywhere in the docs, including `/api/data-sources`; this characterization is inferred from behavior, not documented.)
- **`clusterId` (real-fire, hotspot-seeded)**: pulls **multiple real ignition points from actual satellite hotspots** within the `lookbackHours` window, filtered by `sources`. Verified live against a real active cluster (Monzón, Huesca): got 4 ignition points, each with `hotspotId`, `source` (e.g. `VIIRS_SNPP_NRT`), `observedAt`, `fireRadiativePower` — real detection metadata, not synthetic. This matches the doc statement above.

**Auto simulations exist per docs** — the fire-spread page states the list endpoint returns *"everything your organization has run, from the API or the web app, plus the simulations Deepfire runs automatically on fires in your jurisdiction (auto: true)."* So `auto:true` itself is documented.

**NOT documented — found only by inspecting live auto-simulation payloads**: the `auto:true` background sims (Olius, la Jonquera, etc.) all have `clusterId: null` and only **1** ignition point with empty properties, same shape as a manual lat/lon call. Docs never say which ignition mode `auto` uses internally. So the automatic daily re-forecasts appear to NOT use the richer multi-hotspot `clusterId` ignition — they're just centered on the fire's location. For a more accurate ignition footprint on a real fire, call `clusterId` yourself; don't assume the auto feed already does this.

**You can simulate any point, not just active fires.** Verified with a `latitude`/`longitude` ignition on rural Kansas farmland (no real fire there) — it queued and completed normally. It's a genuine physics-based "what if a fire started here" model driven by local fuel/land-cover + weather, not something that requires an existing hotspot: the Kansas result had a tiny, non-growing burn area (19,800 m², identical polygon every hour) because cropland fuel doesn't support spread, vs. a real chaparral fire (Santa Barbara) growing to 5.15M m² over 6h. So the only hard gate is geography (US/Europe/Hawaii); ignition can be any coordinate. Use `clusterId` instead of lat/lon only when you want ignition seeded from a real detected fire's actual hotspot points.

Limits: 2 simulations in flight per API client.

### Querying/listing simulations (undocumented, discovered live)

`GET /v1/fire-spread/simulations?limit=N&since=<ISO date/timestamp>` — returns `{items: [...], nextCursor}`, newest-first (by `createdAt`).

- `since` is an inclusive lower bound on `createdAt`. Must be a plain ISO date or timestamp (e.g. `2026-09-19T14:15:52Z` or `2026-09-19`) — passing the `nextCursor` value back verbatim gives a 422 (`since must be an ISO-8601 instant or date`), so despite the field name it's not a drop-in resumable cursor in the form returned.
- `status=` and `fireId=` query params are **silently ignored** — tried both, results were unfiltered either way. No confirmed server-side filter for status or fire besides `since`+`limit`.
- The `auto:true` behavior itself **is documented** (see fire-spread section above), including `fireId`/`fireName`/`locationName` appearing on those entries (observed live: "Olius", "la Jonquera", "l'Escala" — real Catalonia fires). Your own on-demand sims come back as `"auto": false` with no `fireId`. There's no documented filter to fetch just the auto ones (see `status=`/`fireId=` note above — both no-ops).
- **Each simulation (auto or not) is still a one-shot, complete, static forecast** — it does not keep running/extending after `COMPLETED`. What "auto" gives you is *periodic re-triggering*: for a fire still active, Deepfire kicks off a brand-new job (fresh ignition points from latest hotspots) roughly **once every ~24–26 hours**, verified by grouping 100 list entries by `fireId` (e.g. one fire had runs at `09-17T02:43`, `09-18T04:43`, `09-19T05:43`). There's no server-side stitching of a fire's successive runs into a continuous timeline — if you want that, you have to poll the list for new `auto:true` entries per `fireId` yourself and concatenate.

## Query mechanics / limits (all OGC Features collections)

- Auth: bearer token from `/v1/token`, plus `filter-lang=cql2-text`, `filter=<CQL2 text>`, `f=application/geo+json`, `bbox=minLon,minLat,maxLon,maxLat`.
- Per-query timeout: **30s** — wide bbox + unbounded time-range filters can blow this (verified: got `HTTP 500 canceling statement due to statement timeout` combining a moderate bbox with `observed_at < ...`).
- `limit` clamps at 10,000/page. No `numberMatched` — page until you get a short page.
- Responses cached 60s per client (freshness lag).
- **Rows are NOT sorted by default** (verified — `observed_at` values in a returned page were out of order). Don't assume newest/oldest-first; add explicit ordering/filtering if it matters.
- Practical gotcha: a `bbox` + `active=true` query with no time bound can exhaust the 10,000-row cap within a few days of data in a hot region (verified: a Zambia bbox returned 10k rows spanning only 3 days, even though the active cluster there started 16 days earlier). To reach further back, page or add a narrow `observed_at` window — but keep windows narrow enough to avoid the 30s timeout.
- Max 5 API keys per account. Concurrency is a shared fixed cap across all users — exceeding it returns 503 + `Retry-After`.

## Using the hourly polygons as an arrival-time grid

Hourly perimeters are nested (hour *h* ⊇ hour *h−1*), so a per-cell "first hour reached" raster can be derived by testing cell centres against the polygons newest→oldest. Implemented in `backend/fire_spread/grid.py`, served as `GET /fire/arrival-grid`
(see `backend/fire_spread/README.md`). A `NO_SPREAD` result has an empty `result.features` and `burnedAreaM2` ≈ 900 (one ~30m cell) — confirms the 30m model grid.

## Open questions / not yet verified

- Exact clustering algorithm/thresholds for `deepfire:clusters` (undocumented).
- Fire-spread spatial resolution in Europe/Hawaii (only measured for US/California).
- How the API selects which 100 hotspots seed a `clusterId` run when the cluster has more.
- Pagination cursor semantics beyond `limit` (docs mention `since`+`limit` "keyset-paginated" for fire-spread list, unconfirmed for OGC collections).
