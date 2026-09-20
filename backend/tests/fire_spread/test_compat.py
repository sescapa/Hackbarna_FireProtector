"""The Deepfire-shaped client the decision layer (app/decision) keeps calling.

``run_simulation_detailed`` must hand back nested hourly perimeters (hour h contains hour
h-1) with the certain core at 1.0 and lower-probability fringes only at the final hour;
``run_simulation`` keeps the core alone. Pure geometry on a synthetic grid - no ELMFIRE.
"""

import asyncio
import importlib

import numpy as np
import pytest
from fastapi import HTTPException
from shapely.geometry import Point

from fire_spread import compat
from fire_spread.models import ArrivalGrid, WeatherSummary
from fire_spread.settings import Settings

r = importlib.import_module("fire_spread.router")

LAT, LON = 41.80, 1.25
DLAT, DLON = 0.0009, 0.0012
N = 21  # cells per side; ignition at the centre


def _grid(hours: np.ndarray, prob: np.ndarray, duration_h: int = 24) -> ArrivalGrid:
    def lists(a, null):
        o = a.astype(object)
        o[null] = None
        return o.tolist()

    return ArrivalGrid(
        originLat=LAT - (N / 2) * DLAT, originLon=LON - (N / 2) * DLON, cellDegLat=DLAT, cellDegLon=DLON,
        cellSizeM=100.0, durationMinutes=duration_h * 60, ensembleMembers=4,
        arrivalHours=lists(hours, hours < 0), arrivalMinutes=lists(hours * 60.0, hours < 0),
        arrivalMinutesP10=lists(hours * 50.0, hours < 0), arrivalMinutesP90=lists(hours * 70.0, hours < 0),
        burnProbability=lists(prob, prob < 0),
        weather=WeatherSummary(source="fixture", windSpeedAvgMs=5.0, windDirectionAvg=270.0),
    )


