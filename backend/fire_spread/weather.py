"""Weather inputs: provider protocol, Open-Meteo + fixture providers, fuel-moisture math.

A provider returns hourly series for a set of points (the coarse weather grid over the
simulation domain) covering ``history_hours`` before ``start`` plus ``hours`` from it.
The history is only used to spin up the time-lagged dead-fuel moistures. When the NWP
ensemble is available every member becomes its own weather stream (temperature, RH,
wind and precipitation co-vary physically); otherwise a single deterministic stream is
returned and the pipeline falls back to statistical wind perturbations.

ELMFIRE wants wind at 10 m (mph, ``WS_AT_10M``), direction (deg, from) and 1/10/100-h
dead fuel moisture (%); ``to_bands`` does the conversion.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

import httpx
import numpy as np

from .models import WeatherProviderError

log = logging.getLogger("fire_spread.weather")

MS_TO_MPH = 2.23693629
DEFAULT_SIGMA_WS_FRAC = 0.20  # fallback when the ensemble call fails
DEFAULT_SIGMA_WD_DEG = 20.0
M_MIN_PCT, M_MAX_PCT = 2.0, 35.0
RAIN_MM_PER_H = 0.2       # hourly precipitation that counts as wetting
RAIN_EMC_PCT = 35.0       # fine dead fuel moisture under rain (NFDRS convention)
TAU_H = {"m1": 1.0, "m10": 10.0, "m100": 100.0}  # time-lag classes (hours)
DEFAULT_HISTORY_HOURS = 48

HOURLY_VARS = ("temperature_2m", "relative_humidity_2m", "wind_speed_10m", "wind_direction_10m", "precipitation")


# --- fuel moisture ---------------------------------------------------------------

# Live fuel moisture (%) by month for Mediterranean Catalonia: herbaceous cures through the
# summer (ELMFIRE's dynamic GR/GS/SH models transfer herbaceous load to dead fuel below
# 120 %), woody shrubs bottom out in Aug-Sep. Approximate climatology, overridable via
# settings; a live source (Bombers/GRAF sampling, satellite LFMC) would replace this table.
LH_BY_MONTH = {1: 90, 2: 100, 3: 120, 4: 140, 5: 120, 6: 80, 7: 45, 8: 30, 9: 40, 10: 70, 11: 90, 12: 90}
LW_BY_MONTH = {1: 100, 2: 100, 3: 110, 4: 120, 5: 120, 6: 100, 7: 85, 8: 75, 9: 80, 10: 90, 11: 100, 12: 100}
# Foliar moisture of conifer canopies (%), drives crown-fire initiation (Van Wagner I0).
FMC_BY_MONTH = {1: 110, 2: 110, 3: 105, 4: 100, 5: 100, 6: 95, 7: 90, 8: 85, 9: 90, 10: 100, 11: 110, 12: 110}


def live_fuel_moisture(month: int) -> tuple[float, float]:
    """(LH, LW) in % for the given month (1-12)."""
    return float(LH_BY_MONTH[month]), float(LW_BY_MONTH[month])


def foliar_moisture(month: int) -> float:
    return float(FMC_BY_MONTH[month])


def fine_fuel_emc(temp_c, rh_pct):
    """Equilibrium moisture content (%) of fine dead fuel - Simard (1968), as used
    in NWCG fine dead fuel moisture tables. Vectorised; temperature converted to F."""
    t = np.asarray(temp_c, dtype=float) * 9.0 / 5.0 + 32.0
    h = np.asarray(rh_pct, dtype=float)
    low = 0.03229 + 0.281073 * h - 0.000578 * h * t
    mid = 2.22749 + 0.160107 * h - 0.014784 * t
    high = 21.0606 + 0.005565 * h * h - 0.00035 * h * t - 0.483199 * h
    return np.where(h < 10.0, low, np.where(h < 50.0, mid, high))


def lagged_moisture(emc: np.ndarray, precip_mm: np.ndarray, tau_h: float, m0: np.ndarray | None = None) -> np.ndarray:
    """Time-lag response of a dead-fuel class to the hourly EMC series along axis 0:
    ``m[t] = m[t-1] + (target - m[t-1]) * (1 - exp(-1/tau))`` where the target is the EMC,
    raised to ``RAIN_EMC_PCT`` during wetting hours. ``m0`` seeds the series (default: first
    EMC); the pipeline passes ~48 h of history so the 10-h class is spun up and the 100-h
    class starts from the history mean."""
    emc = np.asarray(emc, dtype=float)
    target = np.where(np.asarray(precip_mm, dtype=float) >= RAIN_MM_PER_H, np.maximum(emc, RAIN_EMC_PCT), emc)
    k = 1.0 - math.exp(-1.0 / tau_h)
    out = np.empty_like(target)
    out[0] = target[0] if m0 is None else m0
    for t in range(1, target.shape[0]):
        out[t] = out[t - 1] + (target[t] - out[t - 1]) * k
    return np.clip(out, M_MIN_PCT, M_MAX_PCT)


def dead_fuel_moisture(temp_c: float, rh_pct: float) -> tuple[float, float, float]:
    """Instantaneous (m1, m10, m100) in % for a single reading - no history, so the slower
    classes are approximated as m1+1 / m1+2. Used for summaries and tests; the pipeline
    uses ``lagged_moisture`` over the hourly series."""
    m1 = float(np.clip(fine_fuel_emc(temp_c, rh_pct), M_MIN_PCT, M_MAX_PCT))
    return m1, min(m1 + 1.0, M_MAX_PCT), min(m1 + 2.0, M_MAX_PCT)


# --- data model ----------------------------------------------------------------------


@dataclass
class MemberSeries:
    """One hourly weather stream. Arrays are (hours,) for a single point or
    (hours, points) for a weather grid; ``precip_mm`` defaults to dry."""

    temp_c: np.ndarray
    rh_pct: np.ndarray
    ws_ms: np.ndarray
    wd_deg: np.ndarray
    precip_mm: np.ndarray | None = None
    FIELDS = ("temp_c", "rh_pct", "ws_ms", "wd_deg", "precip_mm")

    def __post_init__(self):
        if self.precip_mm is None:
            self.precip_mm = np.zeros_like(np.asarray(self.ws_ms, dtype=float))

    def __len__(self) -> int:
        return int(np.asarray(self.ws_ms).shape[0])

    @property
    def n_points(self) -> int:
        a = np.asarray(self.ws_ms)
        return int(a.shape[1]) if a.ndim == 2 else 1

    def point(self, i: int = 0) -> "MemberSeries":
        """1-D view of point ``i`` (the series itself when it is already 1-D)."""
        if np.asarray(self.ws_ms).ndim == 1:
            return self
        return MemberSeries(*(np.asarray(getattr(self, f))[:, i] for f in ("temp_c", "rh_pct", "ws_ms", "wd_deg", "precip_mm")))

    def slice(self, start: int, stop: int | None = None) -> "MemberSeries":
        return MemberSeries(*(np.asarray(getattr(self, f))[start:stop] for f in ("temp_c", "rh_pct", "ws_ms", "wd_deg", "precip_mm")))


@dataclass(frozen=True)
class WeatherGrid:
    """Coarse weather raster geometry (projected CRS): ``n``×``n`` cells of ``cellsize`` with
    lower-left corner (xll, yll); points are cell centres in raster order (row 0 = top)."""

    xll: float
    yll: float
    n: int
    cellsize: float

    def centres(self) -> list[tuple[float, float]]:
        out = []
        for row in range(self.n):
            y = self.yll + (self.n - row - 0.5) * self.cellsize
            for col in range(self.n):
                out.append((self.xll + (col + 0.5) * self.cellsize, y))
        return out

    def nearest_index(self, x: float, y: float) -> int:
        col = min(max(int((x - self.xll) // self.cellsize), 0), self.n - 1)
        row = min(max(int((self.yll + self.n * self.cellsize - y) // self.cellsize), 0), self.n - 1)
        return row * self.n + col


@dataclass
class WeatherResult:
    source: str
    start: datetime  # UTC, on the hour = band 1 of the forecast part
    members: list[MemberSeries]
    sigma_ws_ms: float
    sigma_wd_deg: float
    history_hours: int = 0  # leading hours in every series that precede ``start``
    grid: WeatherGrid | None = None
    meta: dict = field(default_factory=dict)

    @property
    def primary(self) -> MemberSeries:
        return self.members[0]

    @property
    def is_ensemble(self) -> bool:
        return len(self.members) > 1

    def forecast(self, member: int = 0) -> MemberSeries:
        return self.members[member].slice(self.history_hours)

    def summary(self, point: int = 0) -> dict:
        m = self.forecast().point(point)
        bands = to_bands(self.members[0].point(point), self.history_hours)
        return {
            "source": self.source,
            "windSpeedAvgMs": float(np.mean(m.ws_ms)),
            "windDirectionAvg": circular_mean_deg(m.wd_deg),
            "windSpeedSigmaMs": float(self.sigma_ws_ms),
            "windDirectionSigmaDeg": float(self.sigma_wd_deg),
            "fuelMoisture1hAvgPct": float(np.mean(bands["m1"])),
            "fuelMoisture100hAvgPct": float(np.mean(bands["m100"])),
            "weatherMembers": len(self.members),
        }


def circular_mean_deg(deg: np.ndarray) -> float:
    r = np.deg2rad(np.asarray(deg, dtype=float))
    v = float(np.rad2deg(np.arctan2(np.mean(np.sin(r)), np.mean(np.cos(r)))) % 360.0)
    return 0.0 if v >= 360.0 - 1e-9 else v


def circular_std_deg(deg: np.ndarray) -> float:
    r = np.deg2rad(np.asarray(deg, dtype=float))
    R = math.hypot(float(np.mean(np.sin(r))), float(np.mean(np.cos(r))))
    R = min(max(R, 1e-12), 1.0)
    return float(np.rad2deg(math.sqrt(-2.0 * math.log(R))))


def mean_downwind_unit(m: MemberSeries, min_speed_ms: float = 0.5) -> tuple[float, float] | None:
    """Speed-weighted mean wind as a unit vector pointing where the wind blows *to*
    (x east, y north), or None when the horizon-mean wind is calm."""
    m = m.point(0)
    ws = np.asarray(m.ws_ms, dtype=float)
    to = np.deg2rad((np.asarray(m.wd_deg, dtype=float) + 180.0) % 360.0)  # from -> towards
    u, v = float(np.mean(ws * np.sin(to))), float(np.mean(ws * np.cos(to)))
    mag = math.hypot(u, v)
    if mag < min_speed_ms:
        return None
    return u / mag, v / mag


def to_bands(m: MemberSeries, history_hours: int = 0) -> dict[str, np.ndarray]:
    """ELMFIRE weather bands (one per forecast hour): ws (mph), wd (deg), m1/m10/m100 (%).
    Moistures are time-lagged over the full series (history included), then the history
    is dropped. Output arrays are (hours,) or (hours, points), Float32."""
    emc = fine_fuel_emc(m.temp_c, m.rh_pct)
    precip = np.asarray(m.precip_mm, dtype=float)
    hist = emc[:history_hours] if history_hours > 0 else emc[:1]
    m0_100 = hist.mean(axis=0)
    m1 = lagged_moisture(emc, precip, TAU_H["m1"])
    m10 = lagged_moisture(emc, precip, TAU_H["m10"])
    m100 = lagged_moisture(emc, precip, TAU_H["m100"], m0=m0_100)
    h = history_hours
    return {
        "ws": (np.asarray(m.ws_ms, dtype=np.float32)[h:] * MS_TO_MPH).astype(np.float32),
        "wd": (np.asarray(m.wd_deg, dtype=np.float32)[h:] % 360.0).astype(np.float32),
        "m1": m1[h:].astype(np.float32),
        "m10": m10[h:].astype(np.float32),
        "m100": m100[h:].astype(np.float32),
    }


def _fit_length(values: list | np.ndarray, n: int) -> np.ndarray:
    """Pad (repeat last) or truncate a series to n hours; None values are interpolated."""
    a = np.array([np.nan if v is None else float(v) for v in values], dtype=float)
    if a.size == 0:
        raise WeatherProviderError("empty weather series")
    if np.isnan(a).any():
        idx = np.arange(a.size)
        good = ~np.isnan(a)
        if not good.any():
            raise WeatherProviderError("weather series is all null")
        a = np.interp(idx, idx[good], a[good])
    if a.size >= n:
        return a[:n]
    return np.concatenate([a, np.full(n - a.size, a[-1])])


def constant_series(
    n_hours: int, ws_ms: float, wd_deg: float, temp_c: float = 30.0, rh_pct: float = 25.0
) -> MemberSeries:
    return MemberSeries(
        temp_c=np.full(n_hours, temp_c),
        rh_pct=np.full(n_hours, rh_pct),
        ws_ms=np.full(n_hours, ws_ms),
        wd_deg=np.full(n_hours, wd_deg),
    )


def broadcast_points(m: MemberSeries, n_points: int) -> MemberSeries:
    """Repeat a 1-D series for every grid point -> (hours, points)."""
    if n_points <= 1 or np.asarray(m.ws_ms).ndim == 2:
        return m
    return MemberSeries(*(np.repeat(np.asarray(getattr(m, f), dtype=float)[:, None], n_points, axis=1)
                          for f in ("temp_c", "rh_pct", "ws_ms", "wd_deg", "precip_mm")))


def _series_from_hourly(h: dict, n: int, suffix: str = "", fallback: MemberSeries | None = None) -> MemberSeries:
    """Build a series from an Open-Meteo ``hourly`` dict. A variable that is absent or
    entirely null takes the same variable from ``fallback`` (the deterministic run) when
    given - ICON-EU-EPS members, for instance, carry no humidity - else precipitation
    defaults to dry and anything else raises."""
    cols = []
    for i, k in enumerate(HOURLY_VARS):
        key = k + suffix
        vals = h.get(key)
        if vals is None or all(v is None for v in vals):
            if fallback is not None:
                cols.append(np.asarray(getattr(fallback, MemberSeries.FIELDS[i]), dtype=float))
                continue
            if k == "precipitation":
                cols.append(np.zeros(n))
                continue
            raise WeatherProviderError(f"weather series {key} missing or all null")
        cols.append(_fit_length(vals, n))
    return MemberSeries(*cols)


# --- providers -------------------------------------------------------------------


class WeatherProvider(Protocol):
    async def fetch(
        self, points: list[tuple[float, float]], start: datetime, hours: int,
        history_hours: int = 0, members: int = 1,
    ) -> WeatherResult:
        """``points`` are (lat, lon); point 0 is the reference (ignition) point. Returns
        series of ``history_hours + hours`` values per point starting at
        ``start - history_hours``. ``members`` is the number of NWP ensemble members wanted
        (providers may return fewer, including 1)."""
        ...


def floor_hour(t: datetime) -> datetime:
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


class FixtureProvider:
    """Deterministic weather from a JSON file (tests, CLI, offline demos).

    Format: ``{"source": "...", "hourly": {temperature_2m: [...], relative_humidity_2m: [...],
    wind_speed_10m: [...] (m/s), wind_direction_10m: [...], precipitation: [...]},
    "sigma_ws_ms": x, "sigma_wd_deg": y}``. Series shorter than the horizon are extended
    with their last value; the history is the first value repeated.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)

    async def fetch(self, points, start, hours, history_hours=0, members=1) -> WeatherResult:
        try:
            doc = json.loads(self.path.read_text())
        except (OSError, ValueError) as e:
            raise WeatherProviderError(f"cannot read weather fixture {self.path}: {e}") from e
        h = doc["hourly"]
        fc = _series_from_hourly(h, hours)
        member = _prepend_history(fc, history_hours)
        return WeatherResult(
            source=doc.get("source", f"fixture:{self.path.name}"),
            start=floor_hour(start),
            members=[broadcast_points(member, len(points))],
            sigma_ws_ms=float(doc.get("sigma_ws_ms", DEFAULT_SIGMA_WS_FRAC * float(np.mean(fc.ws_ms)))),
            sigma_wd_deg=float(doc.get("sigma_wd_deg", DEFAULT_SIGMA_WD_DEG)),
            history_hours=history_hours,
        )


