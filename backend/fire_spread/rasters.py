"""Small rasterio helpers shared by landscape/weather/aggregate."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import Affine, from_origin

NODATA = -9999


def grid_transform(xll: float, yll: float, n: int, cellsize: float) -> Affine:
    """Affine for an n×n grid whose lower-left corner is (xll, yll)."""
    return from_origin(xll, yll + n * cellsize, cellsize, cellsize)


def write_raster(
    path: Path,
    data: np.ndarray,
    transform: Affine,
    crs: str,
    dtype: str,
    nodata: float | int | None = NODATA,
    compress: str | None = None,
) -> None:
    """Write a 2-D (single band) or 3-D (bands, rows, cols) array as a GeoTIFF.

    Uncompressed by default: ELMFIRE re-reads every input through gdal_translate and
    DEFLATE costs ~17 % of a run for files that live only inside the run directory."""
    if data.ndim == 2:
        data = data[np.newaxis, ...]
    count, height, width = data.shape
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path, "w", driver="GTiff", height=height, width=width, count=count,
        dtype=dtype, crs=crs, transform=transform, nodata=nodata,
        compress=compress, tiled=True, blockxsize=256, blockysize=256,
    ) as dst:
        dst.write(data.astype(dtype))


def write_bands(path: Path, data: np.ndarray, transform: Affine, crs: str) -> None:
    """Multiband Float32 raster (bands, rows, cols), no nodata (ELMFIRE reads every cell)."""
    write_raster(path, np.ascontiguousarray(data, dtype=np.float32), transform, crs, "float32", nodata=None)


def write_uniform_bands(path: Path, values: np.ndarray, shape: tuple[int, int], transform: Affine, crs: str) -> None:
    """Multiband Float32 raster where each band is spatially uniform (one value per hour)."""
    vals = np.asarray(values, dtype=np.float32)
    write_bands(path, np.broadcast_to(vals[:, None, None], (vals.size, *shape)), transform, crs)
