"""Real ELMFIRE runs on a synthetic landscape (no Catalonia data). Run inside the container:

    pytest -m elmfire
"""

from datetime import datetime, timezone

import numpy as np
import pytest

from fire_spread.elmfire_runner import elmfire_available
from fire_spread.landscape import SyntheticLandscape
from fire_spread.models import Ignition, SimulationRequest
from fire_spread.pipeline import Pipeline
from fire_spread.settings import Settings
from fire_spread.weather import ConstantProvider

pytestmark = [pytest.mark.elmfire, pytest.mark.skipif(not elmfire_available(), reason="elmfire binary not on PATH")]

LAT, LON = 41.59, 1.83
MPH20 = 20.0 / 2.23693629


def _pipeline(tmp_path, members_nproc=2, member_wd_step_deg=None, barrier=None, mode=None, **settings):
    s = Settings(runs_dir=tmp_path / "runs", keep_runs="all", domain_size_m=5000.0, elmfire_nproc=members_nproc, **settings)
    return Pipeline(
        settings=s,
        weather_provider=ConstantProvider(ws_ms=MPH20, wd_deg=270.0, temp_c=30.0, rh_pct=20.0, sigma_ws_ms=1.5,
                                          sigma_wd_deg=10.0, member_wd_step_deg=member_wd_step_deg),
        landscape=SyntheticLandscape(fbfm=102, cellsize=50.0, barrier=barrier),
        mode=mode,
    )


def _arr(rows):
    return np.array([[np.nan if v is None else v for v in row] for row in rows])


def _ign_rc(grid):
    return int((LAT - grid.originLat) / grid.cellDegLat), int((LON - grid.originLon) / grid.cellDegLon)


async def test_deterministic_westerly(tmp_path):
    grid = await _pipeline(tmp_path).run(
        SimulationRequest(Ignition(LAT, LON), duration_hours=2, ensemble_members=1, seed=7, debug=True)
    )
    mins = np.array([[np.nan if v is None else v for v in row] for row in grid.arrivalMinutes])
    prob = np.array([[np.nan if v is None else v for v in row] for row in grid.burnProbability])
    r, c = _ign_rc(grid)
    assert mins[r, c] == 0.0
    burned = ~np.isnan(mins)
    assert burned.sum() > 50, "fire did not spread"
    assert set(np.unique(prob[burned])) == {1.0}, "1-member ensemble must have burnProbability 1 where burned"

    # elongates east (wind from 270 deg): extent east of ignition >> extent west
    cols = np.nonzero(burned[r])[0]
    east, west = cols.max() - c, c - cols.min()
    assert east > 2 * max(west, 1), (east, west)

    # arrival time increases monotonically along the downwind axis
    along = mins[r, c : cols.max() + 1]
    along = along[~np.isnan(along)]
    assert np.all(np.diff(along) >= 0)
    assert grid.arrivalHours[r][c] == 0 and max(v for v in grid.arrivalHours[r] if v is not None) <= 2

    assert grid.debug is not None and grid.debug.timings["elmfire_s"] > 0
    assert (tmp_path / "runs" / grid.debug.runId / "elmfire.data").exists()
    assert grid.ensembleMembers == 1


async def test_ensemble_statistics(tmp_path):
    grid = await _pipeline(tmp_path).run(
        SimulationRequest(Ignition(LAT, LON), duration_hours=2, ensemble_members=4, seed=11)
    )
    assert grid.ensembleMembers == 4
    prob = np.array([[np.nan if v is None else v for v in row] for row in grid.burnProbability])
    med = np.array([[np.nan if v is None else v for v in row] for row in grid.arrivalMinutes])
    p10 = np.array([[np.nan if v is None else v for v in row] for row in grid.arrivalMinutesP10])
    p90 = np.array([[np.nan if v is None else v for v in row] for row in grid.arrivalMinutesP90])
    r, c = _ign_rc(grid)
    assert prob[r, c] == pytest.approx(1.0, abs=0.01)
    # every member must actually spread (guards against perturbation-unit mistakes)
    assert prob[r, c + 3] == pytest.approx(1.0, abs=0.01)
    ok = ~np.isnan(med)
    assert np.all(p10[ok] <= med[ok] + 1e-6) and np.all(med[ok] <= p90[ok] + 1e-6)
    # perturbed members disagree at the fire edge -> some fractional probabilities
    frac = (prob > 0.05) & (prob < 0.95)
    assert frac.sum() > 0, "expected uncertainty at the fire perimeter"


