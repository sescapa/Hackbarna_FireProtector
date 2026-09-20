from pathlib import Path

import pytest

from fire_spread import elmfire_config as cfg

GOLDEN = Path(__file__).parent / "fixtures" / "elmfire.data.golden"


def _params(**kw):
    base = dict(
        xllcorner=370000.0, yllcorner=4590000.0, cellsize=50.0, x_ign=400000.0, y_ign=4620000.0,
        duration_s=86400.0, tstart_s=1200.0, cases=4, weather_members=4, bands_per_block=48, seed=1234,
        diurnal=True, forecast_start_hour_utc=14.0, current_year=2026, hour_of_year=5102,
        perturbations=cfg.default_perturbations(sigma_ws_mph=3.0, sigma_wd_deg=15.0, include_wind=False),
    )
    base.update(kw)
    return cfg.ElmfireParams(**base)


def test_render_matches_golden():
    text = cfg.render(_params())
    assert text == GOLDEN.read_text()


def test_render_contains_key_parameters():
    text = cfg.render(_params())
    for needle in (
        "WS_AT_10M = .TRUE.", "DEAD_MC_IN_PERCENT = .TRUE.", "DT_METEOROLOGY = 3600.0",
        "SIMULATION_TSTART = 1200.0", "SIMULATION_TSTOP = 87600.0", "DTDUMP = 86400.0",
        "DUMP_TIME_OF_ARRIVAL = .TRUE.", "CONVERT_TO_GEOTIFF = .TRUE.",
        "RANDOM_IGNITIONS = .TRUE.", "CSV_FIXED_IGNITION_LOCATIONS = .TRUE.", "IGNITIONS_CSV_FILENAME = 'ignitions.csv'",
        "NUM_ENSEMBLE_MEMBERS = 4", "NUM_METEOROLOGY_TIMES = 48", "METEOROLOGY_BAND_START = 1",
        "METEOROLOGY_BAND_STOP = 145", "METEOROLOGY_BAND_SKIP_INTERVAL = 48", "SEED = 1234",
        "NUM_RASTERS_TO_PERTURB = 2", "RASTER_TO_PERTURB(1) = 'M1'", "PDF_TYPE(1) = 'GAUSSIAN'", "PDF_SIGMA(1) = 1.5000",
        "RASTER_TO_PERTURB(2) = 'ADJ'", "PDF_TYPE(2) = 'UNIFORM'", "PDF_LOWER_LIMIT(2) = -0.2000",
        "NUM_IGNITIONS = 0", "COMPUTATIONAL_DOMAIN_XLLCORNER = 370000.0", "A_SRS = 'EPSG:25831'",
        "USE_DIURNAL_ADJUSTMENT_FACTOR = .TRUE.", "CURRENT_YEAR = 2026", "HOUR_OF_YEAR = 5102",
        "FORECAST_START_HOUR = 14.000", "OVERNIGHT_ADJUSTMENT_FACTOR = 0.70",
        "WIND_DIRECTION_FLUCTUATION_INTENSITY = 0.10", "WIND_SPEED_FLUCTUATION_INTENSITY = 0.20",
        "MAX_LOW = 8.0", "FOLIAR_MOISTURE_CONTENT = 90.0", "ENABLE_SPOTTING = .FALSE.",
    ):
        assert needle in text, needle
    assert "USE_BARRIERS" not in text


def test_single_case_has_no_perturbations():
    text = cfg.render(_params(cases=1, weather_members=1))
    assert "NUM_RASTERS_TO_PERTURB = 0" in text
    assert "RASTER_TO_PERTURB(" not in text
    assert "METEOROLOGY_BAND_STOP = 1" in text


def test_wind_perturbations_only_without_ensemble_weather():
    ws, wd, m1, adj = cfg.default_perturbations(2.0, 10.0, include_wind=True)
    assert (ws.raster, ws.pdf, ws.sigma) == ("WS", "GAUSSIAN", 2.0)
    assert (wd.raster, wd.sigma) == ("WD", 10.0)
    assert (m1.raster, m1.sigma) == ("M1", 1.5)  # percent (DEAD_MC_IN_PERCENT)
    assert (adj.pdf, adj.lower, adj.upper) == ("UNIFORM", -0.2, 0.25)
    assert [q.raster for q in cfg.default_perturbations(2.0, 10.0, include_wind=False)] == ["M1", "ADJ"]


def test_ignitions_csv_cycles_weather_members():
    p = _params(cases=6, weather_members=4, bands_per_block=48)
    rows = cfg.render_ignitions_csv(p).splitlines()
    assert rows[0] == "icase,iband,x,y,astop,tstop"
    assert [r.split(",")[1] for r in rows[1:]] == ["1", "49", "97", "145", "1", "49"]
    assert rows[1].split(",")[2:4] == ["400000.0", "4620000.0"]
    assert cfg.start_band(3, 1, 25) == 1


def test_diurnal_requires_whole_days_per_block():
    with pytest.raises(ValueError):
        cfg.render(_params(bands_per_block=26))
    cfg.render(_params(bands_per_block=26, weather_members=1))  # single block: any length
    cfg.render(_params(bands_per_block=26, diurnal=False))  # no diurnal clock


def test_optional_blocks():
    text = cfg.render(_params(use_barriers=True, spotting=True, max_runtime_s=800))
    assert "USE_BARRIERS = .TRUE." in text and "BARRIER_FILENAME = 'barrier'" in text
    assert "ENABLE_SPOTTING = .TRUE." in text and "SPOTTING_DISTANCE_MODEL = 'EMPIRICAL'" in text
    assert "MAX_RUNTIME = 800" in text
    off = cfg.render(_params(diurnal=False))
    assert "USE_DIURNAL_ADJUSTMENT_FACTOR = .FALSE." in off and "OVERNIGHT_ADJUSTMENT_FACTOR" not in off


def test_namelist_groups_are_closed():
    text = cfg.render(_params())
    assert text.count("&") == text.count("\n/\n")
