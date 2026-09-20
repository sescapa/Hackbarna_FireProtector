"""Aggregate ELMFIRE ensemble time-of-arrival rasters into the ArrivalGrid response.

``summarise`` is pure numpy (unit-tested). ``load_stack`` reads the run outputs and
``to_arrival_grid`` reprojects the statistics onto a lat/lon grid anchored so the
ignition sits at a cell centre (same convention as the previous Deepfire grid).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import Affine, from_origin
from rasterio.warp import Resampling, reproject

from .models import ElmfireFailed

M_PER_DEG_LAT = 111_320.0
PAD_CELLS = 2
_TOA_RE = re.compile(r"time_of_arrival_(\d{7})_(\d{7})\.tif$")


@dataclass
class Stats:
    """Per-cell ensemble statistics on the simulation grid. Times in minutes, NaN where
    no member burned; burn_prob in [0, 1]."""

    burn_prob: np.ndarray
    median: np.ndarray
    p10: np.ndarray
    p90: np.ndarray
    members: int


def summarise(stack: np.ndarray) -> Stats:
    """``stack``: (members, rows, cols) arrival times in seconds; values < 0 = unburned."""
    if stack.ndim != 3 or stack.shape[0] == 0:
        raise ValueError("stack must be (members, rows, cols) with >= 1 member")
    s = stack.astype(np.float32)
    burned = s >= 0
    burn_prob = burned.mean(axis=0, dtype=np.float32)
    # Percentiles over burned members only. np.nanpercentile is ~100x too slow at 1.4 M
    # cells, so sort once (unburned -> +inf, i.e. last) and interpolate by position.
    minutes = np.where(burned, s / 60.0, np.inf)
    minutes.sort(axis=0)
    k = burned.sum(axis=0)  # burned members per cell
    p10, median, p90 = (_percentile_sorted(minutes, k, q) for q in (10, 50, 90))
    return Stats(burn_prob, median, p10, p90, int(stack.shape[0]))


def _percentile_sorted(sorted_vals: np.ndarray, k: np.ndarray, q: float) -> np.ndarray:
    """Linear-interpolated percentile along axis 0 using the first k[cell] sorted values."""
    pos = q / 100.0 * np.maximum(k - 1, 0)
    lo = np.floor(pos).astype(np.intp)
    hi = np.minimum(lo + 1, np.maximum(k - 1, 0)).astype(np.intp)
    frac = (pos - lo).astype(np.float32)
    v_lo = np.take_along_axis(sorted_vals, lo[None], axis=0)[0]
    v_hi = np.take_along_axis(sorted_vals, hi[None], axis=0)[0]
    with np.errstate(invalid="ignore"):  # inf - inf where k == 0
        out = v_lo + (v_hi - v_lo) * frac
    out[k == 0] = np.nan
    return out.astype(np.float32)


def find_final_outputs(outputs_dir: Path) -> list[Path]:
    """Latest ``time_of_arrival_<case>_<sec>.tif`` per ensemble case, sorted by case."""
    return [p for _, p in find_final_outputs_by_case(outputs_dir)]


def find_final_outputs_by_case(outputs_dir: Path) -> list[tuple[int, Path]]:
    """(case number, latest time_of_arrival raster) sorted by case."""
    latest: dict[int, tuple[int, Path]] = {}
    for p in outputs_dir.glob("time_of_arrival_*.tif"):
        m = _TOA_RE.search(p.name)
        if not m:
            continue
        case, sec = int(m.group(1)), int(m.group(2))
        if case not in latest or sec > latest[case][0]:
            latest[case] = (sec, p)
    return [(c, latest[c][1]) for c in sorted(latest)]


def load_stack(outputs_dir: Path) -> tuple[list[int], np.ndarray, Affine, str]:
    """(case numbers, (cases, rows, cols) arrival seconds with -1 = unburned, transform, crs).
    Arrival times are ELMFIRE's absolute simulation time (band 1 = 0 s)."""
    files = find_final_outputs_by_case(outputs_dir)
    if not files:
        raise ElmfireFailed(f"no time_of_arrival rasters in {outputs_dir}")
    arrays = []
    transform = crs = None
    for _, f in files:
        with rasterio.open(f) as src:
            a = src.read(1).astype(np.float32)
            if src.nodata is not None:
                a = np.where(a == src.nodata, -1.0, a)
            if transform is None:
                transform, crs = src.transform, src.crs.to_string()
        arrays.append(a)
    return [c for c, _ in files], np.stack(arrays), transform, crs


def _burned_bbox(burn_prob: np.ndarray, ign_rc: tuple[int, int], pad: int) -> tuple[int, int, int, int]:
    """(row0, row1, col0, col1) exclusive-end window covering burned cells + ignition, padded."""
    rows, cols = np.nonzero(burn_prob > 0)
    r, c = ign_rc
    rows = np.append(rows, r)
    cols = np.append(cols, c)
    h, w = burn_prob.shape
    return (
        max(int(rows.min()) - pad, 0), min(int(rows.max()) + pad + 1, h),
        max(int(cols.min()) - pad, 0), min(int(cols.max()) + pad + 1, w),
    )


