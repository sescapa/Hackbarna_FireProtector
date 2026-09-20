import numpy as np
import pytest

from fire_spread import aggregate as ag


def _stack():
    # 3 members, 2x3 grid, seconds; -1/-9999 = unburned
    m1 = np.array([[0, 600, 1200], [-1, -1, 6000]], float)
    m2 = np.array([[0, 1200, 2400], [-9999, 3000, -1]], float)
    m3 = np.array([[0, 1800, -1], [-1, -1, -1]], float)
    return np.stack([m1, m2, m3])


def test_summarise_stats():
    s = ag.summarise(_stack())
    assert s.members == 3
    np.testing.assert_allclose(s.burn_prob, [[1, 1, 2 / 3], [0, 1 / 3, 1 / 3]], atol=1e-6)
    assert s.median[0, 0] == 0
    assert s.median[0, 1] == pytest.approx(20.0)  # minutes: median of 10, 20, 30
    assert s.p10[0, 1] == pytest.approx(12.0) and s.p90[0, 1] == pytest.approx(28.0)
    assert s.median[0, 2] == pytest.approx(30.0)  # median of 20, 40
    assert np.isnan(s.median[1, 0]) and np.isnan(s.p10[1, 0])
    assert s.median[1, 1] == pytest.approx(50.0)


def test_summarise_ordering_everywhere():
    rng = np.random.default_rng(0)
    stack = rng.uniform(-1000, 5000, size=(8, 20, 20))
    s = ag.summarise(stack)
    ok = ~np.isnan(s.median)
    assert np.all(s.p10[ok] <= s.median[ok] + 1e-6)
    assert np.all(s.median[ok] <= s.p90[ok] + 1e-6)
    assert np.all((s.burn_prob >= 0) & (s.burn_prob <= 1))


def test_summarise_single_member():
    s = ag.summarise(_stack()[:1])
    assert set(np.unique(s.burn_prob)) <= {0.0, 1.0}
    assert s.median[0, 1] == s.p10[0, 1] == s.p90[0, 1] == pytest.approx(10.0)


def test_summarise_rejects_bad_shape():
    with pytest.raises(ValueError):
        ag.summarise(np.zeros((3, 3)))


def test_find_final_outputs(tmp_path):
    for name in (
        "time_of_arrival_0000001_0003600.tif", "time_of_arrival_0000001_0086404.tif",
        "time_of_arrival_0000002_0086404.tif", "flin_0000001_0086404.tif",
    ):
        (tmp_path / name).write_bytes(b"")
    files = ag.find_final_outputs(tmp_path)
    assert [f.name for f in files] == ["time_of_arrival_0000001_0086404.tif", "time_of_arrival_0000002_0086404.tif"]


def test_to_arrival_grid_roundtrip():
    pytest.importorskip("rasterio")
    from rasterio.transform import from_origin

    from fire_spread.landscape import lonlat_to_xy

    n, res = 40, 50.0
    lat, lon = 41.59, 1.83
    x, y = lonlat_to_xy(lon, lat)
    xll, yll = x - n / 2 * res, y - n / 2 * res
    transform = from_origin(xll, yll + n * res, res, res)
    toa = np.full((n, n), -1.0)
    ci, ri = int((x - xll) // res), int((yll + n * res - y) // res)
    toa[ri, ci] = 0
    toa[ri, ci + 1 : ci + 8] = np.arange(1, 8) * 600  # spreads east: 10, 20, ... minutes
    s = ag.summarise(toa[None])
    g = ag.to_arrival_grid(s, transform, "EPSG:25831", np.ones((n, n), bool), x, y, lat, lon, res)

    rows, cols = len(g["arrivalMinutes"]), len(g["arrivalMinutes"][0])
    assert rows > 0 and cols > 0
    # ignition sits at the centre of a cell
    ign_col = int((lon - g["originLon"]) / g["cellDegLon"])
    ign_row = int((lat - g["originLat"]) / g["cellDegLat"])
    assert g["arrivalMinutes"][ign_row][ign_col] == 0
    assert g["arrivalHours"][ign_row][ign_col] == 0
    assert g["burnProbability"][ign_row][ign_col] == pytest.approx(1.0, abs=0.01)
    # cells to the east are burned later; nothing burned to the west
    east = [g["arrivalMinutes"][ign_row][ign_col + k] for k in range(1, 6)]
    assert all(v is not None for v in east) and east == sorted(east)
    assert g["arrivalMinutes"][ign_row][ign_col - 1] is None
    assert g["arrivalHours"][ign_row][ign_col + 5] == 1
    # grid covers the burned bbox only (cropped), not the whole 40x40 domain
    assert rows < n and cols < n