async def test_ensemble_weather_members(tmp_path):
    """4 cases on 4 stacked weather blocks (wind fanning 225..315 deg): each case must
    burn from the ignition at t=0 with the block offset removed, and the spread axes differ."""
    start = datetime(2026, 8, 1, 13, 20, tzinfo=timezone.utc)  # tstart = 1200 s inside band 1
    grid = await _pipeline(tmp_path, member_wd_step_deg=30.0).run(
        SimulationRequest(Ignition(LAT, LON), duration_hours=2, ensemble_members=4, seed=5, start_time=start, debug=True)
    )
    assert grid.ensembleMembers == 4 and grid.weather.weatherMembers == 4
    assert grid.physics["ignitionOffsetS"] == 1200.0
    run_dir = tmp_path / "runs" / grid.debug.runId
    bands = [int(l.split(",")[1]) for l in (run_dir / "inputs" / "ignitions.csv").read_text().splitlines()[1:]]
    assert bands == [1, 25, 49, 73]
    prob = np.array([[np.nan if v is None else v for v in row] for row in grid.burnProbability])
    p10 = np.array([[np.nan if v is None else v for v in row] for row in grid.arrivalMinutesP10])
    r, c = _ign_rc(grid)
    assert prob[r, c] == pytest.approx(1.0, abs=0.01) and p10[r, c] == 0.0
    assert prob[r, c + 3] == pytest.approx(1.0, abs=0.01)
    burned = prob > 0
    rows = np.nonzero(burned[:, c + 10])[0]  # downwind cross-section spans both fan directions
    assert rows.max() - rows.min() >= 4
    assert ((prob > 0.05) & (prob < 0.95)).sum() > 0


async def test_spotting_and_no_diurnal_run(tmp_path):
    grid = await _pipeline(tmp_path, diurnal_adjustment=False).run(
        SimulationRequest(Ignition(LAT, LON), duration_hours=1, ensemble_members=1, seed=2, spotting=True)
    )
    assert grid.physics["spotting"] is True and grid.physics["diurnalAdjustment"] is False
    assert sum(1 for row in grid.burnProbability for v in row if v) > 20


async def test_barrier_stops_surface_fire(tmp_path):
    """A 30 m north-south fire break 500 m downwind stops a grass fire (flame length << 20 m);
    exercised on 2 MPI ranks (upstream only broadcast the barrier header - patched in the Dockerfile)."""
    from fire_spread.landscape import lonlat_to_xy
    x0, _ = lonlat_to_xy(LON, LAT)
    start = datetime(2026, 8, 1, 13, 0, tzinfo=timezone.utc)
    req = SimulationRequest(Ignition(LAT, LON), duration_hours=2, ensemble_members=2, seed=4, start_time=start)
    free = await _pipeline(tmp_path).run(req)
    blocked = await _pipeline(tmp_path, barrier=(x0 + 500.0, 30.0)).run(req)
    assert blocked.physics["barriers"] is True and free.physics["barriers"] is False
    for g, expect_far in ((free, True), (blocked, False)):
        prob = np.array([[np.nan if v is None else v for v in row] for row in g.burnProbability])
        r, c = _ign_rc(g)
        east = np.nonzero(prob[r] > 0)[0].max() - c
        assert bool(east > 20) is expect_far, east


async def test_reproducible_seed(tmp_path):
    # start_time pinned: the ignition offset inside the hour (SIMULATION_TSTART) is part of the run
    start = datetime(2026, 8, 1, 13, 0, tzinfo=timezone.utc)
    req = SimulationRequest(Ignition(LAT, LON), duration_hours=1, ensemble_members=2, seed=3, start_time=start)
    a = await _pipeline(tmp_path).run(req)
    b = await _pipeline(tmp_path).run(req)
    assert a.arrivalMinutes == b.arrivalMinutes