def to_arrival_grid(
    stats: Stats,
    transform: Affine,
    crs: str,
    coverage: np.ndarray,
    ign_x: float,
    ign_y: float,
    ign_lat: float,
    ign_lon: float,
    cell_size_m: float,
    stamp_ignition: bool = True,
) -> dict:
    """Reproject statistics to a lat/lon grid (~cell_size_m) cropped to the burned extent.

    Returns the grid part of the ArrivalGrid response: rows S->N, cols W->E, ``None``
    where no member burned or outside static-data coverage. ``stamp_ignition`` forces the
    reference cell to t 0 (a point ignition; off for perimeters, whose centroid may lie
    outside the fire).
    """
    res = transform.a
    ign_col = int((ign_x - transform.c) // res)
    ign_row = int((transform.f - ign_y) // res)
    r0, r1, c0, c1 = _burned_bbox(stats.burn_prob, (ign_row, ign_col), PAD_CELLS)
    sub_transform = transform * Affine.translation(c0, r0)

    def crop(a: np.ndarray) -> np.ndarray:
        return np.ascontiguousarray(a[r0:r1, c0:c1])

    cov = crop(coverage)

    # Source-window bounds in lon/lat (all four corners; the projection rotates slightly).
    from .landscape import xy_to_lonlat  # local import keeps aggregate importable without pyproj at test time

    xs = [transform.c + c0 * res, transform.c + c1 * res]
    ys = [transform.f - r1 * res, transform.f - r0 * res]
    corners = [xy_to_lonlat(x, y) for x in xs for y in ys]
    minx, maxx = min(p[0] for p in corners), max(p[0] for p in corners)
    miny, maxy = min(p[1] for p in corners), max(p[1] for p in corners)

    dlat = cell_size_m / M_PER_DEG_LAT
    dlon = cell_size_m / (M_PER_DEG_LAT * math.cos(math.radians(ign_lat)))
    ox = ign_lon - dlon / 2 - math.ceil((ign_lon - dlon / 2 - minx) / dlon) * dlon
    oy = ign_lat - dlat / 2 - math.ceil((ign_lat - dlat / 2 - miny) / dlat) * dlat
    nx = int(math.ceil((maxx - ox) / dlon))
    ny = int(math.ceil((maxy - oy) / dlat))
    dst_transform = from_origin(ox, oy + ny * dlat, dlon, dlat)

    def warp(a: np.ndarray, resampling: Resampling, nodata: float) -> np.ndarray:
        src = np.where(np.isnan(a) | ~cov, nodata, a).astype(np.float32)
        dst = np.full((ny, nx), nodata, np.float32)
        reproject(
            src, dst, src_transform=sub_transform, src_crs=crs, src_nodata=nodata,
            dst_transform=dst_transform, dst_crs="EPSG:4326", dst_nodata=nodata,
            resampling=resampling,
        )
        return dst[::-1]  # rows S->N

    # Nearest everywhere: the two grids are ~1:1 so bilinear only smears 0/1 edges.
    prob = warp(crop(stats.burn_prob), Resampling.nearest, -1.0)
    med = warp(crop(stats.median), Resampling.nearest, -1.0)
    p10 = warp(crop(stats.p10), Resampling.nearest, -1.0)
    p90 = warp(crop(stats.p90), Resampling.nearest, -1.0)

    # The ignition cell burns at t=0 by definition; nearest resampling can miss the single
    # source cell when the ignition is off-centre, so set it explicitly (as the old grid did).
    ir, ic = int((ign_lat - oy) / dlat), int((ign_lon - ox) / dlon)
    if stamp_ignition and 0 <= ir < ny and 0 <= ic < nx and 0 <= ign_row < coverage.shape[0] and 0 <= ign_col < coverage.shape[1] and coverage[ign_row, ign_col]:
        med[ir, ic] = p10[ir, ic] = p90[ir, ic] = 0.0
        prob[ir, ic] = 1.0

    valid_time = med >= 0
    hours = np.where(valid_time, np.ceil(med / 60.0), -1).astype(np.int32)

    return {
        "originLat": float(oy),
        "originLon": float(ox),
        "cellDegLat": dlat,
        "cellDegLon": dlon,
        "cellSizeM": float(cell_size_m),
        "arrivalHours": _to_lists(hours, hours < 0),
        "arrivalMinutes": _to_lists(np.round(med, 1), ~valid_time),
        "arrivalMinutesP10": _to_lists(np.round(p10, 1), p10 < 0),
        "arrivalMinutesP90": _to_lists(np.round(p90, 1), p90 < 0),
        "burnProbability": _to_lists(np.round(np.clip(prob, 0, 1), 3), prob < 0),
    }


def _to_lists(a: np.ndarray, null_mask: np.ndarray) -> list[list]:
    # Object array + tolist() is ~10x faster than a Python comprehension at 1M+ cells.
    obj = a.astype(object)
    obj[null_mask] = None
    return obj.tolist()
