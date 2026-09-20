"""Pipeline modes: knob bundles, precedence, and what actually reaches elmfire.data."""

from datetime import datetime, timezone

import pytest

from fire_spread import modes
from fire_spread.landscape import SyntheticLandscape
from fire_spread.models import Ignition, SimulationRequest
from fire_spread.pipeline import Pipeline, RunDirOnly
from fire_spread.settings import Settings
from fire_spread.weather import ConstantProvider


def test_modes_are_base_and_tuned():
    assert modes.MODES == ("base", "tuned")
    assert set(modes.MODE_KNOBS) == set(modes.MODES)
    assert set(modes.MODE_KNOBS["base"]) == set(modes.MODE_KNOBS["tuned"]), "both modes must define the same knobs"


def test_base_is_stock_elmfire_and_tuned_is_ours():
    base = modes.apply_mode(Settings(), "base")
    tuned = modes.apply_mode(Settings(), "tuned")
    # ELMFIRE namelist defaults (elmfire_namelists.f90 at the pinned commit)
    assert (base.diurnal_adjustment, base.wind_fluctuations, base.max_low) == (False, False, 8.0)
    assert (base.lh_moisture_pct, base.lw_moisture_pct, base.foliar_moisture_pct) == (60.0, 60.0, 90.0)
    assert base.fuel_model_set == "scott_burgan" and base.pipeline_mode == "base"
    # our Catalan adjustments
    assert tuned.fuel_model_set == "mediterranean" and tuned.diurnal_adjustment and tuned.wind_fluctuations
    assert tuned.overnight_adjustment_factor == 0.7 and tuned.adj_factor == 1.4 and tuned.lh_moisture_pct is None
    assert tuned.pipeline_mode == "tuned"
    # data-side settings are identical: modes only differ in model knobs
    for k in ("use_barriers", "weather_grid_n", "weather_ensemble", "domain_size_m", "data_dir", "weather_history_hours"):
        assert getattr(base, k) == getattr(tuned, k), k


def test_default_mode_and_unknown_mode():
    assert Settings().pipeline_mode == "tuned"
    assert modes.apply_mode(Settings()).pipeline_mode == "tuned"
    assert modes.apply_mode(Settings(pipeline_mode="base")).pipeline_mode == "base"
    with pytest.raises(ValueError, match="unknown pipeline mode"):
        modes.apply_mode(Settings(), "fast")


def test_explicit_setting_survives_the_mode(monkeypatch):
    # constructor kwarg
    s = modes.apply_mode(Settings(adj_factor=0.8), "tuned")
    assert s.adj_factor == 0.8 and s.fuel_model_set == "mediterranean"
    # environment variable
    monkeypatch.setenv("FUEL_MODEL_SET", "scott_burgan")
    s = modes.apply_mode(Settings(), "tuned")
    assert s.fuel_model_set == "scott_burgan" and s.diurnal_adjustment is True
    # applying another mode afterwards still switches the knobs the first mode wrote
    monkeypatch.delenv("FUEL_MODEL_SET")
    s = modes.apply_mode(modes.apply_mode(Settings(), "tuned"), "base")
    assert s.fuel_model_set == "scott_burgan" and s.diurnal_adjustment is False


def test_mode_summary_lists_every_knob():
    s = modes.apply_mode(Settings(), "base")
    assert modes.mode_summary(s) == modes.MODE_KNOBS["base"]


def test_pipeline_resolves_mode():
    assert Pipeline(settings=Settings(), mode="base").mode == "base"
    assert Pipeline(settings=Settings(pipeline_mode="base")).mode == "base"
    assert Pipeline(settings=Settings()).mode == "tuned"


@pytest.mark.parametrize("mode", modes.MODES)
async def test_run_directory_reflects_mode(tmp_path, mode):
    """Up to the ELMFIRE call (no binary needed): the namelist and fuel table carry the mode."""
    s = Settings(runs_dir=tmp_path / "runs", keep_runs="all", domain_size_m=2000.0)
    pipe = Pipeline(settings=s, weather_provider=ConstantProvider(ws_ms=5.0, wd_deg=270.0),
                    landscape=SyntheticLandscape(fbfm=145, cellsize=50.0), mode=mode)
    req = SimulationRequest(Ignition(41.59, 1.83), duration_hours=1, ensemble_members=1,
                            start_time=datetime(2026, 8, 1, 13, 0, tzinfo=timezone.utc))
    with pytest.raises(RunDirOnly) as e:
        await pipe.run(req, elmfire_data_only=True)
    run_dir = e.value.run_dir
    data = (run_dir / "elmfire.data").read_text()
    fuels = (run_dir / "inputs" / "fuel_models.csv").read_text()
    if mode == "base":
        assert "USE_DIURNAL_ADJUSTMENT_FACTOR = .FALSE." in data and "WIND_FLUCTUATIONS = .FALSE." in data
        assert "LH_MOISTURE_CONTENT = 60.0" in data and "LW_MOISTURE_CONTENT = 60.0" in data
        assert "SH5-med" not in fuels
    else:
        assert "USE_DIURNAL_ADJUSTMENT_FACTOR = .TRUE." in data and "OVERNIGHT_ADJUSTMENT_FACTOR = 0.70" in data
        assert "WIND_FLUCTUATIONS = .TRUE." in data
        assert "LH_MOISTURE_CONTENT = 30.0" in data  # August climatology
        assert "SH5-med" in fuels
    assert "NUM_IGNITIONS = 0" in data