def _radial(duration_h: int = 24):
    """Fire growing one cell per hour from the centre; the ensemble agrees inside 6 cells,
    the outer rings burned in fewer members."""
    rr, cc = np.mgrid[0:N, 0:N]
    dist = np.maximum(abs(rr - N // 2), abs(cc - N // 2))
    hours = np.where(dist <= 9, dist, -1).astype(np.int32)
    prob = np.where(dist <= 6, 1.0, np.where(dist <= 8, 0.3, np.where(dist <= 9, 0.15, -1.0)))
    return _grid(hours, prob, duration_h)


def test_hourly_perimeters_nest_across_hours_and_levels():
    got = compat.hourly_perimeters(_radial())
    assert got == sorted(got, key=lambda t: (t[0], -t[1]))
    assert {h for h, _, _ in got} == set(range(1, 25))  # every hour, like Deepfire
    assert {p for _, p, _ in got} <= set(compat.LEVELS)
    by_hour = {h: {p: g for hh, p, g in got if hh == h} for h in range(1, 25)}
    for h in range(1, 25):
        levels = by_hour[h]
        assert 1.0 in levels  # the ignition cell is certain from the start
        ordered = sorted(levels, reverse=True)
        for hi, lo in zip(ordered, ordered[1:]):  # a lower level contains a higher one, strictly
            assert levels[lo].covers(levels[hi]) and levels[lo].area > levels[hi].area
        if h > 1:  # hour h contains hour h-1 at the same level
            for p, g in by_hour[h - 1].items():
                if p in levels:
                    assert levels[p].covers(g)
    # the certain core grows one cell per hour up to 6 cells (P90 = 70 min/cell) then stops;
    # the 0.3 ring (dist 7-8) and the 0.15 ring (dist 9) show up as lower levels at the end
    assert by_hour[24][1.0].equals(by_hour[10][1.0])
    assert by_hour[24][0.3].area > by_hour[24][1.0].area and 0.2 not in by_hour[24] and 0.1 in by_hour[24]


def test_burned_within_interpolates_the_member_distribution():
    g = _radial()
    burned_at = lambda t: compat.burned_within(g, t)[N // 2, N // 2 + 1]  # noqa: E731 - cell at distance 1
    assert burned_at(49.0) == 0.0 and burned_at(50.0) == pytest.approx(0.1)
    assert burned_at(55.0) == pytest.approx(0.3) and burned_at(60.0) == pytest.approx(0.5)
    assert burned_at(65.0) == pytest.approx(0.7) and burned_at(70.0) == pytest.approx(1.0)
    far = compat.burned_within(g, 1e6)  # eventually: the burn probability itself
    assert far[N // 2, N // 2 + 7] == pytest.approx(0.3) and far[N // 2, N // 2 + 10] == 0.0


def test_perimeters_are_wgs84_over_the_grid():
    got = compat.hourly_perimeters(_radial())
    certain = {h: g for h, p, g in got if p == 1.0}
    assert certain[1].contains(Point(LON, LAT))
    assert certain[1].area == pytest.approx(DLAT * DLON, rel=1e-6)  # hour 1: the ignition cell alone
    minx, miny, maxx, maxy = certain[7].bounds  # 13x13 cells centred on the ignition cell
    assert (minx, miny, maxx, maxy) == pytest.approx((LON - 6.5 * DLON, LAT - 6.5 * DLAT, LON + 6.5 * DLON, LAT + 6.5 * DLAT))


def test_single_member_and_empty_grids():
    assert compat.hourly_perimeters(_grid(np.full((0, 0), -1), np.full((0, 0), -1.0))) == []
    # one member: every burned cell is certain, so only 1.0 perimeters come back (as with Deepfire)
    rr, cc = np.mgrid[0:N, 0:N]
    dist = np.maximum(abs(rr - N // 2), abs(cc - N // 2))
    hours = np.where(dist <= 3, dist, -1).astype(np.int32)
    prob = np.where(dist <= 3, 1.0, -1.0)
    g = _grid(hours, prob, 4)
    g = g.model_copy(update={"arrivalMinutesP10": g.arrivalMinutes, "arrivalMinutesP90": g.arrivalMinutes})
    got = compat.hourly_perimeters(g)
    assert [(h, p) for h, p, _ in got] == [(1, 1.0), (2, 1.0), (3, 1.0), (4, 1.0)]
    assert got[-1][2].equals(got[-2][2]) and got[2][2].area > got[1][2].area


def test_run_simulation_keeps_only_the_certain_core(monkeypatch):
    client = compat.ElmfireSpread()

    async def detailed(lat, lon):
        return compat.hourly_perimeters(_radial())

    monkeypatch.setattr(client, "run_simulation_detailed", detailed)
    got = asyncio.run(client.run_simulation(LAT, LON))
    assert [h for h, _ in got] == list(range(1, 25))  # one certain perimeter per hour, nothing below 1.0


def test_client_runs_the_pipeline_with_deepfire_defaults(monkeypatch):
    seen = []

    async def run_grid(req):
        seen.append(req)
        return _radial()

    monkeypatch.setattr(r, "run_grid", run_grid)
    got = asyncio.run(compat.ElmfireSpread().run_simulation_detailed(LAT, LON))
    req = seen[0]
    assert (req.ignition.lat, req.ignition.lon) == (LAT, LON)
    assert (req.duration_hours, req.ensemble_members, req.output_cell_m, req.mode) == (24, 16, None, None)
    assert got and got[-1][0] == 24


@pytest.mark.parametrize("elmfire,data,expect", [
    (False, True, ["ELMFIRE binary"]),
    (True, False, ["static data"]),
    (True, True, []),
])
def test_missing_credentials_reports_what_the_pipeline_lacks(monkeypatch, tmp_path, elmfire, data, expect):
    if data:
        (tmp_path / "dem.tif").touch()
    monkeypatch.setattr(r, "elmfire_available", lambda: elmfire)
    monkeypatch.setattr(r, "get_settings", lambda: Settings(data_dir=tmp_path))
    r.get_deepfire.cache_clear()
    missing = r.missing_credentials()
    assert [m.split(" (")[0].replace("fire-spread ", "") for m in missing] == expect
    if expect:
        with pytest.raises(HTTPException) as e:
            r.get_deepfire()
        assert e.value.status_code == 503 and expect[0] in e.value.detail
    else:
        assert isinstance(r.get_deepfire(), compat.ElmfireSpread)
    r.get_deepfire.cache_clear()