async def test_perimeter_ignition(tmp_path):
    """An active perimeter (600 x 600 m square) is burning at t 0 in every member and the
    fire grows out of it - mostly downwind - via the fixed X_IGN/Y_IGN points."""
    from fire_spread.landscape import lonlat_to_xy, xy_to_lonlat
    x0, y0 = lonlat_to_xy(LON, LAT)
    ring = [xy_to_lonlat(x0 + dx, y0 + dy) for dx, dy in ((-300, -300), (300, -300), (300, 300), (-300, 300), (-300, -300))]
    perimeter = {"type": "Polygon", "coordinates": [[list(p) for p in ring]]}
    start = datetime(2026, 8, 1, 13, 0, tzinfo=timezone.utc)
    req = SimulationRequest(Ignition(LAT, LON, perimeter), duration_hours=1, ensemble_members=2, seed=9, start_time=start, debug=True)
    grid = await _pipeline(tmp_path).run(req)
    assert grid.physics["perimeterIgnitions"] > 20
    data = (tmp_path / "runs" / grid.debug.runId / "elmfire.data").read_text()
    assert f"NUM_IGNITIONS = {grid.physics['perimeterIgnitions'] - 1}" in data and "T_IGN(1) = 0.0" in data
    mins, prob = _arr(grid.arrivalMinutes), _arr(grid.burnProbability)
    r, c = _ign_rc(grid)
    # inside the square (±6 cells): arrival 0, probability 1 in both members
    inner = (slice(r - 4, r + 5), slice(c - 4, c + 5))
    assert np.all(mins[inner] == 0.0) and np.all(prob[inner] == 1.0)
    # the fire left the square and ran east (wind from 270)
    burned = ~np.isnan(mins)
    assert burned.sum() > 12 * 12 * 1.5, burned.sum()
    cols = np.nonzero(burned[r])[0]
    assert cols.max() - c > 6 + 5 and (cols.max() - c) > 2 * (c - cols.min()) - 6
    # cells just outside the square burn later than the ring (arrival increases outward)
    assert mins[r, c + 8] > 0 and mins[r, c + 10] >= mins[r, c + 8]

    # a point run from the same reference burns strictly less within the hour
    point = await _pipeline(tmp_path).run(SimulationRequest(Ignition(LAT, LON), duration_hours=1, ensemble_members=2, seed=9, start_time=start))
    assert (~np.isnan(_arr(point.arrivalMinutes))).sum() < burned.sum()


async def test_both_modes_run(tmp_path):
    """base (stock physics) and tuned (our knobs) both complete on the same inputs and
    differ only through the model knobs; the response says which mode produced it."""
    start = datetime(2026, 8, 1, 13, 0, tzinfo=timezone.utc)
    req = SimulationRequest(Ignition(LAT, LON), duration_hours=1, ensemble_members=1, seed=6, start_time=start, debug=True)
    out = {m: await _pipeline(tmp_path, mode=m).run(req) for m in ("base", "tuned")}
    for m, g in out.items():
        assert g.physics["mode"] == m and g.physics["modeKnobs"]["fuel_model_set"] == ("mediterranean" if m == "tuned" else "scott_burgan")
        assert (~np.isnan(_arr(g.arrivalMinutes))).sum() > 50, m
        assert g.debug.timings["elmfire_s"] > 0
    assert out["base"].physics["diurnalAdjustment"] is False and out["tuned"].physics["diurnalAdjustment"] is True
    assert out["base"].arrivalMinutes != out["tuned"].arrivalMinutes


async def test_legacy_output_resolution_and_perimeters(tmp_path):
    """The default GET answers 100 m cells resampled from the 50 m simulation, and the
    decision layer's hourly perimeters nest around the ignition."""
    from fire_spread.aggregate import M_PER_DEG_LAT
    from fire_spread.compat import hourly_perimeters
    from shapely.geometry import Point

    start = datetime(2026, 8, 1, 13, 0, tzinfo=timezone.utc)
    req = SimulationRequest(Ignition(LAT, LON), duration_hours=2, ensemble_members=2, seed=8, start_time=start)
    native = await _pipeline(tmp_path).run(req)
    coarse = await _pipeline(tmp_path).run(SimulationRequest(
        Ignition(LAT, LON), duration_hours=2, ensemble_members=2, seed=8, start_time=start, output_cell_m=100.0))
    assert native.cellSizeM == 50.0 and coarse.cellSizeM == 100.0
    assert coarse.cellDegLat == pytest.approx(100.0 / M_PER_DEG_LAT)
    assert len(coarse.arrivalHours) < len(native.arrivalHours)
    r, c = _ign_rc(coarse)
    assert coarse.arrivalHours[r][c] == 0
    burned = lambda g: sum(v is not None for row in g.arrivalHours for v in row) * g.cellSizeM ** 2  # noqa: E731
    assert burned(coarse) == pytest.approx(burned(native), rel=0.15)  # same fire, coarser cells

    perims = hourly_perimeters(native)
    hours = [h for h, p, _ in perims if p == 1.0]
    assert hours == [1, 2]
    h1, h2 = (g for _, p, g in perims if p == 1.0)
    assert h1.contains(Point(LON, LAT)) and h2.covers(h1) and h2.area > h1.area
