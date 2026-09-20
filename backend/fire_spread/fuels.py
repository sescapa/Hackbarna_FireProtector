"""Rothermel fuel-model table for ELMFIRE (``FUEL_MODEL_FILE``).

ELMFIRE reads ``num,name,dynamic,w1h,w10h,w100h,wLH,wLW,sav1h,savLH,savLW,depth,mx,heat``
with loads in lb/ft², SAV in 1/ft, depth in ft, moisture of extinction in %, heat in
BTU/lb (elmfire_init.f90 READ_FUEL_MODEL_TABLE_ROTHERMEL). ``data/fuel_models_elmfire.csv``
is ELMFIRE's own table (Anderson 13 + Scott & Burgan 40) in those units;
``data/fuel_models_mediterranean.csv`` re-parameterises the codes used in Catalonia in
metric units and is merged over it (same code numbers, so the fuel raster is untouched).
"""

from __future__ import annotations

import csv
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
SETS = {"scott_burgan": None, "mediterranean": DATA_DIR / "fuel_models_mediterranean.csv"}

T_HA_TO_LB_FT2 = 0.020481  # 1 t/ha = 0.1 kg/m² = 0.020481 lb/ft²
M_TO_FT = 3.28084
PER_CM_TO_PER_FT = 30.48
KJ_KG_TO_BTU_LB = 0.429923


def base_rows() -> list[list[str]]:
    with (DATA_DIR / "fuel_models_elmfire.csv").open(newline="") as f:
        return [r for r in csv.reader(f) if r and r[0].strip()]


def metric_rows(path: Path) -> list[list[str]]:
    """Convert a metric override table to ELMFIRE's row format (comment lines skipped)."""
    out = []
    with path.open(newline="") as f:
        for r in csv.reader(f):
            if not r or r[0].startswith("#"):
                continue
            code, name, dyn, w1, w10, w100, wlh, wlw, s1, slh, slw, depth, mx, heat = r[:14]
            loads = [f"{float(w) * T_HA_TO_LB_FT2:.5f}" for w in (w1, w10, w100, wlh, wlw)]
            savs = [f"{float(s) * PER_CM_TO_PER_FT:.0f}" if float(s) > 0 else "9999" for s in (s1, slh, slw)]
            out.append([
                code.strip(), name.strip(), ".TRUE." if dyn.strip().upper() in ("TRUE", ".TRUE.") else ".FALSE.",
                *loads, *savs, f"{float(depth) * M_TO_FT:.3f}", f"{float(mx):g}", f"{float(heat) * KJ_KG_TO_BTU_LB:.0f}",
            ])
    return out


def fuel_model_table(fuel_set: str = "scott_burgan") -> str:
    """Contents of ``fuel_models.csv`` for ELMFIRE: the base table with the chosen set's
    rows replacing the same codes (rows sorted by code)."""
    if fuel_set not in SETS:
        raise ValueError(f"unknown fuel model set {fuel_set!r}; choose from {sorted(SETS)}")
    rows = {int(r[0]): r for r in base_rows()}
    if SETS[fuel_set] is not None:
        for r in metric_rows(SETS[fuel_set]):
            rows[int(r[0])] = r
    return "\n".join(",".join(rows[k]) for k in sorted(rows)) + "\n"
