"""FireProtector backend: the asset-register API over Postgres."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from psycopg_pool import AsyncConnectionPool

from fire_spread import router as fire_spread_router
from fire_spread import startup as fire_spread_startup

from .config import Settings, get_settings
from .db import create_pool, get_pool
from .errors import install_handlers
from .routers import add_building, assets, building_specs, decision
from .schemas import HealthResponse

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.pool = create_pool(settings)
    # Does not block startup: connections are established in the background, so
    # the server comes up even while a suspended Neon compute is waking.
    await app.state.pool.open()
    # The fire-spread service starts in the same process: it logs its mode/readiness and
    # opens the static tier once; simulations then run per request under /fire.
    fire_spread_startup()
    try:
        yield
    finally:
        await app.state.pool.close()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="FireProtector API",
        description=(
            "Asset register for the wildfire values-at-risk tool. GET /assets "
            "implements the asset-register contract; the other two routes are "
            "internal. GET /fire/arrival-grid runs a self-hosted ELMFIRE fire-spread "
            "simulation and returns the hour the fire reaches each grid cell.\n\n"
            "Everything under /api is the decision layer: it takes an ignition "
            "scenario, works out what the fire reaches and ranks it. Those "
            "routes serve one front end and may change with it; the routes at "
            "the root implement a contract shared with other people's code and "
            "do not."
        ),
        version="1.0.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(assets.router)
    app.include_router(building_specs.router)
    app.include_router(add_building.router)
    # Mounted from the sibling `fire_spread` package rather than app/routers:
    # it drives ELMFIRE and Open-Meteo, not Postgres, and stays independently
    # mountable (it reads its own settings from the environment).
    app.include_router(fire_spread_router, prefix="/fire")
    # The decision layer. Prefixed, because the root belongs to the contract.
    app.include_router(decision.router)
    install_handlers(app)

    @app.get("/health", response_model=HealthResponse, tags=["meta"])
    async def health(
        pool: AsyncConnectionPool = Depends(get_pool),
        settings: Settings = Depends(get_settings),
    ) -> HealthResponse:
        try:
            async with pool.connection() as conn, conn.cursor() as cur:
                await cur.execute("SELECT 1")
                await cur.fetchone()
        except Exception as exc:  # surfaced as a status, not a 500
            return HealthResponse(status="degraded", database="unreachable", detail=str(exc))
        return HealthResponse(status="ok", database="connected")

    return app


app = create_app()
