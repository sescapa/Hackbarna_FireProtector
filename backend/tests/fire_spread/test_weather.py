import json
from datetime import datetime, timezone

import httpx
import numpy as np
import pytest
import respx

from fire_spread import weather as wx
from fire_spread.models import WeatherProviderError
from tests.fire_spread.conftest import FIXTURES

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "temp_c,rh,expected",
    [
        (30.0, 30.0, 5.76),   # 86 F / 30 %  -> 2.22749 + 4.80321 - 1.27142
        (20.0, 70.0, 12.84),  # 68 F / 70 %  -> high-RH branch
        (35.0, 8.0, 2.0),     # 95 F / 8 %   -> below the 2 % floor
        (0.0, 100.0, 27.27),  # 32 F / 100 % -> high-RH branch
        (30.0, 100.0, 25.38),  # 86 F / 100 %
        (-60.0, 100.0, 31.05),  # -76 F / 100 %
    ],
)
def test_dead_fuel_moisture(temp_c, rh, expected):
    m1, m10, m100 = wx.dead_fuel_moisture(temp_c, rh)
    assert m1 == pytest.approx(expected, abs=0.02)
    assert m10 == pytest.approx(min(m1 + 1, 35))
    assert m100 == pytest.approx(min(m1 + 2, 35))


def test_lagged_moisture_time_constants():
    emc = np.concatenate([np.full(48, 20.0), np.full(24, 5.0)])  # step from wet to dry at hour 48
    rain = np.zeros(72)
    m1 = wx.lagged_moisture(emc, rain, 1.0)
    m10 = wx.lagged_moisture(emc, rain, 10.0)
    m100 = wx.lagged_moisture(emc, rain, 100.0)
    assert m1[47] == pytest.approx(20.0) and m1[52] == pytest.approx(5.0, abs=0.2)
    assert m10[57] == pytest.approx(20 - 15 * (1 - np.exp(-1.0)), abs=0.05)  # ~63 % after one time-lag (10 steps)
    assert m1[60] < m10[60] < m100[60] < 20.0  # slower classes lag behind
    # wetting hour drives the target to 35 % regardless of EMC
    rain[50] = 1.0
    assert wx.lagged_moisture(emc, rain, 1.0)[50] > 20.0


def test_to_bands_units_and_history():
    m = wx.constant_series(3 + 24, ws_ms=10.0, wd_deg=-90.0, temp_c=30.0, rh_pct=30.0)
    b = wx.to_bands(m, history_hours=24)
    assert b["ws"].shape == (3,) and b["ws"].dtype == np.float32
    assert b["ws"][0] == pytest.approx(22.369, abs=1e-2)
    assert b["wd"][0] == 270.0
    assert b["m1"][0] == pytest.approx(5.76, abs=0.02)
    assert b["m100"][0] == pytest.approx(5.76, abs=0.02)  # constant history -> equilibrium
    grid = wx.broadcast_points(m, 4)
    assert wx.to_bands(grid, 24)["m10"].shape == (3, 4)


def test_circular_stats():
    assert wx.circular_mean_deg(np.array([350.0, 10.0])) == pytest.approx(0.0, abs=1e-6)
    assert wx.circular_std_deg(np.array([90.0, 90.0, 90.0])) == pytest.approx(0.0, abs=1e-3)
    assert 5 < wx.circular_std_deg(np.array([80.0, 100.0])) < 15


def _ens_hourly(n=3, members=(("01", 7.0, 190.0), ("02", 9.0, 210.0))):
    h = {"time": [f"2026-08-01T{i:02d}:00" for i in range(n)]}
    for sfx, ws, wd in members:
        h[f"temperature_2m_member{sfx}"] = [30.0] * n
        h[f"relative_humidity_2m_member{sfx}"] = [25.0] * n
        h[f"wind_speed_10m_member{sfx}"] = [ws] * n
        h[f"wind_direction_10m_member{sfx}"] = [wd] * n
        h[f"precipitation_member{sfx}"] = [0.0] * n
    return h


def test_ensemble_member_variable_fallback():
    h = _ens_hourly()
    h["relative_humidity_2m_member01"] = [None] * 3  # ICON-EU-EPS style: no humidity
    del h["relative_humidity_2m_member02"]
    det = wx.constant_series(3, ws_ms=1.0, wd_deg=0.0, temp_c=30.0, rh_pct=42.0)
    ens = wx.ensemble_members([h], 3, 1, fallback=[det])
    assert len(ens) == 2 and ens[0].rh_pct.tolist() == [42.0] * 3 and ens[0].ws_ms.tolist() == [7.0] * 3
    with pytest.raises(ValueError):  # no fallback -> members unusable
        wx.ensemble_members([h], 3, 1)


