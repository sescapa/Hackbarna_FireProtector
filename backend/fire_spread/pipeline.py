"""Per-request orchestration: ignition -> landscape window -> weather -> ELMFIRE -> grid.

Every run writes to ``RUNS_DIR/<run_id>/``::

    request.json  inputs/*.tif  inputs/ignitions.csv  weather/*.tif  elmfire.data
    elmfire.out  outputs/time_of_arrival_*.tif  timings.json

so any step can be inspected in QGIS or re-run by hand (``cd runs/<id> && elmfire elmfire.data``).

Weather layout: the weather rasters are coarse (``weather_grid_n`` x ``weather_grid_n``
cells over the domain, ELMFIRE interpolates bilinearly) and stack one block of
``bands_per_block`` hourly bands per NWP ensemble member; case k runs on member
``(k-1) mod members`` (see ``elmfire_config``).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import aggregate, elmfire_config, fuels, landscape as ls, weather as wx
from .elmfire_runner import run_elmfire
from .models import ArrivalGrid, DebugInfo, PipelineError, SimulationRequest
from .modes import apply_mode, mode_summary
from .rasters import grid_transform, write_bands
from .settings import Settings, get_settings
from .solar import sunrise_sunset_utc
from .zones import zone_info

log = logging.getLogger("fire_spread")


class Pipeline:
    """One pipeline = one resolved mode (``settings.pipeline_mode`` after ``apply_mode``);
    the router keeps one per mode and the evaluation loop builds one per mode."""

    def __init__(
        self,
        settings: Settings | None = None,
        weather_provider: wx.WeatherProvider | None = None,
        landscape: ls.Landscape | ls.SyntheticLandscape | None = None,
        mode: str | None = None,
    ):
        self.settings = apply_mode(settings or get_settings(), mode)
        self._provider = weather_provider
        self._landscape = landscape

    @property
    def mode(self) -> str:
        return self.settings.pipeline_mode

    # --- lazily-built collaborators ---------------------------------------------------

    @property
    def provider(self) -> wx.WeatherProvider:
        if self._provider is None:
            s = self.settings
            if s.weather_fixture:
                self._provider = wx.FixtureProvider(s.weather_fixture)
            else:
                self._provider = wx.OpenMeteoProvider(
                    s.open_meteo_base_url, s.open_meteo_ensemble_base_url, s.open_meteo_ensemble_model,
                    api_key=s.open_meteo_api_key, cache_ttl_s=s.weather_cache_ttl_s,
                )
        return self._provider

    @property
    def landscape(self) -> ls.Landscape | ls.SyntheticLandscape:
        if self._landscape is None:
            self._landscape = ls.Landscape(self.settings.data_dir)
        return self._landscape

    @property
    def cellsize(self) -> float:
        return self.settings.cell_size_m or self.landscape.cellsize

    # --- main entry point ---------------------------------------------------------------

    async def run(self, req: SimulationRequest, *, elmfire_data_only: bool = False) -> ArrivalGrid:
        s = self.settings
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
        run_dir = Path(s.runs_dir) / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "request.json").write_text(json.dumps(req.to_json(), indent=2))
        timings: dict[str, float] = {}
        ok = False
        try:
            grid = await self._run_steps(req, run_dir, run_id, timings, elmfire_data_only)
            ok = True
            return grid
        finally:
            (run_dir / "timings.json").write_text(json.dumps(timings, indent=2))
            if s.keep_runs == "none" or (s.keep_runs == "failed" and ok):
                shutil.rmtree(run_dir, ignore_errors=True)

    async def _run_steps(
        self, req: SimulationRequest, run_dir: Path, run_id: str, timings: dict, elmfire_data_only: bool
    ) -> ArrivalGrid:
        s = self.settings
        ign = req.ignition
        start = req.start_time or datetime.now(timezone.utc)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        start = start.astimezone(timezone.utc)
        band1 = wx.floor_hour(start)  # band 1 = the hour containing the ignition
        tstart_s = (start - band1).total_seconds()
        hours = req.duration_hours + 2  # band 1 = t0 ... covers t0 + tstart + duration
        seed = req.seed if req.seed is not None else derive_seed(ign.lat, ign.lon, start)
        spotting = s.spotting_default if req.spotting is None else req.spotting
        cellsize = self.cellsize

        # 1. point forecast at the ignition (needed first: the domain is shifted downwind)
        t = time.perf_counter()
        point_wx = await self.provider.fetch([(ign.lat, ign.lon)], band1, hours, s.weather_history_hours, 1)
        timings["weather_point_s"] = time.perf_counter() - t

        # 2. reference point -> projected domain, placed upwind of centre
        x, y, domain = ls.ignition_to_domain(
            ign, s.domain_size_m, cellsize,
            downwind=wx.mean_downwind_unit(point_wx.forecast()), ignition_frac=s.ignition_frac,
        )
        land = self.landscape
        if not land.contains(x, y):
            raise ls.OutsideCoverage("ignition outside static data coverage (Catalonia)")

        # 3. static landscape window (recent DARP burns remapped for the ignition year).
        # The raster work here, in 4 and in 7 takes seconds of CPU and disk; it runs in a
        # worker thread so the other routes of the app stay responsive during a simulation.
        # An active perimeter becomes fixed ignition points along its boundary (see
        # elmfire_config) plus a mask of the cells already burning at t 0.
        t = time.perf_counter()
        use_barriers = s.use_barriers and land.has_barriers

        def landscape_step():
            win = land.read_window(domain, now_year=start.year, barriers=use_barriers, burn_min_age=1 if s.hindcast else 0)
            if ign.is_perimeter:
                geom = ls.perimeter_to_xy(ign.perimeter)
                burning = ls.perimeter_mask(win, geom)
                points = ls.perimeter_ignitions(win, geom, s.perimeter_max_ignitions, mask=burning)
            else:
                ls.check_ignition(win, x, y)
                points, burning = [(x, y)], None
            ls.write_inputs(win, run_dir / "inputs", adj=s.adj_factor)
            return win, points, burning

        win, ign_points, burning = await asyncio.to_thread(landscape_step)
        x_ign, y_ign = ign_points[0]  # CSV ignition: the point itself, or the first boundary cell
        timings["landscape_s"] = time.perf_counter() - t

        # 4. weather grid over the domain: deterministic + NWP ensemble members
        t = time.perf_counter()
        wgrid = wx.WeatherGrid(domain.xll, domain.yll, max(1, s.weather_grid_n), domain.n * cellsize / max(1, s.weather_grid_n))
        points = [(ign.lat, ign.lon)] if wgrid.n == 1 else [ls.xy_to_lonlat(px, py)[::-1] for px, py in wgrid.centres()]
        members_wanted = req.ensemble_members if s.weather_ensemble else 1
        weather = await self.provider.fetch(points, band1, hours, s.weather_history_hours, members_wanted)
        weather.grid = wgrid
        n_members = len(weather.members)
        # several blocks: pad to whole days so the diurnal clock stays aligned in every block
        bands_per_block = hours if n_members == 1 else 24 * math.ceil(hours / 24)
        await asyncio.to_thread(write_weather, run_dir / "weather", weather, wgrid, bands_per_block)
        timings["weather_grid_s"] = time.perf_counter() - t

        # 5. namelist + ignitions
        month = start.month
        lh, lw = wx.live_fuel_moisture(month)
        lh = s.lh_moisture_pct if s.lh_moisture_pct is not None else lh
        lw = s.lw_moisture_pct if s.lw_moisture_pct is not None else lw
        fmc = s.foliar_moisture_pct if s.foliar_moisture_pct is not None else wx.foliar_moisture(month)
        sunrise, sunset = sunrise_sunset_utc(ign.lat, ign.lon, start.date())  # reported; ELMFIRE recomputes
        hour_of_year = (band1 - datetime(band1.year, 1, 1, tzinfo=timezone.utc)).total_seconds() / 3600
        params = elmfire_config.ElmfireParams(
            xllcorner=domain.xll, yllcorner=domain.yll, cellsize=domain.cellsize,
            x_ign=x_ign, y_ign=y_ign, extra_ignitions=ign_points[1:],
            duration_s=req.duration_hours * 3600.0, tstart_s=tstart_s, a_srs=ls.CRS,
            cases=req.ensemble_members, weather_members=n_members, bands_per_block=bands_per_block,
            seed=seed, lh_moisture_pct=lh, lw_moisture_pct=lw, foliar_moisture_pct=fmc,
            diurnal=s.diurnal_adjustment, forecast_start_hour_utc=band1.hour,
            current_year=band1.year, hour_of_year=int(hour_of_year),
            overnight_adjustment_factor=s.overnight_adjustment_factor,
            max_low=s.max_low, crown_ratio=s.crown_ratio, wind_fluctuations=s.wind_fluctuations,
            wx_bilinear=s.weather_bilinear and wgrid.n > 1,
            use_barriers=win.barrier is not None, spotting=spotting,
            max_runtime_s=max(60.0, s.elmfire_timeout_s - 30.0),
            perturbations=elmfire_config.default_perturbations(
                weather.sigma_ws_ms * wx.MS_TO_MPH, weather.sigma_wd_deg, include_wind=n_members == 1
            ),
        )
        (run_dir / "elmfire.data").write_text(elmfire_config.render(params))
        (run_dir / "inputs" / "ignitions.csv").write_text(elmfire_config.render_ignitions_csv(params))
        (run_dir / "inputs" / "fuel_models.csv").write_text(fuels.fuel_model_table(s.fuel_model_set))
        if elmfire_data_only:
            raise RunDirOnly(run_dir)

        # 6. ELMFIRE
        res = await run_elmfire(run_dir, s.elmfire_nproc, req.ensemble_members, s.elmfire_timeout_s)
        timings["elmfire_s"] = res.seconds
        log.info("run %s: elmfire %d cases on %d weather members in %.1fs",
                 run_id, req.ensemble_members, n_members, res.seconds)

        # 7. aggregate (arrival times relative to the ignition, clipped to the horizon)
        t = time.perf_counter()

        def aggregate_step():
            cases, stack, transform, crs = aggregate.load_stack(run_dir / "outputs")
            if len(cases) != req.ensemble_members:
                log.warning("run %s: expected %d cases, found %d", run_id, req.ensemble_members, len(cases))
            offsets = np.array([
                (elmfire_config.start_band(k, n_members, bands_per_block) - 1) * 3600.0 + tstart_s for k in cases
            ], dtype=np.float32)
            burned = stack >= 0
            stack = np.where(burned, np.clip(stack - offsets[:, None, None], 0.0, req.duration_hours * 3600.0), -1.0)
            if burning is not None:
                stack[:, burning] = 0.0  # inside the perimeter: burning at t 0 by definition
            else:
                # ELMFIRE stamps the ignition cell with the time of its first (CFL-sized) step
                ir, ic = int((domain.yur - y) // cellsize), int((x - domain.xll) // cellsize)
                stack[:, ir, ic] = 0.0
            stats = aggregate.summarise(stack)
            return stats, aggregate.to_arrival_grid(stats, transform, crs, win.coverage, x, y, ign.lat, ign.lon,
                                                    req.output_cell_m or cellsize, stamp_ignition=burning is None)

        stats, grid = await asyncio.to_thread(aggregate_step)
        timings["aggregate_s"] = time.perf_counter() - t

        zone = zone_info(Path(s.data_dir), ign.lat, ign.lon) if isinstance(land, ls.Landscape) else {}
        out = ArrivalGrid(
            **grid,
            durationMinutes=req.duration_hours * 60,
            ensembleMembers=int(stats.members),
            weather={**weather.summary(wgrid.nearest_index(x, y)), "liveHerbaceousPct": lh, "liveWoodyPct": lw,
                     "foliarMoisturePct": fmc},
            physics={
                "mode": s.pipeline_mode, "modeKnobs": mode_summary(s),
                "spotting": spotting, "barriers": win.barrier is not None, "diurnalAdjustment": s.diurnal_adjustment,
                "sunriseUtc": round(sunrise, 2), "sunsetUtc": round(sunset, 2),
                "weatherGrid": wgrid.n, "cellSizeM": cellsize, "ignitionOffsetS": tstart_s,
                "fuelModelSet": s.fuel_model_set, "adjFactor": s.adj_factor,
                "perimeterIgnitions": len(ign_points) if ign.is_perimeter else 0,
            },
            zone=zone,
        )
        if req.debug:
            out.debug = DebugInfo(**{
                "runId": run_id, "runDir": str(run_dir), "timings": timings,
                "elmfireStdoutTail": res.stdout_tail(),
            })
        return out


WX_EDGE_PAD = 2  # coarse weather cells replicated beyond the top/right domain edges


def write_weather(out_dir: Path, weather: wx.WeatherResult, grid: wx.WeatherGrid, bands_per_block: int) -> None:
    """Write ws/wd/m1/m10/m100 rasters: (members * bands_per_block, n+pad, n+pad) on the
    coarse grid. Blocks shorter than ``bands_per_block`` are padded with their last hour.

    ELMFIRE's bilinear lookup (GET_BILINEAR_INTERPOLATE_COEFFS) rounds to the *upper*
    weather cell and clamps, so the last 1.5 cells on the right/top are degenerate and the
    top-right corner divides by zero. The grid is therefore extended by ``WX_EDGE_PAD``
    edge-replicated cells beyond the domain so the degenerate zone lies outside it."""
    pad = WX_EDGE_PAD if grid.n > 1 else 0
    tr = grid_transform(grid.xll, grid.yll, grid.n + pad, grid.cellsize)
    stacks: dict[str, list[np.ndarray]] = {k: [] for k in ("ws", "wd", "m1", "m10", "m100")}
    for m in weather.members:
        bands = wx.to_bands(m, weather.history_hours)
        for name, arr in bands.items():
            a = arr.reshape(arr.shape[0], grid.n, grid.n) if arr.ndim == 2 else np.broadcast_to(arr[:, None, None], (arr.shape[0], grid.n, grid.n))
            if a.shape[0] < bands_per_block:
                a = np.concatenate([a, np.repeat(a[-1:], bands_per_block - a.shape[0], axis=0)])
            a = np.pad(a[:bands_per_block], ((0, 0), (pad, 0), (0, pad)), mode="edge")  # top rows, right cols
            stacks[name].append(np.asarray(a, dtype=np.float32))
    for name, blocks in stacks.items():
        write_bands(out_dir / f"{name}.tif", np.concatenate(blocks, axis=0), tr, ls.CRS)


class RunDirOnly(PipelineError):
    """Raised by ``run(elmfire_data_only=True)`` once the run directory is complete."""

    def __init__(self, run_dir: Path):
        super().__init__(str(run_dir))
        self.run_dir = run_dir


def derive_seed(lat: float, lon: float, start: datetime) -> int:
    """Reproducible ELMFIRE seed from the request (positive 31-bit int)."""
    key = f"{lat:.5f},{lon:.5f},{start.replace(minute=0, second=0, microsecond=0).isoformat()}"
    return int.from_bytes(hashlib.sha256(key.encode()).digest()[:4], "big") & 0x7FFFFFFF or 1
