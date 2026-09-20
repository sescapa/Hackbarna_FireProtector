import asyncio
import importlib
import json
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi import FastAPI
from jsonschema import Draft202012Validator

# The package re-exports the APIRouter under the same name, so import the module explicitly.
r = importlib.import_module("fire_spread.router")
from fire_spread.models import (
    ArrivalGrid, ElmfireTimeout, OutsideCoverage, WeatherProviderError, WeatherSummary,
)
from fire_spread.settings import Settings

OPENAPI = Path(__file__).resolve().parents[2] / "fire_spread" / "openapi.yaml"


def _grid(**kw) -> ArrivalGrid:
    base = dict(
        originLat=41.58, originLon=1.82, cellDegLat=0.00045, cellDegLon=0.0006, cellSizeM=50.0,
        durationMinutes=1440, ensembleMembers=2,
        arrivalHours=[[0, 1], [None, 2]], arrivalMinutes=[[0.0, 30.5], [None, 90.0]],
        arrivalMinutesP10=[[0.0, 25.0], [None, 80.0]], arrivalMinutesP90=[[0.0, 40.0], [None, 100.0]],
        burnProbability=[[1.0, 1.0], [0.0, 0.5]],
        weather=WeatherSummary(source="fixture", windSpeedAvgMs=5.0, windDirectionAvg=270.0),
    )
    base.update(kw)
    return ArrivalGrid(**base)


class FakePipeline:
    def __init__(self, outcome, delay=0.0):
        self.outcome, self.delay, self.calls, self.modes = outcome, delay, [], []

    async def run(self, req, **kw):
        self.calls.append(req)
        if self.delay:
            await asyncio.sleep(self.delay)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@pytest.fixture
def app(monkeypatch, tmp_path):
    def make(outcome, delay=0.0, **settings):
        fake = FakePipeline(outcome, delay)
        # The route refuses to run without the static tier; a stub dem.tif marks it present.
        settings.setdefault("data_dir", tmp_path)
        (settings["data_dir"] / "dem.tif").touch()
        s = Settings(acquire_timeout_s=0.05, **settings)

        def pipeline_for(mode=None):
            fake.modes.append(mode)
            return fake

        monkeypatch.setattr(r, "get_pipeline", pipeline_for)
        monkeypatch.setattr(r, "get_settings", lambda: s)
        r.get_semaphore.cache_clear()
        app = FastAPI()
        app.include_router(r.router, prefix="/fire")
        return app, fake

    yield make
    r.get_semaphore.cache_clear()


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


LEGACY_KEYS = {"originLat", "originLon", "cellDegLat", "cellDegLon", "arrivalHours"}


async def test_default_get_is_the_original_contract(app):
    """``GET ?lat&lon`` answers exactly what the Deepfire-backed route did: five keys, 100 m cells."""
    a, fake = app(_grid())
    async with _client(a) as c:
        res = await c.get("/fire/arrival-grid", params={"lat": 41.59, "lon": 1.83, "ensembleMembers": 2})
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body) == LEGACY_KEYS
    assert body["arrivalHours"] == [[0, 1], [None, 2]] and body["originLat"] == 41.58
    req = fake.calls[0]
    assert (req.ignition.lat, req.ignition.lon, req.ensemble_members, req.duration_hours) == (41.59, 1.83, 2, 24)
    assert req.output_cell_m == 100.0
    assert req.mode is None and fake.modes == ["tuned"]  # the configured default mode

    spec = yaml.safe_load(OPENAPI.read_text(encoding="utf-8"))
    schema = {**spec["components"]["schemas"]["LegacyArrivalGrid"], "components": spec["components"]}
    Draft202012Validator(schema).validate(body)


async def test_detail_returns_the_full_model(app):
    a, fake = app(_grid())
    async with _client(a) as c:
        res = await c.get("/fire/arrival-grid", params={"lat": 41.59, "lon": 1.83, "detail": "true", "cellSizeM": 50})
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["arrivalHours"][1][0] is None and body["arrivalMinutes"][0][1] == 30.5
        assert "burnProbability" in body and "weather" in body and "debug" not in body
        assert fake.calls[-1].output_cell_m == 50.0
        # debug implies the full model (that is where the debug block lives)
        res = await c.get("/fire/arrival-grid", params={"lat": 41.59, "lon": 1.83, "debug": "true"})
        assert "burnProbability" in res.json() and fake.calls[-1].debug is True
        assert (await c.get("/fire/arrival-grid", params={"lat": 41.59, "lon": 1.83, "cellSizeM": 5})).status_code == 422

    spec = yaml.safe_load(OPENAPI.read_text(encoding="utf-8"))
    schema = {**spec["components"]["schemas"]["ArrivalGrid"], "components": spec["components"]}
    Draft202012Validator(schema).validate(body)


async def test_query_validation(app):
    a, _ = app(_grid())
    async with _client(a) as c:
        assert (await c.get("/fire/arrival-grid", params={"lat": 91, "lon": 0})).status_code == 422
        assert (await c.get("/fire/arrival-grid", params={"lat": 41, "lon": 1, "ensembleMembers": 65})).status_code == 422
        assert (await c.get("/fire/arrival-grid", params={"lat": 41, "lon": 1, "durationHours": 0})).status_code == 422
        assert (await c.get("/fire/arrival-grid", params={"lat": 41, "lon": 1, "mode": "fast"})).status_code == 422