def test_ensemble_members_and_sigmas():
    ens = wx.ensemble_members([_ens_hourly()], 3, 1)
    assert len(ens) == 2 and ens[0].ws_ms.tolist() == [7.0] * 3
    s_ws, s_wd = wx.ensemble_sigmas(ens)
    assert s_ws == pytest.approx(1.0)
    assert 8 < s_wd < 12
    with pytest.raises(ValueError):
        wx.ensemble_members([{"wind_speed_10m_member01": [1]}], 1, 1)
    # two points -> (hours, points) arrays
    two = wx.ensemble_members([_ens_hourly(), _ens_hourly()], 3, 2)
    assert two[1].wd_deg.shape == (3, 2)
    assert [m.ws_ms[0] for m in wx.pick_members(ens, 3)] == [7.0, 9.0, 7.0]


def test_weather_grid_geometry():
    g = wx.WeatherGrid(xll=0.0, yll=0.0, n=2, cellsize=100.0)
    assert g.centres() == [(50.0, 150.0), (150.0, 150.0), (50.0, 50.0), (150.0, 50.0)]
    assert g.nearest_index(160.0, 20.0) == 3 and g.nearest_index(-5.0, 500.0) == 0


async def test_fixture_provider_pads_series():
    p = wx.FixtureProvider(FIXTURES / "weather_west_30mph.json")
    r = await p.fetch([(41.6, 1.8), (41.7, 1.9)], T0, 25, history_hours=6)
    assert len(r.primary) == 31 and r.primary.n_points == 2
    assert len(r.forecast()) == 25
    assert r.forecast().wd_deg[-1, 1] == 270.0
    assert r.sigma_wd_deg == 15.0
    assert r.summary()["windSpeedAvgMs"] == pytest.approx(13.41)
    assert r.summary()["weatherMembers"] == 1


async def test_fixture_provider_missing_file(tmp_path):
    with pytest.raises(WeatherProviderError):
        await wx.FixtureProvider(tmp_path / "nope.json").fetch([(0, 0)], T0, 2)


def _forecast_doc(n=3):
    return {
        "hourly": {
            "time": [f"2026-08-01T{h:02d}:00" for h in range(n)],
            "temperature_2m": [30.0] * n,
            "relative_humidity_2m": [25.0] * n,
            "wind_speed_10m": [8.0] * n,
            "wind_direction_10m": [200.0] * n,
            "precipitation": [0.0] * n,
        }
    }


@respx.mock
async def test_open_meteo_provider_ensemble_members():
    route = respx.get("https://api.open-meteo.com/v1/forecast").mock(
        return_value=httpx.Response(200, json=[_forecast_doc(), _forecast_doc()]))
    respx.get("https://ensemble-api.open-meteo.com/v1/ensemble").mock(
        return_value=httpx.Response(200, json=[{"hourly": _ens_hourly()}, {"hourly": _ens_hourly()}]))
    p = wx.OpenMeteoProvider()
    r = await p.fetch([(41.6, 1.8), (41.7, 1.9)], datetime(2026, 8, 1, 12, 30, tzinfo=timezone.utc), 2, history_hours=1, members=3)
    assert route.called
    q = dict(route.calls[0].request.url.params)
    assert q["latitude"] == "41.60000,41.70000"
    assert q["start_hour"] == "2026-08-01T11:00" and q["end_hour"] == "2026-08-01T13:00"
    assert q["wind_speed_unit"] == "ms" and "precipitation" in q["hourly"]
    assert r.start == T0 and r.history_hours == 1
    assert len(r.members) == 3 and r.members[0].ws_ms.shape == (3, 2)
    assert r.members[0].ws_ms[0, 0] == 8.0 and r.members[1].ws_ms[0, 1] == 7.0
    assert r.sigma_ws_ms == pytest.approx(1.0)
    assert r.meta["ensembleMembers"] == 2 and r.is_ensemble


@respx.mock
async def test_open_meteo_ensemble_failure_falls_back():
    respx.get("https://api.open-meteo.com/v1/forecast").mock(return_value=httpx.Response(200, json=_forecast_doc()))
    respx.get("https://ensemble-api.open-meteo.com/v1/ensemble").mock(return_value=httpx.Response(500))
    r = await wx.OpenMeteoProvider().fetch([(41.6, 1.8)], T0, 3, members=4)
    assert len(r.members) == 1
    assert r.sigma_ws_ms == pytest.approx(0.2 * 8.0)
    assert r.sigma_wd_deg == wx.DEFAULT_SIGMA_WD_DEG
    assert r.meta["ensemble"] == "fallback"


