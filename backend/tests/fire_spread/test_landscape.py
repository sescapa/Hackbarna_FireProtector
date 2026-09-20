import numpy as np
import pytest

from fire_spread import landscape as ls
from fire_spread.models import OutsideCoverage

rasterio = pytest.importorskip("rasterio")
from rasterio.transform import from_origin  # noqa: E402

from fire_spread.rasters import write_raster  # noqa: E402


@pytest.fixture
def static_dir(tmp_path):
    """200x200 synthetic static tier at 50 m; fbfm 102 except a water blob in the middle."""
    n, res = 200, 50.0
    xll, yll = 400000.0, 4600000.0
    tr = from_origin(xll, yll + n * res, res, res)
    fbfm = np.full((n, n), 102, np.int16)
    fbfm[90:110, 90:110] = 98
    layers = {
        "dem": (np.arange(n * n, dtype=np.int32).reshape(n, n) % 1000).astype(np.int16),
        "slp": np.full((n, n), 10, np.int16), "asp": np.full((n, n), 180, np.int16),
        "fbfm40": fbfm, "cc": np.full((n, n), 40, np.int16), "ch": np.full((n, n), 120, np.int16),
        "cbh": np.full((n, n), 30, np.int16), "cbd": np.full((n, n), 12, np.int16),
    }
    for name, arr in layers.items():
        write_raster(tmp_path / f"{name}.tif", arr, tr, ls.CRS, "int16")
    (tmp_path / "manifest.json").write_text('{"grid": "test"}')
    return tmp_path


def test_domain_snaps_to_grid():
    d = ls.domain_for(400123.0, 4600456.0, 60000.0, 50.0)
    assert d.xll % 50 == 0 and d.yll % 50 == 0 and d.n == 1200
    assert d.xll <= 400123.0 - 30000 < d.xll + 50


def test_lonlat_roundtrip():
    x, y = ls.lonlat_to_xy(1.83, 41.59)
    assert 380000 < x < 420000 and 4590000 < y < 4620000
    lon, lat = ls.xy_to_lonlat(x, y)
    assert (lon, lat) == pytest.approx((1.83, 41.59), abs=1e-9)


def test_window_inside(static_dir):
    land = ls.Landscape(static_dir)
    assert land.manifest == {"grid": "test"}
    d = ls.Domain(402000.0, 4602000.0, 40, 50.0)
    win = land.read_window(d)
    assert win.layers["fbfm40"].shape == (40, 40)
    assert win.coverage.all()
    with rasterio.open(static_dir / "dem.tif") as src:
        full = src.read(1)
    # rows 40:80 from the top (y from 4608000 down to 4606000), cols 40:80
    np.testing.assert_array_equal(win.layers["dem"], full[120:160, 40:80])


def test_window_clipped_at_edge(static_dir):
    land = ls.Landscape(static_dir)
    d = ls.Domain(399000.0, 4599000.0, 40, 50.0)  # 20 cells outside on W and S
    win = land.read_window(d)
    assert win.layers["fbfm40"].shape == (40, 40)
    assert not win.coverage[:, :20].any()
    assert not win.coverage[20:, :].any()
    assert win.coverage[:20, 20:].all()
    assert (win.layers["dem"][:, :20] == ls.NODATA).all()


def test_window_misaligned_raises(static_dir):
    with pytest.raises(ValueError):
        ls.Landscape(static_dir).read_window(ls.Domain(402010.0, 4602000.0, 40, 50.0))


def test_check_ignition(static_dir):
    land = ls.Landscape(static_dir)
    win = land.read_window(ls.Domain(400000.0, 4600000.0, 200, 50.0))
    ls.check_ignition(win, 401000.0, 4601000.0)  # burnable
    with pytest.raises(OutsideCoverage, match="non-burnable"):
        ls.check_ignition(win, 405000.0, 4605000.0)  # water blob centre
    with pytest.raises(OutsideCoverage):
        ls.check_ignition(win, 300000.0, 4601000.0)
    win2 = land.read_window(ls.Domain(399000.0, 4599000.0, 40, 50.0))
    with pytest.raises(OutsideCoverage, match="coverage"):
        ls.check_ignition(win2, 399100.0, 4599100.0)