async def test_mode_override(app):
    a, fake = app(_grid(), pipeline_mode="base")
    async with _client(a) as c:
        assert (await c.get("/fire/arrival-grid", params={"lat": 41.59, "lon": 1.83})).status_code == 200
        assert (await c.get("/fire/arrival-grid", params={"lat": 41.59, "lon": 1.83, "mode": "tuned"})).status_code == 200
        assert (await c.get("/fire/health")).json()["mode"] == "base"
    assert fake.modes == ["base", "tuned"]
    assert [req.mode for req in fake.calls] == [None, "tuned"]


SQUARE = {"type": "Polygon", "coordinates": [[[1.82, 41.58], [1.84, 41.58], [1.84, 41.60], [1.82, 41.60], [1.82, 41.58]]]}


async def test_post_fire_state_perimeter(app):
    a, fake = app(_grid())
    async with _client(a) as c:
        res = await c.post("/fire/arrival-grid", json={"perimeter": SQUARE, "durationHours": 6, "ensembleMembers": 2, "mode": "base"})
        assert res.status_code == 200, res.text
        assert res.json()["arrivalMinutes"][0][1] == 30.5  # POST always answers the full model
        assert fake.calls[-1].output_cell_m == 100.0
        # explicit reference point wins over the centroid
        res = await c.post("/fire/arrival-grid", json={"ignition": {"lat": 41.59, "lon": 1.83}, "perimeter": SQUARE})
        assert res.status_code == 200, res.text
        # a bare point works through POST too
        res = await c.post("/fire/arrival-grid", json={"ignition": {"lat": 41.59, "lon": 1.83}, "seed": 3})
        assert res.status_code == 200, res.text
    centroid, explicit, point = fake.calls
    assert centroid.ignition.perimeter == SQUARE and centroid.duration_hours == 6 and centroid.mode == "base"
    assert (round(centroid.ignition.lat, 3), round(centroid.ignition.lon, 3)) == (41.59, 1.83)
    assert (explicit.ignition.lat, explicit.ignition.lon) == (41.59, 1.83) and explicit.ignition.is_perimeter
    assert point.ignition.perimeter is None and point.seed == 3 and point.mode is None


async def test_post_fire_state_validation(app):
    a, fake = app(_grid())
    async with _client(a) as c:
        assert (await c.post("/fire/arrival-grid", json={"durationHours": 6})).status_code == 422
        assert (await c.post("/fire/arrival-grid", json={"perimeter": {"type": "Point", "coordinates": [1, 41]}})).status_code == 422
        assert (await c.post("/fire/arrival-grid", json={"perimeter": {"type": "Polygon", "coordinates": "nope"}})).status_code == 422
        assert (await c.post("/fire/arrival-grid", json={"ignition": {"lat": 95, "lon": 1}})).status_code == 422
    assert fake.calls == []


@pytest.mark.parametrize(
    "exc,status",
    [
        (OutsideCoverage("ignition on a non-burnable cell"), 422),
        (WeatherProviderError("Open-Meteo returned 503"), 502),
        (ElmfireTimeout("ELMFIRE exceeded 900 s"), 504),
    ],
)
async def test_error_mapping(app, exc, status):
    a, _ = app(exc)
    async with _client(a) as c:
        res = await c.get("/fire/arrival-grid", params={"lat": 41.59, "lon": 1.83})
    assert res.status_code == status
    assert str(exc) in res.json()["detail"]


async def test_busy_returns_503(app):
    a, _ = app(_grid(), delay=0.5, max_concurrent_runs=1)
    async with _client(a) as c:
        t1 = asyncio.create_task(c.get("/fire/arrival-grid", params={"lat": 41.59, "lon": 1.83}))
        await asyncio.sleep(0.1)
        r2 = await c.get("/fire/arrival-grid", params={"lat": 41.59, "lon": 1.83})
        r1 = await t1
    assert r1.status_code == 200
    assert r2.status_code == 503


async def test_health_and_data_info(app, tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({"grid": {"epsg": 25831}}))
    a, _ = app(_grid(), data_dir=tmp_path)
    async with _client(a) as c:
        h = (await c.get("/fire/health")).json()
        assert h["status"] == "ok" and h["dataPresent"] is True
        assert (await c.get("/fire/data-info")).json() == {"grid": {"epsg": 25831}}


async def test_no_static_data_is_503(app, tmp_path):
    a, fake = app(_grid(), data_dir=tmp_path)
    (tmp_path / "dem.tif").unlink()
    async with _client(a) as c:
        res = await c.get("/fire/arrival-grid", params={"lat": 41.59, "lon": 1.83})
        assert res.status_code == 503 and "setup_fire_data" in res.json()["detail"]
        assert (await c.get("/fire/health")).json()["dataPresent"] is False
        assert (await c.get("/fire/data-info")).status_code == 404
    assert fake.calls == []