@respx.mock
@pytest.mark.parametrize("failure", ["status", "network", "badjson", "nohourly", "npoints"])
async def test_open_meteo_errors(failure):
    route = respx.get("https://api.open-meteo.com/v1/forecast")
    if failure == "status":
        route.mock(return_value=httpx.Response(429, text="rate limited"))
    elif failure == "network":
        route.mock(side_effect=httpx.ConnectError("dns"))
    elif failure == "badjson":
        route.mock(return_value=httpx.Response(200, text="<html>"))
    elif failure == "npoints":
        route.mock(return_value=httpx.Response(200, json=[_forecast_doc()]))  # asked for 2
    else:
        route.mock(return_value=httpx.Response(200, json={"error": True}))
    with pytest.raises(WeatherProviderError):
        await wx.OpenMeteoProvider(retry_waits_s=()).fetch([(41.6, 1.8), (41.7, 1.9)], T0, 3)


@respx.mock
async def test_open_meteo_retries_rate_limit(monkeypatch):
    waits = []

    async def fake_sleep(s):
        waits.append(s)

    monkeypatch.setattr(wx.asyncio, "sleep", fake_sleep)
    route = respx.get("https://api.open-meteo.com/v1/forecast").mock(side_effect=[
        httpx.Response(429, text="Minutely API request limit exceeded", headers={"retry-after": "7"}),
        httpx.Response(503),
        httpx.Response(200, json=_forecast_doc()),
    ])
    respx.get("https://ensemble-api.open-meteo.com/v1/ensemble").mock(return_value=httpx.Response(500))
    r = await wx.OpenMeteoProvider(retry_waits_s=(2.0, 5.0)).fetch([(41.6, 1.8)], T0, 3)
    assert route.call_count == 3 and len(r.members) == 1
    # Retry-After wins over the configured wait, then the second wait; the ensemble 500 is retried too
    assert waits == [7.0, 5.0, 2.0, 5.0]
    # retries exhausted -> error; 4xx other than 429 is never retried
    route.mock(return_value=httpx.Response(429, text="still"))
    with pytest.raises(WeatherProviderError, match="429"):
        await wx.OpenMeteoProvider(retry_waits_s=(1.0,)).fetch([(41.6, 1.8)], T0, 3)
    route.mock(return_value=httpx.Response(400, text="bad"))
    waits.clear()
    with pytest.raises(WeatherProviderError, match="400"):
        await wx.OpenMeteoProvider(retry_waits_s=(1.0,)).fetch([(41.6, 1.8)], T0, 3)
    assert waits == []


def test_mean_downwind_unit():
    west = wx.constant_series(3, ws_ms=5.0, wd_deg=270.0)  # from the west -> blows east
    ux, uy = wx.mean_downwind_unit(west)
    assert (ux, uy) == pytest.approx((1.0, 0.0), abs=1e-9)
    north = wx.constant_series(3, ws_ms=5.0, wd_deg=0.0)  # from the north -> blows south
    assert wx.mean_downwind_unit(north) == pytest.approx((0.0, -1.0), abs=1e-9)
    assert wx.mean_downwind_unit(wx.constant_series(3, ws_ms=0.1, wd_deg=90.0)) is None
    # opposing winds of equal strength cancel -> calm
    m = wx.MemberSeries(np.zeros(2), np.zeros(2), np.array([5.0, 5.0]), np.array([0.0, 180.0]))
    assert wx.mean_downwind_unit(m) is None
    assert wx.mean_downwind_unit(wx.broadcast_points(west, 3)) == pytest.approx((1.0, 0.0), abs=1e-9)


def test_live_fuel_moisture_climatology():
    lh_aug, lw_aug = wx.live_fuel_moisture(8)
    lh_apr, lw_apr = wx.live_fuel_moisture(4)
    assert lh_aug < 60 < lh_apr  # herbaceous cured in summer (< 120 % triggers dynamic curing)
    assert lw_aug < lw_apr
    assert wx.foliar_moisture(8) < wx.foliar_moisture(1)
    for m in range(1, 13):
        wx.live_fuel_moisture(m)


@respx.mock
async def test_open_meteo_archive_mode():
    route = respx.get("https://archive-api.open-meteo.com/v1/archive").mock(return_value=httpx.Response(200, json=_forecast_doc()))
    ens = respx.get("https://ensemble-api.open-meteo.com/v1/ensemble").mock(return_value=httpx.Response(500))
    r = await wx.OpenMeteoProvider("https://archive-api.open-meteo.com", archive=True).fetch([(41.6, 1.8)], T0, 3, members=8)
    assert route.called and not ens.called
    assert "models" not in dict(route.calls[0].request.url.params)
    assert r.source == "open-meteo:archive" and len(r.members) == 1


