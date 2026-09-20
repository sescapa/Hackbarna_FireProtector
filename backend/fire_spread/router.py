"""HTTP surface: the synchronous prediction endpoint (GET for a point, POST for a fire
state with an optional perimeter) plus health/data-info.

    from fire_spread.router import router, startup
    app.include_router(router, prefix="/fire")   # and call startup() in the lifespan

``GET /arrival-grid?lat&lon`` keeps the contract the Deepfire-backed version served -
``{originLat, originLon, cellDegLat, cellDegLon, arrivalHours}`` at 100 m - so its consumers
see no change; ``detail=true`` (and the POST route) return the full ensemble output.
``get_deepfire()`` / ``missing_credentials()`` are the names the decision layer
(``app/decision``) imports; they are kept and backed by the same pipeline (``compat.py``).

Normal-running mode: the API serves predictions on live Open-Meteo weather with the
pipeline mode from ``PIPELINE_MODE`` (``base`` or ``tuned``, see ``modes.py``); a request
may pick the other mode with ``mode=``. The evaluation loop
(``scripts/fire_spread/evaluate.py``) replays both modes on historical weather.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from functools import lru_cache

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import ORJSONResponse

from .compat import ElmfireSpread
from .elmfire_runner import elmfire_available
from .models import (
    LEGACY_KEYS,
    ArrivalGrid,
    FireStateRequest,
    Ignition,
    LegacyArrivalGrid,
    PipelineError,
    PipelineMode,
    SimulationRequest,
)
from .pipeline import Pipeline
from .settings import get_settings

log = logging.getLogger("fire_spread")
router = APIRouter(tags=["fire-spread"])


@lru_cache(maxsize=4)
def get_pipeline(mode: str | None = None) -> Pipeline:
    """One pipeline per mode (lazily built; ``mode=None`` = the configured one)."""
    return Pipeline(mode=mode or get_settings().pipeline_mode)


@lru_cache(maxsize=1)
def get_semaphore() -> asyncio.Semaphore:
    return asyncio.Semaphore(get_settings().max_concurrent_runs)


def missing_credentials() -> list[str]:
    """What this deployment lacks to simulate, or ``[]``.

    Anything asking "can this deployment simulate?" goes through here (``/api/health``
    reports it as the fire-spread provider's error). Deepfire needed two credentials; the
    self-hosted pipeline needs the ELMFIRE binary (baked into the image) and the static
    tier (scripts/setup_fire_data.sh), so those are what can be missing now.
    """
    st = status()
    if st == "no elmfire":
        return ["ELMFIRE binary (ELMFIRE_INSTALL_DIR)"]
    if st == "no data":
        return ["fire-spread static data (scripts/setup_fire_data.sh)"]
    return []


@lru_cache(maxsize=1)
def get_deepfire() -> ElmfireSpread:
    """The simulation client the decision layer calls; ELMFIRE-backed, same interface."""
    missing = missing_credentials()
    if missing:
        # Not cached: lru_cache only stores return values, so preparing the data and
        # restarting the worker is enough to recover.
        raise HTTPException(status_code=503, detail=f"fire spread unavailable: missing {', '.join(missing)}")
    return ElmfireSpread()


def data_present() -> bool:
    return (get_settings().data_dir / "dem.tif").exists()


def status() -> str:
    """``ready`` | ``no elmfire`` | ``no data`` - what ``/health`` reports."""
    if not elmfire_available():
        return "no elmfire"
    if not data_present():
        return "no data"
    return "ready"


def startup() -> str:
    """Called from the app lifespan: log the configured mode and readiness and warm the
    pipeline for it (opens the static tier once) so the first request pays nothing extra.
    Never raises - a missing tier or binary is a health status, not a startup failure."""
    s = get_settings()
    st = status()
    log.info("fire_spread: mode=%s status=%s data_dir=%s runs_dir=%s", s.pipeline_mode, st, s.data_dir, s.runs_dir)
    if st == "ready":
        try:
            get_pipeline(s.pipeline_mode).landscape  # noqa: B018 - property opens dem.tif
        except Exception as e:  # pragma: no cover - surfaced at request time as a 500 anyway
            log.warning("fire_spread: could not open the static tier: %s", e)
    return st


@router.get("/health")
async def health() -> dict:
    s = get_settings()
    return {
        "status": "ok",
        "elmfire": elmfire_available(),
        "dataDir": str(s.data_dir),
        "dataPresent": data_present(),
        "mode": s.pipeline_mode,
    }


@router.get("/data-info")
async def data_info() -> dict:
    """Manifest of the static tier (grid, sources, dates, licenses)."""
    s = get_settings()
    p = s.data_dir / "manifest.json"
    if not p.exists():
        raise HTTPException(status_code=404, detail="static data not prepared (run scripts/setup_fire_data.sh)")
    import json

    return json.loads(p.read_text())


async def run_grid(req: SimulationRequest) -> ArrivalGrid:
    """Run one request through the concurrency gate; HTTP errors for the not-ready cases.
    Shared by the routes and by the decision layer's in-process client (``compat.py``)."""
    s = get_settings()
    if not data_present():
        raise HTTPException(status_code=503, detail="static data not prepared (run scripts/setup_fire_data.sh)")
    sem = get_semaphore()
    try:
        await asyncio.wait_for(sem.acquire(), timeout=s.acquire_timeout_s)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=503, detail="simulation slots busy; retry later")
    try:
        return await get_pipeline(req.mode or s.pipeline_mode).run(req)
    except PipelineError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    finally:
        sem.release()