class ConstantProvider:
    """Synthetic constant weather (integration tests). With ``member_wd_step_deg`` the
    requested ensemble members are returned as streams whose direction fans out around
    ``wd_deg`` (member i: wd + step * (i - (n-1)/2)), emulating an NWP ensemble."""

    def __init__(self, ws_ms: float, wd_deg: float, temp_c: float = 30.0, rh_pct: float = 25.0,
                 sigma_ws_ms: float = 1.0, sigma_wd_deg: float = 10.0, member_wd_step_deg: float | None = None):
        self.ws_ms, self.wd_deg, self.temp_c, self.rh_pct = ws_ms, wd_deg, temp_c, rh_pct
        self.sigma_ws_ms, self.sigma_wd_deg = sigma_ws_ms, sigma_wd_deg
        self.member_wd_step_deg = member_wd_step_deg

    async def fetch(self, points, start, hours, history_hours=0, members=1) -> WeatherResult:
        n = hours + history_hours
        wds = [self.wd_deg]
        if self.member_wd_step_deg is not None and members > 1:
            wds = [self.wd_deg + self.member_wd_step_deg * (i - (members - 1) / 2) for i in range(members)]
        series = [broadcast_points(constant_series(n, self.ws_ms, wd % 360.0, self.temp_c, self.rh_pct), len(points)) for wd in wds]
        return WeatherResult(
            source="constant", start=floor_hour(start), members=series,
            sigma_ws_ms=self.sigma_ws_ms, sigma_wd_deg=self.sigma_wd_deg, history_hours=history_hours,
        )