def test_customer_host():
    assert wx.customer_host("https://api.open-meteo.com", "k") == "https://customer-api.open-meteo.com"
    assert wx.customer_host("https://ensemble-api.open-meteo.com/", "k") == "https://customer-ensemble-api.open-meteo.com"
    assert wx.customer_host("https://customer-api.open-meteo.com", "k") == "https://customer-api.open-meteo.com"
    assert wx.customer_host("https://api.open-meteo.com", None) == "https://api.open-meteo.com"
    assert wx.customer_host("http://proxy.local:8080", "k") == "http://proxy.local:8080"


@respx.mock
async def test_open_meteo_api_key_routes_to_customer_hosts():
    fc = respx.get("https://customer-api.open-meteo.com/v1/forecast").mock(return_value=httpx.Response(200, json=_forecast_doc()))
    ens = respx.get("https://customer-ensemble-api.open-meteo.com/v1/ensemble").mock(return_value=httpx.Response(500))
    r = await wx.OpenMeteoProvider(api_key="secret", retry_waits_s=()).fetch([(41.6, 1.8)], T0, 3)
    assert fc.called and ens.called and len(r.members) == 1
    assert dict(fc.calls[0].request.url.params)["apikey"] == "secret"
    assert dict(ens.calls[0].request.url.params)["apikey"] == "secret"


@respx.mock
async def test_open_meteo_cache(monkeypatch):
    fc = respx.get("https://api.open-meteo.com/v1/forecast").mock(return_value=httpx.Response(200, json=_forecast_doc()))
    respx.get("https://ensemble-api.open-meteo.com/v1/ensemble").mock(return_value=httpx.Response(500))
    clock = [1000.0]
    monkeypatch.setattr(wx.time, "monotonic", lambda: clock[0])
    p = wx.OpenMeteoProvider(retry_waits_s=(), cache_ttl_s=600)
    a = await p.fetch([(41.6, 1.8)], T0, 3)
    a.grid = "mutated by the caller"
    b = await p.fetch([(41.6, 1.8)], T0, 3)  # same request -> cached, and a fresh copy
    assert fc.call_count == 1 and b.grid is None and b.members[0] is a.members[0]
    await p.fetch([(41.6, 1.8)], T0, 4)  # different horizon -> new request
    await p.fetch([(41.6, 1.8)], T0, 3, members=4)  # different member count -> new request
    assert fc.call_count == 3
    clock[0] += 599
    await p.fetch([(41.6, 1.8)], T0, 3)
    assert fc.call_count == 3  # still within the TTL
    clock[0] += 2
    await p.fetch([(41.6, 1.8)], T0, 3)
    assert fc.call_count == 4  # expired
    # off by default
    q = wx.OpenMeteoProvider(retry_waits_s=())
    await q.fetch([(41.6, 1.8)], T0, 3); await q.fetch([(41.6, 1.8)], T0, 3)
    assert fc.call_count == 6


@respx.mock
async def test_open_meteo_disk_cache_for_past_weather(tmp_path):
    route = respx.get("https://historical-forecast-api.open-meteo.com/v1/forecast").mock(
        return_value=httpx.Response(200, json=_forecast_doc()))
    keyed = respx.get("https://customer-historical-forecast-api.open-meteo.com/v1/forecast").mock(
        return_value=httpx.Response(200, json=_forecast_doc()))
    p = wx.OpenMeteoProvider("https://historical-forecast-api.open-meteo.com", historical=True, retry_waits_s=(),
                             cache_dir=tmp_path / "wx", api_key="k")
    a = await p.fetch([(41.6, 1.8)], T0, 3)
    files = list((tmp_path / "wx").glob("*.json"))
    assert keyed.call_count == 1 and route.call_count == 0 and len(files) == 1
    assert "apikey" not in files[0].read_text() and '"k"' not in files[0].name
    # a new provider instance (another process, another day, no key) replays from disk
    q = wx.OpenMeteoProvider("https://historical-forecast-api.open-meteo.com", historical=True, retry_waits_s=(), cache_dir=tmp_path / "wx")
    b = await q.fetch([(41.6, 1.8)], T0, 3)
    assert route.call_count == 0 and b.members[0].ws_ms.tolist() == a.members[0].ws_ms.tolist()
    # a different request is a different file; a corrupt entry is refetched and overwritten
    await q.fetch([(41.6, 1.8)], T0, 4)
    assert route.call_count == 1 and len(list((tmp_path / "wx").glob("*.json"))) == 2
    files[0].write_text("{not json")
    await q.fetch([(41.6, 1.8)], T0, 3)
    assert route.call_count == 2 and json.loads(files[0].read_text())
