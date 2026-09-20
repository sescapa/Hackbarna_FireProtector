import numpy as np
from shapely.geometry import Polygon, box

from fire_spread.models import ArrivalGrid, WeatherSummary
from scripts.fire_spread import hindcast as hc


def _grid(prob):
    rows, cols = prob.shape
    return ArrivalGrid(
        originLat=41.0, originLon=1.0, cellDegLat=0.001, cellDegLon=0.001, cellSizeM=100.0,
        durationMinutes=60, ensembleMembers=1,
        arrivalHours=[[None] * cols] * rows, arrivalMinutes=[[None] * cols] * rows,
        arrivalMinutesP10=[[None] * cols] * rows, arrivalMinutesP90=[[None] * cols] * rows,
        burnProbability=prob.tolist(), weather=WeatherSummary(source="t", windSpeedAvgMs=1, windDirectionAvg=0),
    )


def test_score_perfect_and_partial():
    prob = np.zeros((10, 10)); prob[2:6, 2:6] = 1.0  # rows S->N: lat 41.002-41.006, lon 1.002-1.006
    obs = box(1.002, 41.002, 1.006, 41.006)
    r = hc.score(_grid(prob), obs, 16 * 100.0 ** 2, 0.5)
    assert r["jaccard"] == 1.0 and r["sorensen"] == 1.0 and r["bias"] == 1.0
    half = box(1.002, 41.002, 1.006, 41.004)  # observed is the southern half
    r = hc.score(_grid(prob), half, 8 * 100.0 ** 2, 0.5)
    assert r["jaccard"] == 0.5 and r["bias"] == 2.0
    # observed area outside the (cropped) grid counts as missed
    big = box(0.9, 40.9, 1.2, 41.2)
    r = hc.score(_grid(prob), big, 900 * 100.0 ** 2, 0.5)
    assert r["tp_ha"] == 16.0 and r["jaccard"] < 0.02


def test_upwind_candidates():
    sq = Polygon([(0, 0), (1000, 0), (1000, 1000), (0, 1000)])
    east = hc.upwind_candidates(sq, (1.0, 0.0))  # wind blowing east -> ignite on the west edge, inset 100 m
    assert all(x < 200 for x, _ in east) and len(east) == hc.CANDIDATES  # densified west edge
    calm = hc.upwind_candidates(sq, None)
    assert calm[0] == (500.0, 500.0)


def test_build_settings_applies_mode_then_overrides():
    s = hc.build_settings("base")
    assert (s.pipeline_mode, s.fuel_model_set, s.diurnal_adjustment) == ("base", "scott_burgan", False)
    assert s.keep_runs == "none" and s.weather_ensemble is False and s.hindcast is True
    s = hc.build_settings("tuned", adj=0.8, fuels="scott_burgan", spotting_default=True)
    assert (s.pipeline_mode, s.fuel_model_set, s.adj_factor, s.spotting_default) == ("tuned", "scott_burgan", 0.8, True)
    assert s.diurnal_adjustment is True  # the rest of the bundle still applies


def test_weather_provider_kinds():
    assert hc.weather_provider("archive").archive and not hc.weather_provider("archive").historical
    assert hc.weather_provider("historical").historical and "historical-forecast-api" in hc.weather_provider("historical").base_url