def _prepend_history(fc: MemberSeries, history_hours: int) -> MemberSeries:
    if history_hours <= 0:
        return fc
    return MemberSeries(*(np.concatenate([np.full(history_hours, np.asarray(getattr(fc, f))[0]), np.asarray(getattr(fc, f))])
                          for f in ("temp_c", "rh_pct", "ws_ms", "wd_deg", "precip_mm")))


class OpenMeteoProvider:
    """Open-Meteo deterministic forecast (``best_match`` -> AROME 1.3 km over Catalonia) at
    every grid point, plus the Ensemble API (ICON-EU-EPS, 40 members) for per-member
    weather streams. If the ensemble call fails the deterministic stream is returned alone
    with default sigmas (the pipeline then perturbs wind statistically)."""

    def __init__(
        self,
        base_url: str = "https://api.open-meteo.com",
        ensemble_base_url: str = "https://ensemble-api.open-meteo.com",
        ensemble_model: str = "icon_eu_eps",
        timeout_s: float = 30.0,
        client: httpx.AsyncClient | None = None,
        archive: bool = False,
        historical: bool = False,
        retry_waits_s: tuple[float, ...] = (5.0, 15.0),
        api_key: str | None = None,
        cache_ttl_s: float = 0.0,
        cache_dir: Path | str | None = None,
    ):
        """Hindcast modes (no ensemble; cases fall back to statistical wind perturbations):
        ``archive=True`` targets the ERA5(-Land) reanalysis endpoint (``/v1/archive`` on
        ``base_url``); ``historical=True`` targets past runs of the high-resolution forecast
        models (``/v1/forecast`` on the historical-forecast host, ``best_match``).

        ``retry_waits_s``: pauses before retrying a 429 (minutely rate limit) or 5xx answer,
        one retry per entry; ``Retry-After`` wins when the server sends it. The live API keeps
        this short; batch hindcasts pass longer waits.

        ``api_key``: Open-Meteo commercial key - added as ``apikey`` to every request and the
        hosts get the ``customer-`` prefix they require. ``cache_ttl_s``: identical requests
        (same points, start, horizon, members) within the window reuse the parsed answer,
        which keeps a demo that re-runs the same ignition from spending quota. ``cache_dir``:
        raw responses stored on disk *without expiry* - only sensible for ``archive`` /
        ``historical`` data, which never changes, so hindcast batches and evaluations replay
        for free after the first run."""
        self.base_url = customer_host(base_url, api_key)
        self.ensemble_base_url = customer_host(ensemble_base_url, api_key)
        self.ensemble_model = ensemble_model
        self.timeout_s = timeout_s
        self._client = client
        self.archive = archive
        self.historical = historical
        self.retry_waits_s = tuple(retry_waits_s)
        self.api_key = api_key
        self.cache_ttl_s = cache_ttl_s
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._cache: dict[tuple, tuple[float, WeatherResult]] = {}

    @staticmethod
    def _fmt(t: datetime) -> str:
        return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:00")

    def _disk_path(self, url: str, params: dict) -> Path | None:
        if self.cache_dir is None:
            return None
        # keyed on the canonical host and the query, so keyed and free-tier runs share entries
        key = json.dumps([url.replace("://customer-", "://"), sorted(params.items())], sort_keys=True)
        return self.cache_dir / (hashlib.sha256(key.encode()).hexdigest()[:24] + ".json")

    async def _get(self, client: httpx.AsyncClient, url: str, params: dict) -> list[dict]:
        disk = self._disk_path(url, params)
        if disk is not None and disk.exists():
            try:
                return json.loads(disk.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass  # unreadable entry: fetch again and overwrite
        docs = await self._get_remote(client, url, params)
        if disk is not None:
            try:
                disk.parent.mkdir(parents=True, exist_ok=True)
                disk.write_text(json.dumps(docs), encoding="utf-8")
            except OSError as e:
                log.warning("cannot write weather cache %s: %s", disk, e)
        return docs

    async def _get_remote(self, client: httpx.AsyncClient, url: str, params: dict) -> list[dict]:
        if self.api_key:
            params = {**params, "apikey": self.api_key}
        for attempt in range(len(self.retry_waits_s) + 1):
            try:
                r = await client.get(url, params=params, timeout=self.timeout_s)
            except httpx.HTTPError as e:
                raise WeatherProviderError(f"Open-Meteo request failed: {e}") from e
            if r.status_code == 200:
                break
            if (r.status_code == 429 or r.status_code >= 500) and attempt < len(self.retry_waits_s):
                wait = self.retry_waits_s[attempt]
                try:
                    wait = max(wait, float(r.headers.get("retry-after", 0)))
                except ValueError:
                    pass
                log.warning("Open-Meteo returned %d; retrying in %.0f s", r.status_code, wait)
                await asyncio.sleep(wait)
                continue
            raise WeatherProviderError(f"Open-Meteo returned {r.status_code}: {r.text[:200]}")
        try:
            doc = r.json()
        except ValueError as e:
            raise WeatherProviderError("Open-Meteo returned invalid JSON") from e
        return doc if isinstance(doc, list) else [doc]

    async def fetch(self, points, start, hours, history_hours=0, members=1) -> WeatherResult:
        start = floor_hour(start)
        key = (tuple((round(p[0], 5), round(p[1], 5)) for p in points), start, hours, history_hours, members)
        if self.cache_ttl_s > 0:
            hit = self._cache.get(key)
            if hit and time.monotonic() - hit[0] < self.cache_ttl_s:
                return copy.copy(hit[1])  # callers set .grid / .members on their copy
        result = await self._fetch(points, start, hours, history_hours, members)
        if self.cache_ttl_s > 0:
            self._cache = {k: v for k, v in self._cache.items() if time.monotonic() - v[0] < self.cache_ttl_s}
            self._cache[key] = (time.monotonic(), copy.copy(result))
        return result

    async def _fetch(self, points, start, hours, history_hours=0, members=1) -> WeatherResult:
        t0 = start - timedelta(hours=history_hours)
        n = history_hours + hours
        end = t0 + timedelta(hours=n - 1)
        params = {
            "latitude": ",".join(f"{p[0]:.5f}" for p in points),
            "longitude": ",".join(f"{p[1]:.5f}" for p in points),
            "hourly": ",".join(HOURLY_VARS),
            "wind_speed_unit": "ms", "timezone": "UTC",
            "start_hour": self._fmt(t0), "end_hour": self._fmt(end),
        }
        own = self._client is None
        client = self._client or httpx.AsyncClient()
        try:
            if self.archive:
                docs = await self._get(client, f"{self.base_url}/v1/archive", params)
            else:
                docs = await self._get(client, f"{self.base_url}/v1/forecast", {**params, "models": "best_match"})
            try:
                det = _stack_points([_series_from_hourly(d["hourly"], n) for d in docs], len(points))
            except (KeyError, TypeError) as e:
                raise WeatherProviderError(f"Open-Meteo response missing hourly data: {e}") from e

            src = "open-meteo:archive" if self.archive else ("open-meteo:historical" if self.historical else "open-meteo:best_match")
            result = WeatherResult(
                source=src, start=start, members=[det],
                sigma_ws_ms=DEFAULT_SIGMA_WS_FRAC * float(np.mean(det.ws_ms)), sigma_wd_deg=DEFAULT_SIGMA_WD_DEG,
                history_hours=history_hours, meta={"ensemble": "none" if (self.archive or self.historical) else "fallback"},
            )
            if self.archive or self.historical:
                return result
            try:
                ens_docs = await self._get(
                    client, f"{self.ensemble_base_url}/v1/ensemble", {**params, "models": self.ensemble_model},
                )
                ens = ensemble_members([d["hourly"] for d in ens_docs], n, len(points),
                                       fallback=[_series_from_hourly(d["hourly"], n) for d in docs])
                result.sigma_ws_ms, result.sigma_wd_deg = ensemble_sigmas(ens)
                if members > 1:
                    result.members = [det, *pick_members(ens, members - 1)]
                    result.source = f"open-meteo:best_match+{self.ensemble_model}"
                result.meta = {"ensemble": self.ensemble_model, "ensembleMembers": len(ens)}
            except (WeatherProviderError, ValueError, KeyError, TypeError) as e:
                log.warning("ensemble weather unavailable (%s); using statistical wind perturbations", e)
        finally:
            if own:
                await client.aclose()
        return result


def customer_host(url: str, api_key: str | None) -> str:
    """Open-Meteo serves paying customers from ``customer-<host>``; add the prefix when a key
    is given and the URL is one of theirs (a proxy or a test server is left alone)."""
    url = url.rstrip("/")
    if api_key and ".open-meteo.com" in url and "://customer-" not in url:
        scheme, host = url.split("://", 1)
        return f"{scheme}://customer-{host}"
    return url


def _stack_points(series: list[MemberSeries], n_points: int) -> MemberSeries:
    if len(series) != n_points:
        raise WeatherProviderError(f"expected {n_points} locations in the response, got {len(series)}")
    if n_points == 1:
        return series[0]
    return MemberSeries(*(np.stack([np.asarray(getattr(s, f), dtype=float) for s in series], axis=1)
                          for f in ("temp_c", "rh_pct", "ws_ms", "wd_deg", "precip_mm")))


def ensemble_members(
    hourly_docs: list[dict], n_hours: int, n_points: int, fallback: list[MemberSeries] | None = None
) -> list[MemberSeries]:
    """Parse ``*_memberNN`` columns of Open-Meteo Ensemble responses (one per point) into
    one MemberSeries per member with (hours, points) arrays. Variables a model does not
    provide for its members are taken from ``fallback`` (per-point deterministic series);
    members that still lack a variable are dropped. Needs at least 2 usable members."""
    suffixes = sorted({k[len("wind_speed_10m"):] for k in hourly_docs[0] if k.startswith("wind_speed_10m_member")})
    out = []
    for sfx in suffixes:
        try:
            per_point = [_series_from_hourly(h, n_hours, sfx, fallback[i] if fallback else None)
                         for i, h in enumerate(hourly_docs)]
        except WeatherProviderError:
            continue
        out.append(_stack_points(per_point, n_points))
    if len(out) < 2:
        raise ValueError("ensemble response has < 2 usable members")
    return out


def pick_members(ens: list[MemberSeries], k: int) -> list[MemberSeries]:
    """``k`` members spread evenly over the ensemble (wrapping if k > available)."""
    if not ens:
        return []
    idx = [int(round(i * len(ens) / k)) % len(ens) for i in range(k)] if k <= len(ens) else [i % len(ens) for i in range(k)]
    return [ens[i] for i in idx]


def ensemble_sigmas(ens: list[MemberSeries]) -> tuple[float, float]:
    """Mean-over-time std-dev across members of 10 m wind speed (m/s) and direction (deg),
    at point 0."""
    if len(ens) < 2:
        raise ValueError("ensemble has < 2 members")
    ws = np.stack([np.asarray(m.point(0).ws_ms, dtype=float) for m in ens])
    wd = np.stack([np.asarray(m.point(0).wd_deg, dtype=float) for m in ens])
    ws_sigma = float(np.nanmean(np.nanstd(ws, axis=0)))
    wd_sigma = float(np.mean([circular_std_deg(wd[:, i]) for i in range(wd.shape[1])]))
    if not (np.isfinite(ws_sigma) and np.isfinite(wd_sigma)):
        raise ValueError("ensemble sigma not finite")
    return ws_sigma, wd_sigma