def test_write_inputs(static_dir, tmp_path):
    land = ls.Landscape(static_dir)
    win = land.read_window(ls.Domain(402000.0, 4602000.0, 40, 50.0))
    out = tmp_path / "inputs"
    ls.write_inputs(win, out)
    names = sorted(p.name for p in out.glob("*.tif"))
    assert names == sorted(f"{n}.tif" for n in (*ls.LAYERS, "adj", "phi"))
    with rasterio.open(out / "phi.tif") as src:
        assert src.dtypes[0] == "float32" and src.read(1).min() == 1.0
    with rasterio.open(out / "slp.tif") as src:
        assert src.dtypes[0] == "int16" and src.crs.to_epsg() == 25831


def test_synthetic_landscape():
    win = ls.SyntheticLandscape(fbfm=102).read_window(ls.Domain(0.0, 0.0, 10, 50.0))
    assert (win.layers["fbfm40"] == 102).all() and win.coverage.all()


def test_domain_offset_downwind():
    x, y, size = 400000.0, 4600000.0, 40000.0
    centred = ls.domain_for(x, y, size, 50.0)
    east = ls.domain_for(x, y, size, 50.0, downwind=(1.0, 0.0), ignition_frac=1 / 3)
    assert centred.xll == pytest.approx(x - size / 2, abs=50)
    # ignition 1/3 from the upwind (west) edge: 2/3 of the domain lies east of it
    assert east.xll == pytest.approx(x - size / 3, abs=50)
    assert east.yll == centred.yll and east.n == centred.n
    assert east.xll % 50 == 0
    north = ls.domain_for(x, y, size, 50.0, downwind=(0.0, 1.0), ignition_frac=1 / 3)
    assert north.yll == pytest.approx(y - size / 3, abs=50) and north.xll == centred.xll
    assert ls.domain_for(x, y, size, 50.0, downwind=None, ignition_frac=1 / 3) == centred


def test_apply_recent_burns():
    fbfm = np.array([[145, 145, 102, 145, ls.NODATA]], np.int16)
    burn = np.array([[2025, 2022, 2022, 2015, 2025]], np.int16)
    out = ls.apply_recent_burns(fbfm, burn, now_year=2026)
    same_year = ls.apply_recent_burns(np.array([145], np.int16), np.array([2026], np.int16), 2026, min_age=1)
    assert same_year[0] == 145  # hindcast: the simulated fire's own scar is ignored
    assert out.tolist() == [[99, 102, 102, 145, ls.NODATA]]  # 1 y: bare; 4 y shrub: GR2; grass unchanged; old: unchanged
    assert fbfm[0, 0] == 145  # pure


def test_read_window_applies_burns(static_dir):
    n = 200
    burn = np.zeros((n, n), np.int16)
    burn[:50, :50] = 2025
    with rasterio.open(static_dir / "dem.tif") as src:
        tr = src.transform
    write_raster(static_dir / "burnyear.tif", burn, tr, ls.CRS, "int16", nodata=0)
    land = ls.Landscape(static_dir)
    win = land.read_window(ls.Domain(400000.0, 4600000.0, 200, 50.0), now_year=2026)
    assert (win.layers["fbfm40"][:50, :50] == 99).all() and (win.layers["fbfm40"][60:, 60:] != 99).any()
    assert win.coverage.all()  # burned cells are still covered
    win2 = land.read_window(ls.Domain(400000.0, 4600000.0, 200, 50.0))  # no year -> untouched
    assert (win2.layers["fbfm40"][:50, :50] == 102).all()


def test_apply_agriculture():
    fbfm = np.array([104, 104, 145, 104], np.int16)
    agri = np.array([1, 2, 1, 0], np.uint8)
    assert ls.apply_agriculture(fbfm, agri).tolist() == [93, 104, 145, 104]
