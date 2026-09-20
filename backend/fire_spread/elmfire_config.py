"""Render ``elmfire.data`` (Fortran namelists) and ``ignitions.csv`` from plain parameters.

Pure functions: no I/O, unit-testable against a golden file. Parameter names verified
against ELMFIRE main (commit pinned in the Dockerfile) ``build/source/elmfire_namelists.f90``.

ELMFIRE semantics that shape the choices here:
* Every run uses the "fixed ignitions CSV" path (``RANDOM_IGNITIONS`` +
  ``CSV_FIXED_IGNITION_LOCATIONS``): one CSV row per case gives the ignition point *and
  the starting weather band*. Weather rasters hold ``weather_members`` blocks of
  ``bands_per_block`` hourly bands, so each case can run on its own NWP ensemble member.
  Case ``k`` starts at band ``1 + ((k-1) mod members) * bands_per_block``; simulation
  time and time-of-arrival values are absolute (band 1 = t 0), the pipeline subtracts the
  block offset. ``NUM_METEOROLOGY_TIMES`` must equal the bands per case, otherwise
  ELMFIRE never advances past band 1 (elmfire_level_set.f90).
* ``SIMULATION_TSTART`` is the ignition offset inside band 1 (band 1 = the hour that
  contains the ignition), per the user guide's "14:20 -> TSTART = 1200" rule.
* Perturbations are additive. WS in mph, WD in degrees, ADJ dimensionless, dead fuel
  moistures in *percent* on main (``DEAD_MC_IN_PERCENT`` scales them; the 2025.0717 tag
  wanted fractions). ``GAUSSIAN`` uses PDF_MEAN/PDF_SIGMA, ``UNIFORM`` PDF_LOWER/UPPER_LIMIT.
* Wind fluctuations (elmfire_subs.f90 APPLY_WIND_FLUCTUATIONS): one global draw every
  ``DT_WIND_FLUCTUATIONS`` applied to every burning cell; speed factor ``1 + I_ws*(r-0.5)``
  and direction offset ``I_wd*(r-0.5)*360`` degrees. So *both* intensities are fractions
  (0.1 -> ±5 % speed, ±18° direction), not degrees as the user guide says - 15.0 makes
  the direction random and the fire circular.
* Overnight adjustment: ``HOUR_OF_DAY = FORECAST_START_HOUR + T/3600`` compared with
  sunrise/sunset that ELMFIRE computes in UTC from ``CURRENT_YEAR``/``HOUR_OF_YEAR`` and
  the domain's lower-left corner (SUNRISE_HOUR/SUNSET_HOUR are no longer namelist inputs
  on main). So ``forecast_start_hour_utc`` is the UTC hour of band 1 and, with several
  weather blocks, ``bands_per_block`` must be a multiple of 24.
* ``WS_AT_10M = .TRUE.`` makes ELMFIRE scale 10 m wind to 20 ft (x0.87).
* An active perimeter cannot come through the ``PHI`` raster: on the random/CSV ignition
  path ELMFIRE never copies ``PHI0`` into the level set (elmfire_level_set.f90, ``IF (.NOT.
  RANDOM_IGNITIONS) PHIP = PHI0``). The &SIMULATOR fixed ignitions ``X_IGN/Y_IGN/T_IGN``
  *are* applied in every case (at most 100: ``ALREADY_IGNITED(1:100)``), so a perimeter is
  written as up to 100 points along its boundary, all igniting at ``SIMULATION_TSTART``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Perturbation:
    raster: str  # ADJ, WS, WD, M1, ...
    pdf: str = "GAUSSIAN"  # GAUSSIAN (sigma) | UNIFORM (lower, upper)
    sigma: float = 0.0
    lower: float = 0.0
    upper: float = 0.0
    spatial: str = "GLOBAL"  # GLOBAL | PIXEL
    temporal: str = "STATIC"  # STATIC | DYNAMIC


@dataclass
class ElmfireParams:
    xllcorner: float
    yllcorner: float
    cellsize: float
    x_ign: float
    y_ign: float
    duration_s: float
    cases: int = 1
    weather_members: int = 1
    bands_per_block: int = 25
    tstart_s: float = 0.0
    a_srs: str = "EPSG:25831"
    seed: int = 2024
    dt_meteorology_s: float = 3600.0
    lh_moisture_pct: float = 60.0
    lw_moisture_pct: float = 90.0
    foliar_moisture_pct: float = 90.0
    # overnight adjustment; band-1 clock in UTC (ELMFIRE derives sunrise/sunset from it)
    diurnal: bool = False
    forecast_start_hour_utc: float = 0.0
    current_year: int = 2026
    hour_of_year: int = 0
    overnight_adjustment_factor: float = 0.7
    burn_period_length_h: float = 10.0
    burn_period_center_frac: float = 0.667
    # numerics
    dtmax_s: float = 300.0
    target_cfl: float = 0.4
    # physics
    crown_fire_model: int = 1
    crown_ratio: float = 1.0
    critical_canopy_cover: float = 0.39
    max_low: float = 8.0
    wind_fluctuations: bool = True
    ws_fluctuation_intensity: float = 0.2  # ±10 % of speed
    wd_fluctuation_intensity: float = 0.1  # fraction of 360 deg -> ±18 deg
    dt_wind_fluctuations_s: float = 30.0
    wx_bilinear: bool = True
    use_barriers: bool = False
    spotting: bool = False
    max_runtime_s: float | None = None
    perturbations: list[Perturbation] = field(default_factory=list)
    # extra fixed ignition points (x, y) lit at tstart in every case: an active perimeter
    extra_ignitions: list[tuple[float, float]] = field(default_factory=list)
    inputs_dir: str = "./inputs"
    weather_dir: str = "./weather"
    outputs_dir: str = "./outputs"
    scratch_dir: str = "./scratch"
    path_to_gdal: str = "/usr/bin"

    @property
    def tstop_s(self) -> float:
        return self.tstart_s + self.duration_s


def default_perturbations(
    sigma_ws_mph: float, sigma_wd_deg: float, include_wind: bool, sigma_m1_pct: float = 1.5
) -> list[Perturbation]:
    """Ensemble spread. Wind speed/direction from the NWP ensemble Ïƒ only when the cases
    do not already run on real ensemble members; always 1-h fuel moisture (Ïƒ 1.5 %) and
    the spread-rate adjustment factor (0.8-1.25) for model error."""
    out: list[Perturbation] = []
    if include_wind:
        out += [Perturbation("WS", sigma=sigma_ws_mph), Perturbation("WD", sigma=sigma_wd_deg)]
    out += [
        Perturbation("M1", sigma=sigma_m1_pct),
        Perturbation("ADJ", pdf="UNIFORM", lower=-0.2, upper=0.25),
    ]
    return out


def start_band(case: int, weather_members: int, bands_per_block: int) -> int:
    """1-based first weather band of ``case`` (1-based); members are cycled."""
    return 1 + ((case - 1) % max(weather_members, 1)) * bands_per_block


def render_ignitions_csv(p: ElmfireParams) -> str:
    """``ignitions.csv``: case, start band, x, y, area stop (acres; huge = off),
    duration stop (h; -1 = SIMULATION_TSTOP)."""
    lines = ["icase,iband,x,y,astop,tstop"]
    for k in range(1, p.cases + 1):
        lines.append(f"{k},{start_band(k, p.weather_members, p.bands_per_block)},{p.x_ign:.1f},{p.y_ign:.1f},1e9,-1")
    return "\n".join(lines) + "\n"


MAX_FIXED_IGNITIONS = 100  # ALREADY_IGNITED(1:100) in elmfire_level_set.f90


def _b(v: bool) -> str:
    return ".TRUE." if v else ".FALSE."


def render(p: ElmfireParams) -> str:
    """Return the full contents of ``elmfire.data``."""
    if p.weather_members > 1 and p.diurnal and p.bands_per_block % 24 != 0:
        raise ValueError("bands_per_block must be a multiple of 24 when several weather blocks are stacked")
    perturb = p.perturbations if p.cases > 1 else []
    lines: list[str] = []
    add = lines.append

    add("&INPUTS")
    add(f"FUELS_AND_TOPOGRAPHY_DIRECTORY = '{p.inputs_dir}'")
    for key, name in (
        ("ASP", "asp"), ("CBD", "cbd"), ("CBH", "cbh"), ("CC", "cc"), ("CH", "ch"),
        ("DEM", "dem"), ("FBFM", "fbfm40"), ("SLP", "slp"), ("ADJ", "adj"), ("PHI", "phi"),
    ):
        add(f"{key}_FILENAME = '{name}'")
    add("CC_IN_PERCENT = .TRUE.")
    add("CH_TIMES_10 = .TRUE.")
    add("CBH_TIMES_10 = .TRUE.")
    add("CBD_TIMES_100 = .TRUE.")
    add("IGNITIONS_CSV_FILENAME = 'ignitions.csv'")
    if p.use_barriers:
        add("USE_BARRIERS = .TRUE.")
        add("BARRIER_FILENAME = 'barrier'")
    add(f"WEATHER_DIRECTORY = '{p.weather_dir}'")
    for key, name in (("WS", "ws"), ("WD", "wd"), ("M1", "m1"), ("M10", "m10"), ("M100", "m100")):
        add(f"{key}_FILENAME = '{name}'")
    add(f"DT_METEOROLOGY = {p.dt_meteorology_s:.1f}")
    add("WS_AT_10M = .TRUE.")
    add("DEAD_MC_IN_PERCENT = .TRUE.")
    add(f"LH_MOISTURE_CONTENT = {p.lh_moisture_pct:.1f}")
    add(f"LW_MOISTURE_CONTENT = {p.lw_moisture_pct:.1f}")
    add(f"FOLIAR_MOISTURE_CONTENT = {p.foliar_moisture_pct:.1f}")
    add("/")
    add("")

    add("&OUTPUTS")
    add(f"OUTPUTS_DIRECTORY = '{p.outputs_dir}'")
    add(f"DTDUMP = {p.duration_s:.1f}")
    add("DUMP_TIME_OF_ARRIVAL = .TRUE.")
    add("DUMP_FLIN = .FALSE.")
    add("DUMP_SPREAD_RATE = .FALSE.")
    add("CONVERT_TO_GEOTIFF = .TRUE.")
    add("/")
    add("")

    add("&COMPUTATIONAL_DOMAIN")
    add(f"A_SRS = '{p.a_srs}'")
    add(f"COMPUTATIONAL_DOMAIN_CELLSIZE = {p.cellsize:.1f}")
    add(f"COMPUTATIONAL_DOMAIN_XLLCORNER = {p.xllcorner:.1f}")
    add(f"COMPUTATIONAL_DOMAIN_YLLCORNER = {p.yllcorner:.1f}")
    add("/")
    add("")

    add("&TIME_CONTROL")
    add(f"SIMULATION_TSTART = {p.tstart_s:.1f}")
    add(f"SIMULATION_TSTOP = {p.tstop_s:.1f}")
    add("SIMULATION_DT = 5.0")
    add(f"SIMULATION_DTMAX = {p.dtmax_s:.1f}")
    add(f"TARGET_CFL = {p.target_cfl:.2f}")
    add(f"FORECAST_START_HOUR = {p.forecast_start_hour_utc:.3f}")
    add(f"CURRENT_YEAR = {p.current_year}")
    add(f"HOUR_OF_YEAR = {p.hour_of_year}")
    add(f"USE_DIURNAL_ADJUSTMENT_FACTOR = {_b(p.diurnal)}")
    if p.diurnal:
        add(f"OVERNIGHT_ADJUSTMENT_FACTOR = {p.overnight_adjustment_factor:.2f}")
        add(f"BURN_PERIOD_LENGTH = {p.burn_period_length_h:.1f}")
        add(f"BURN_PERIOD_CENTER_FRAC = {p.burn_period_center_frac:.3f}")
    add("/")
    add("")

    add("&SIMULATOR")
    if len(p.extra_ignitions) > MAX_FIXED_IGNITIONS:
        raise ValueError(f"ELMFIRE accepts at most {MAX_FIXED_IGNITIONS} fixed ignition points")
    add(f"NUM_IGNITIONS = {len(p.extra_ignitions)}")
    for i, (x, y) in enumerate(p.extra_ignitions, start=1):
        add(f"X_IGN({i}) = {x:.1f}")
        add(f"Y_IGN({i}) = {y:.1f}")
        add(f"T_IGN({i}) = {p.tstart_s:.1f}")
    add(f"CROWN_FIRE_MODEL = {p.crown_fire_model}")
    add(f"CROWN_RATIO = {p.crown_ratio:.2f}")
    add(f"CRITICAL_CANOPY_COVER = {p.critical_canopy_cover:.2f}")
    add(f"MAX_LOW = {p.max_low:.1f}")
    add(f"WIND_FLUCTUATIONS = {_b(p.wind_fluctuations)}")
    if p.wind_fluctuations:
        add(f"WIND_SPEED_FLUCTUATION_INTENSITY = {p.ws_fluctuation_intensity:.2f}")
        add(f"WIND_DIRECTION_FLUCTUATION_INTENSITY = {p.wd_fluctuation_intensity:.2f}")
        add(f"DT_WIND_FLUCTUATIONS = {p.dt_wind_fluctuations_s:.1f}")
    add(f"WX_BILINEAR_INTERPOLATION = {_b(p.wx_bilinear)}")
    if p.max_runtime_s is not None:
        add(f"MAX_RUNTIME = {p.max_runtime_s:.0f}")
    add("/")
    add("")

    add("&SPOTTING")
    add(f"ENABLE_SPOTTING = {_b(p.spotting)}")
    if p.spotting:
        # UMD model stack: embers per MW of fireline HRR, Sardoy lognormal landing pdf
        # driven by 10 m wind and intensity, Eulerian flux accumulation, direct ignition.
        # Generation percentages are kept low with PIGN = 100 (same statistics, ~10x
        # cheaper than tracking embers that never ignite, per the user guide).
        add("USE_SUPERSEDED_SPOTTING = .FALSE.")
        add("GENERATION_MODEL = 'PER-MW'")
        add("SPOTTING_DISTANCE_MODEL = 'EMPIRICAL'")
        add("ACCUMULATION_MODEL = 'EULERIAN'")
        add("IGNITION_MODEL = 'DIRECT'")
        add("PIGN = 100.0")
        add("CROWN_FIRE_SPOTTING_PERCENT = 2.0")
        add("ENABLE_SURFACE_FIRE_SPOTTING = .TRUE.")
        add("GLOBAL_SURFACE_FIRE_SPOTTING_PERCENT = 0.5")
        add("CRITICAL_SPOTTING_FIRELINE_INTENSITY(:) = 1000.0")
        add("P_EPS = 0.01")
    add("/")
    add("")

    add("&MONTE_CARLO")
    add("RANDOM_IGNITIONS = .TRUE.")
    add("CSV_FIXED_IGNITION_LOCATIONS = .TRUE.")
    add(f"NUM_ENSEMBLE_MEMBERS = {p.cases}")
    add(f"NUM_METEOROLOGY_TIMES = {p.bands_per_block}")
    add("METEOROLOGY_BAND_START = 1")
    add(f"METEOROLOGY_BAND_STOP = {start_band(p.weather_members, p.weather_members, p.bands_per_block)}")
    add(f"METEOROLOGY_BAND_SKIP_INTERVAL = {p.bands_per_block}")
    add(f"SEED = {p.seed}")
    add(f"NUM_RASTERS_TO_PERTURB = {len(perturb)}")
    for i, q in enumerate(perturb, start=1):
        add(f"RASTER_TO_PERTURB({i}) = '{q.raster}'")
        add(f"SPATIAL_PERTURBATION({i}) = '{q.spatial}'")
        add(f"TEMPORAL_PERTURBATION({i}) = '{q.temporal}'")
        add(f"PDF_TYPE({i}) = '{q.pdf}'")
        if q.pdf == "GAUSSIAN":
            add(f"PDF_MEAN({i}) = 0.0")
            add(f"PDF_SIGMA({i}) = {q.sigma:.4f}")
        else:
            add(f"PDF_LOWER_LIMIT({i}) = {q.lower:.4f}")
            add(f"PDF_UPPER_LIMIT({i}) = {q.upper:.4f}")
    add("/")
    add("")

    add("&MISCELLANEOUS")
    add(f"MISCELLANEOUS_INPUTS_DIRECTORY = '{p.inputs_dir}/'")
    add("FUEL_MODEL_FILE = 'fuel_models.csv'")
    add(f"PATH_TO_GDAL = '{p.path_to_gdal}'")
    add(f"SCRATCH = '{p.scratch_dir}'")
    add("/")
    add("")
    return "\n".join(lines)
