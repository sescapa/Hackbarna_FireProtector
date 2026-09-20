"""Drop-in for the Deepfire client the decision layer (``app/decision``) was written against.

``app/decision/bundle.py`` simulates through ``fire_spread.router.get_deepfire()`` and consumes
``run_simulation_detailed(lat, lon) -> [(hour, burn_probability, perimeter)]``: WGS84
perimeters per simulated hour, one per probability level - the area at least that fraction
of the ensemble had burned within that many hours (the recorded bundles under
``data/bundles/`` show Deepfire's levels 0.1 .. 1.0 at every hour). Hour *h* at a level
contains hour *h-1* at the same level, and a lower level contains a higher one.
``ElmfireSpread`` keeps that shape and backs it with the ELMFIRE pipeline (configured mode,
live weather), so nothing under ``/api`` changes when Deepfire goes.

Per cell the ensemble grid carries the burn probability and the P10 / median / P90 arrival
over the members that burned, so P(burned within t) = burnProbability x F(t) with F the
burned-member arrival distribution, taken piecewise linear through (P10, 0.1), (median, 0.5),
(P90, 0.9) and 1 beyond P90. The level-``p`` perimeter at hour ``h`` is the union of the cells
where that probability reaches ``p``; levels whose polygon equals the next higher one are
dropped, so a single-member run yields only the 1.0 perimeters, as it did with Deepfire.
"""

from __future__ import annotations

import numpy as np
from affine import Affine
from rasterio import features
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from .models import ArrivalGrid, Ignition, SimulationRequest

DURATION_HOURS = 24  # what deepfire.py ran
ENSEMBLE_MEMBERS = 16  # the GET /fire/arrival-grid default (Deepfire used 10)
LEVELS = tuple(round(0.1 * k, 1) for k in range(10, 0, -1))  # 1.0, 0.9, ... 0.1
_EPS = 1e-6


def _array(rows: list[list], fill: float = np.nan) -> np.ndarray:
    a = np.array([[fill if v is None else v for v in r] for r in rows], dtype=np.float64)
    return a.reshape(len(rows), -1) if rows else a.reshape(0, 0)


def _polygon(mask: np.ndarray, transform: Affine) -> BaseGeometry | None:
    if not mask.any():
        return None
    parts = [shape(g) for g, v in features.shapes(mask.astype(np.uint8), mask=mask, transform=transform) if v]
    geom = unary_union(parts)
    return None if geom.is_empty else geom


def burned_within(grid: ArrivalGrid, minutes: float) -> np.ndarray:
    """Per-cell probability that the fire has reached the cell ``minutes`` after ignition."""
    prob = np.nan_to_num(_array(grid.burnProbability), nan=0.0)
    p10, med, p90 = (_array(x) for x in (grid.arrivalMinutesP10, grid.arrivalMinutes, grid.arrivalMinutesP90))
    with np.errstate(invalid="ignore"):
        lo = 0.1 + 0.4 * np.clip((minutes - p10) / np.maximum(med - p10, _EPS), 0.0, 1.0)
        hi = 0.5 + 0.4 * np.clip((minutes - med) / np.maximum(p90 - med, _EPS), 0.0, 1.0)
        f = np.where(minutes < p10, 0.0, np.where(minutes < med, lo, np.where(minutes < p90, hi, 1.0)))
    return prob * np.nan_to_num(f, nan=0.0)


def hourly_perimeters(grid: ArrivalGrid) -> list[tuple[int, float, BaseGeometry]]:
    """``(hour, probability level, WGS84 perimeter)`` sorted by hour, descending level within an hour."""
    if not grid.arrivalHours or not grid.arrivalHours[0]:
        return []
    # Row 0 is the southernmost row, so the transform's y step is positive.
    transform = Affine(grid.cellDegLon, 0.0, grid.originLon, 0.0, grid.cellDegLat, grid.originLat)
    out: list[tuple[int, float, BaseGeometry]] = []
    for h in range(1, int(round(grid.durationMinutes / 60)) + 1):
        p_h = burned_within(grid, h * 60.0)
        previous: np.ndarray | None = None
        for level in LEVELS:
            mask = p_h >= level - _EPS
            if previous is not None and np.array_equal(mask, previous):
                continue  # nothing new at this level: Deepfire reported no such band either
            previous = mask
            geom = _polygon(mask, transform)
            if geom is not None:
                out.append((h, level, geom))
    return out


class ElmfireSpread:
    """The two methods ``app/decision`` calls on ``get_deepfire()``."""

    async def run_simulation_detailed(self, lat: float, lon: float) -> list[tuple[int, float, BaseGeometry]]:
        from .router import run_grid  # late: router imports this module

        grid = await run_grid(SimulationRequest(
            ignition=Ignition(lat=lat, lon=lon), duration_hours=DURATION_HOURS, ensemble_members=ENSEMBLE_MEMBERS,
        ))
        return hourly_perimeters(grid)

    async def run_simulation(self, lat: float, lon: float) -> list[tuple[int, BaseGeometry]]:
        """Only the perimeters every member agrees on (they nest; a fringe does not)."""
        perimeters = await self.run_simulation_detailed(lat, lon)
        certain = max((p for _, p, _ in perimeters), default=1.0)
        return [(hour, geom) for hour, prob, geom in perimeters if prob >= certain]
