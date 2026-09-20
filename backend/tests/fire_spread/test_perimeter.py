"""Active-perimeter fire state: boundary ignition points, burning mask, namelist output."""

import numpy as np
import pytest
from shapely.geometry import Polygon, mapping

from fire_spread import elmfire_config as cfg, landscape as ls
from fire_spread.models import FireStateRequest, Ignition, OutsideCoverage


def _window(n=100, cellsize=50.0, fbfm=102):
    dom = ls.Domain(400000.0, 4600000.0, n, cellsize)
    layers = {"fbfm40": np.full((n, n), fbfm, np.int16)}
    return ls.LandscapeWindow(dom, layers, np.ones((n, n), bool))


def _square_xy(x0, y0, side):
    return Polygon([(x0, y0), (x0 + side, y0), (x0 + side, y0 + side), (x0, y0 + side)])


def test_perimeter_mask_and_boundary_points():
    win = _window()
    geom = _square_xy(401000.0, 4601000.0, 1000.0)  # 20 x 20 cells
    mask = ls.perimeter_mask(win, geom)
    assert mask.sum() == 400
    rows, cols = np.nonzero(mask)
    assert cols.min() == 20 and cols.max() == 39 and rows.min() == 100 - 40 and rows.max() == 100 - 21
    pts = ls.perimeter_ignitions(win, geom, max_points=100)
    # the boundary ring of a 20x20 block is 76 cells; every point is a cell centre on the ring
    assert len(pts) == 76
    for x, y in pts:
        assert (x - 25) % 50 == 0 and (y - 25) % 50 == 0
        col, row = int((x - win.domain.xll) // 50), int((win.domain.yur - y) // 50)
        assert mask[row, col]
        assert col in (20, 39) or row in (60, 79)
    # ordered along the boundary: consecutive points are neighbours
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        assert max(abs(x1 - x0), abs(y1 - y0)) <= 50.0 * 1.01
    # even subsampling keeps the spacing regular
    sub = ls.perimeter_ignitions(win, geom, max_points=19)
    assert len(sub) == 19


def test_perimeter_ignitions_are_subsampled_and_burnable():
    win = _window()
    geom = _square_xy(400500.0, 4600500.0, 4000.0)  # 80x80 cells: ring of 316
    assert len(ls.perimeter_ignitions(win, geom, max_points=1000)) == 316
    pts = ls.perimeter_ignitions(win, geom, max_points=100)
    assert len(pts) == 100
    # non-burnable strip along the west edge: those boundary cells are skipped
    win.layers["fbfm40"][:, :20] = 91
    pts = ls.perimeter_ignitions(win, geom, max_points=1000)
    assert all(int((x - win.domain.xll) // 50) >= 20 for x, _ in pts)
    win.layers["fbfm40"][:] = 98
    with pytest.raises(OutsideCoverage, match="no burnable"):
        ls.perimeter_ignitions(win, geom)


def test_perimeter_must_fit_the_domain():
    win = _window()
    with pytest.raises(OutsideCoverage, match="does not fit"):
        ls.perimeter_ignitions(win, _square_xy(404000.0, 4604000.0, 2000.0))


def test_perimeter_to_xy_projects_and_validates():
    lonlat = Polygon([(1.82, 41.58), (1.84, 41.58), (1.84, 41.60), (1.82, 41.60)])
    geom = ls.perimeter_to_xy(mapping(lonlat))
    x, y = ls.lonlat_to_xy(1.83, 41.59)
    assert geom.contains(__import__("shapely.geometry", fromlist=["Point"]).Point(x, y))
    assert 1.5e6 < geom.area < 4e6  # ~1.7 km x 2.2 km
    with pytest.raises(OutsideCoverage):
        ls.perimeter_to_xy({"type": "Point", "coordinates": [1.83, 41.59]})


def test_namelist_fixed_ignitions():
    p = cfg.ElmfireParams(xllcorner=0, yllcorner=0, cellsize=50, x_ign=125, y_ign=125, duration_s=3600, tstart_s=600,
                          extra_ignitions=[(175.0, 125.0), (225.0, 125.0)])
    text = cfg.render(p)
    assert "NUM_IGNITIONS = 2" in text
    assert "X_IGN(1) = 175.0" in text and "Y_IGN(2) = 125.0" in text and "T_IGN(2) = 600.0" in text
    # still on the CSV path: the reference ignition is case 1's CSV row
    assert "RANDOM_IGNITIONS = .TRUE." in text and cfg.render_ignitions_csv(p).splitlines()[1] == "1,1,125.0,125.0,1e9,-1"
    with pytest.raises(ValueError, match="at most 100"):
        cfg.render(cfg.ElmfireParams(xllcorner=0, yllcorner=0, cellsize=50, x_ign=0, y_ign=0, duration_s=1,
                                     extra_ignitions=[(0.0, 0.0)] * 101))


def test_ignition_model():
    assert not Ignition(41.0, 1.0).is_perimeter
    assert Ignition(41.0, 1.0, {"type": "Polygon", "coordinates": []}).is_perimeter
    ok = FireStateRequest(perimeter={"type": "Polygon", "coordinates": [[[1, 41], [2, 41], [2, 42], [1, 41]]]})
    assert ok.ignition is None and ok.durationHours == 24
    with pytest.raises(ValueError):
        FireStateRequest()
    with pytest.raises(ValueError):
        FireStateRequest(perimeter={"type": "LineString", "coordinates": []})