async def _simulate(req: SimulationRequest, *, detail: bool = True) -> ORJSONResponse:
    grid = await run_grid(req)
    # Bypass FastAPI's re-validation + jsonable_encoder walk: the grids hold millions of
    # scalars and orjson serialises them ~20x faster.
    body = grid.model_dump(exclude_none=True)
    if not detail:
        body = {k: body[k] for k in LEGACY_KEYS}
    return ORJSONResponse(body)


@router.get(
    "/arrival-grid",
    response_model=LegacyArrivalGrid,
    response_model_exclude_none=True,
    response_class=ORJSONResponse,
    responses={200: {"description": "The arrival grid; the full ArrivalGrid model when detail=true."}},
)
async def arrival_grid(
    lat: float = Query(ge=-90, le=90, description="Ignition latitude (WGS84)"),
    lon: float = Query(ge=-180, le=180, description="Ignition longitude (WGS84)"),
    durationHours: int = Query(24, ge=1, le=48),
    ensembleMembers: int = Query(16, ge=1, le=64),
    startTime: datetime | None = Query(None, description="ISO 8601 ignition time (default: now)"),
    seed: int | None = Query(None, ge=1),
    spotting: bool | None = Query(None, description="Ember transport (slower); default from settings"),
    mode: PipelineMode | None = Query(None, description="Pipeline mode: base | tuned (default: PIPELINE_MODE)"),
    cellSizeM: float = Query(100.0, ge=30, le=1000, description="Output grid cell size (m); the simulation runs at native resolution"),
    detail: bool = Query(False, description="Return the full ensemble output (minutes, percentiles, burn probability, weather, physics)"),
    debug: bool = Query(False),
) -> ORJSONResponse:
    """Simulate a point ignition and return the hour each grid cell is first reached.

    Without ``detail`` the response is exactly ``{originLat, originLon, cellDegLat, cellDegLon,
    arrivalHours}`` (rows S->N, cols W->E, 0 = ignition, null = not reached), as before."""
    return await _simulate(SimulationRequest(
        ignition=Ignition(lat=lat, lon=lon),
        duration_hours=durationHours, ensemble_members=ensembleMembers, start_time=startTime,
        seed=seed, spotting=spotting, mode=mode, output_cell_m=cellSizeM, debug=debug,
    ), detail=detail or debug)  # debug output only exists in the full model


@router.post("/arrival-grid", response_model=ArrivalGrid, response_model_exclude_none=True, response_class=ORJSONResponse)
async def arrival_grid_from_state(body: FireStateRequest) -> ORJSONResponse:
    """Run an ELMFIRE ensemble from an initial fire state - a point and/or an active
    perimeter (GeoJSON polygon, lit along its boundary at t 0) - and return the arrival grid.
    With only a perimeter the reference point is its centroid."""
    if body.perimeter is not None:
        if body.ignition is not None:
            lat, lon = body.ignition.lat, body.ignition.lon
        else:
            from shapely.geometry import shape

            try:
                c = shape(body.perimeter).centroid
                lat, lon = float(c.y), float(c.x)
            except Exception as e:  # malformed coordinates
                raise HTTPException(status_code=422, detail=f"invalid perimeter geometry: {e}")
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise HTTPException(status_code=422, detail="perimeter coordinates must be WGS84 lon/lat")
        ign = Ignition(lat=lat, lon=lon, perimeter=body.perimeter)
    else:
        ign = Ignition(lat=body.ignition.lat, lon=body.ignition.lon)
    return await _simulate(SimulationRequest(
        ignition=ign,
        duration_hours=body.durationHours, ensemble_members=body.ensembleMembers, start_time=body.startTime,
        seed=body.seed, spotting=body.spotting, mode=body.mode, output_cell_m=body.cellSizeM, debug=body.debug,
    ))
