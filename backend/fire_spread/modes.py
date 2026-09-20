"""Pipeline modes: ``base`` (stock ELMFIRE physics) and ``tuned`` (our Catalan adjustments).

Both modes run on the same inputs - the static tier (ZAFM fuel map with the OSM
agriculture and DARP burn-scar remaps, LiDAR canopy, OSM barriers), the same Open-Meteo
weather and the same ensemble machinery. A mode only decides the *model* knobs below,
so the evaluation loop (``scripts/fire_spread/evaluate.py``) measures our tweaks and
nothing else. ELMFIRE defaults were read from ``build/source/elmfire_namelists.f90`` at
the pinned commit.
"""

from __future__ import annotations

import logging
from typing import Literal, get_args

from .settings import Settings

log = logging.getLogger("fire_spread")

Mode = Literal["base", "tuned"]
MODES: tuple[str, ...] = get_args(Mode)

# Knob -> value per mode. Every knob listed here is overridden by the mode (unless the
# operator set it explicitly in the environment, see ``apply_mode``); a knob absent from
# this table keeps its Settings default in both modes.
MODE_KNOBS: dict[str, dict[str, object]] = {
    "base": {
        "fuel_model_set": "scott_burgan",      # ELMFIRE's own Scott & Burgan 40 table
        "adj_factor": 1.0,
        "diurnal_adjustment": False,           # USE_DIURNAL_ADJUSTMENT_FACTOR default
        "overnight_adjustment_factor": 0.1,    # ELMFIRE default (unused while diurnal is off)
        "max_low": 8.0,                        # ELMFIRE default
        "wind_fluctuations": False,            # WIND_FLUCTUATIONS default
        "lh_moisture_pct": 60.0,               # LH/LW/FOLIAR_MOISTURE_CONTENT defaults
        "lw_moisture_pct": 60.0,
        "foliar_moisture_pct": 90.0,
    },
    "tuned": {
        "fuel_model_set": "mediterranean",     # garriga/maquia/P. halepensis re-parameterisation
        "adj_factor": 1.4,                     # free-burning eval 2026-09-20: 1.0 under-burns (bias 0.53), 1.5 costs Jaccard
        "diurnal_adjustment": True,            # night-time damping, but mild: hourly RH already drives the
        "overnight_adjustment_factor": 0.7,    # lagged dead-fuel moisture, so 0.4 double-counted the night
        "max_low": 8.0,
        "wind_fluctuations": True,             # ±10 % speed / ±18° direction every 30 s
        "lh_moisture_pct": None,               # None -> monthly Catalan climatology (weather.py)
        "lw_moisture_pct": None,
        "foliar_moisture_pct": None,
    },
}


def apply_mode(settings: Settings, mode: str | None = None) -> Settings:
    """Copy of ``settings`` with the mode's knobs applied. A knob the operator set
    explicitly (environment / .env / constructor) is kept and logged, so a single
    ``ADJ_FACTOR=0.8`` still works for experiments without editing this table."""
    mode = mode or settings.pipeline_mode
    if mode not in MODE_KNOBS:
        raise ValueError(f"unknown pipeline mode {mode!r}; choose from {MODES}")
    update: dict[str, object] = {"pipeline_mode": mode}
    for knob, value in MODE_KNOBS[mode].items():
        if knob in settings.model_fields_set and getattr(settings, knob) != value:
            log.info("mode %s: %s=%r kept from the environment (mode value %r)", mode, knob, getattr(settings, knob), value)
            continue
        update[knob] = value
    out = settings.model_copy(update=update)
    # model_copy marks updated fields as "set"; keep the operator's explicit set only, so
    # applying another mode later still overrides the knobs this call wrote.
    object.__setattr__(out, "__pydantic_fields_set__", set(settings.model_fields_set))
    return out


def mode_summary(settings: Settings) -> dict[str, object]:
    """The knobs a mode controls, as resolved in ``settings`` (reported under ``physics``)."""
    return {k: getattr(settings, k) for k in MODE_KNOBS["tuned"]}
