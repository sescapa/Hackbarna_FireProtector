#!/usr/bin/env python
"""Evaluation execution: run the ``base`` and ``tuned`` pipeline modes on historical weather
against real free-burning Catalan wildfires and compare them with the observed spread.

Run inside the container (ELMFIRE + ogr2ogr + static tier), from ``backend/``:

    scripts/evaluate_fire_spread.sh                       # both modes, curated fire set, 4 members
    scripts/evaluate_fire_spread.sh --members 8 --weather archive --fires 2022250092,2022250084
    docker compose run --rm api python -m scripts.fire_spread.evaluate --modes tuned --limit 2

Same code path as the API (``fire_spread.pipeline``) with two swaps: the weather provider
is Open-Meteo's historical forecast archive (past runs of the same high-resolution models
the live API uses; ``--weather archive`` = ERA5 reanalysis) and the ignition-year burn scar
is ignored. Each fire in ``eval_fires.json`` gives the ignition time and the free-burning
horizon; the DARP perimeter (``data/fire_spread/raw/incendis``) is the observation. Both
modes see the same fires, weather, ignition points and seeds.

Outputs, under ``data/fire_spread/hindcast/eval_<timestamp>/``: ``<mode>.csv`` (per fire:
Jaccard, Sørensen, area bias, recall/precision, ELMFIRE/prep/wall seconds), ``summary.json``
and ``summary.md`` (paired per-fire table, per-mode aggregates, timing). Timing is reported
per mode even though both modes do the same work - it flags regressions when a knob makes
ELMFIRE take smaller time steps.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from fire_spread.models import PipelineError
from fire_spread.modes import MODES
from fire_spread.pipeline import Pipeline

from . import hindcast as hc

FIRE_SET = Path(__file__).with_name("eval_fires.json")
OUT_ROOT = hc.OUT  # data/fire_spread/hindcast

METRICS = ("jaccard", "sorensen", "bias", "recall", "precision")
TIMINGS = ("wall_s", "elmfire_s", "prep_s")


# --- fire set --------------------------------------------------------------------------


def load_fire_set(path: Path = FIRE_SET) -> list[dict]:
    """Parsed, validated entries of ``eval_fires.json``: code, name, start (aware UTC
    datetime), hours, optional ignition (lat, lon), notes."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    out = []
    for i, f in enumerate(doc.get("fires", [])):
        try:
            code, hours = str(f["code"]), int(f["hours"])
            start = datetime.fromisoformat(str(f["start_utc"]).replace("Z", "+00:00"))
        except (KeyError, ValueError, TypeError) as e:
            raise ValueError(f"eval_fires.json entry {i}: {e}") from e
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if not 1 <= hours <= 48:
            raise ValueError(f"eval_fires.json {code}: hours must be 1-48 (the API's range)")
        ign = f.get("ignition")
        if ign is not None:
            lat, lon = float(ign[0]), float(ign[1])
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError(f"eval_fires.json {code}: ignition must be [lat, lon]")
            ign = (lat, lon)
        out.append({"code": code, "name": str(f.get("name", code)), "start": start.astimezone(timezone.utc),
                    "hours": hours, "ignition": ign, "notes": str(f.get("notes", ""))})
    if not out:
        raise ValueError(f"no fires in {path}")
    return out


def match_perimeters(fire_set: list[dict], perimeters: list[dict]) -> list[tuple[dict, dict]]:
    """Pair each evaluation entry with its DARP perimeter record (by CODI_FINAL)."""
    by_code = {p["code"]: p for p in perimeters}
    missing = [f["code"] for f in fire_set if f["code"] not in by_code]
    if missing:
        raise SystemExit(f"DARP perimeters not found for {missing} (run prepare_static_data.py --steps burns)")
    return [(f, by_code[f["code"]]) for f in fire_set]


# --- running -----------------------------------------------------------------------------


async def run_mode(mode: str, cases: list[tuple[dict, dict]], weather: str, members: int, pmin: float,
                   spotting: bool = False, log=print) -> list[dict]:
    """All fires through one mode; a fire that fails (weather gap, no burnable candidate)
    is reported and skipped so the other mode is still scored on the same set."""
    provider = hc.weather_provider(weather)
    pipe = Pipeline(settings=hc.build_settings(mode, spotting_default=spotting), weather_provider=provider)
    rows = []
    for i, (spec, fire) in enumerate(cases, 1):
        log(f"[{mode} {i}/{len(cases)}] {spec['name']} {spec['start'].isoformat()} {spec['hours']} h ...", end=" ", flush=True)
        try:
            r = await hc.run_case(pipe, provider, fire, spec["hours"], members, spotting, pmin,
                                  start=spec["start"], ignition=spec["ignition"])
        except PipelineError as e:
            log(f"skipped ({e})")
            rows.append({"code": spec["code"], "date": spec["start"].date().isoformat(), "municipality": fire["municipality"],
                         "mode": mode, "start_utc": spec["start"].isoformat(), "hours": spec["hours"], "error": str(e)})
            continue
        r["name"] = spec["name"]
        rows.append(r)
        log(f"pred {r['pred_ha']:.0f} / obs {r['obs_ha']:.0f} ha  J {r['jaccard']:.2f}  bias {r['bias']:.2f}  "
            f"{r['elmfire_s']:.0f}s elmfire / {r['wall_s']:.0f}s")
    return rows


# --- reporting (pure) ----------------------------------------------------------------------


def _agg(values: list[float]) -> dict:
    a = np.array(values, dtype=float)
    if a.size == 0:
        return {"n": 0}
    return {"n": int(a.size), "mean": round(float(a.mean()), 3), "median": round(float(np.median(a)), 3),
            "p25": round(float(np.percentile(a, 25)), 3), "p75": round(float(np.percentile(a, 75)), 3),
            "min": round(float(a.min()), 3), "max": round(float(a.max()), 3)}


def summarise(rows_by_mode: dict[str, list[dict]]) -> dict:
    """Per-mode aggregates over the fires *every* mode scored (paired comparison), the
    per-fire table and timing. Rows with an ``error`` key are listed but not scored."""
    scored = {m: {r["code"]: r for r in rows if "error" not in r} for m, rows in rows_by_mode.items()}
    common = sorted(set.intersection(*(set(s) for s in scored.values()))) if scored else []
    modes = list(rows_by_mode)
    per_mode = {}
    for m in modes:
        rs = [scored[m][c] for c in common]
        per_mode[m] = {
            "fires_scored": len(scored[m]), "fires_paired": len(rs),
            **{k: _agg([r[k] for r in rs]) for k in METRICS},
            "timing": {k: _agg([r[k] for r in rs]) for k in TIMINGS},
        }
    fires = []
    for c in common:
        first = scored[modes[0]][c]
        fires.append({"code": c, "name": first.get("name", first.get("municipality", c)), "obs_ha": first["obs_ha"],
                      "hours": first["hours"],
                      "modes": {m: {k: scored[m][c][k] for k in ("pred_ha", *METRICS, *TIMINGS)} for m in modes}})
    wins = {}
    if len(modes) == 2 and common:
        a, b = modes
        wins = {b: sum(1 for c in common if scored[b][c]["jaccard"] > scored[a][c]["jaccard"]),
                a: sum(1 for c in common if scored[a][c]["jaccard"] > scored[b][c]["jaccard"]),
                "ties": sum(1 for c in common if scored[a][c]["jaccard"] == scored[b][c]["jaccard"])}
    errors = [{"mode": m, "code": r["code"], "error": r["error"]} for m, rows in rows_by_mode.items() for r in rows if "error" in r]
    return {"modes": modes, "paired_fires": common, "per_mode": per_mode, "fires": fires, "jaccard_wins": wins, "errors": errors}


def render_markdown(summary: dict, meta: dict) -> str:
    modes = summary["modes"]
    out = [f"# Fire-spread evaluation {meta.get('timestamp', '')}", "",
           f"Modes {', '.join(modes)} · weather `{meta.get('weather')}` · {meta.get('members')} members · pmin {meta.get('pmin')}"
           f" · {len(summary['paired_fires'])} fires scored in every mode.", ""]
    out += ["## Aggregates (paired fires)", "",
            "| Mode | n | Jaccard mean / median | Sørensen mean | Area bias median (p25–p75) | Recall / precision | Wall s median (max) | ELMFIRE s median | Prep s median |",
            "|---|---|---|---|---|---|---|---|---|"]
    for m in modes:
        p = summary["per_mode"][m]
        if p["fires_paired"] == 0:
            out.append(f"| {m} | 0 | – | – | – | – | – | – | – |")
            continue
        t = p["timing"]
        out.append(f"| {m} | {p['fires_paired']} | {p['jaccard']['mean']:.3f} / {p['jaccard']['median']:.3f} | {p['sorensen']['mean']:.3f} "
                   f"| {p['bias']['median']:.2f} ({p['bias']['p25']:.2f}–{p['bias']['p75']:.2f}) | {p['recall']['mean']:.2f} / {p['precision']['mean']:.2f} "
                   f"| {t['wall_s']['median']:.0f} ({t['wall_s']['max']:.0f}) | {t['elmfire_s']['median']:.0f} | {t['prep_s']['median']:.0f} |")
    if summary["jaccard_wins"]:
        w = summary["jaccard_wins"]
        out += ["", "Jaccard wins per fire: " + ", ".join(f"{k} {v}" for k, v in w.items()) + "."]
    out += ["", "## Per fire", "",
            "| Fire | Horizon | Observed ha | " + " | ".join(f"{m}: pred ha / J / bias / wall s" for m in modes) + " |",
            "|---|---|---|" + "---|" * len(modes)]
    for f in summary["fires"]:
        cells = " | ".join(f"{f['modes'][m]['pred_ha']:.0f} / {f['modes'][m]['jaccard']:.2f} / {f['modes'][m]['bias']:.2f} / {f['modes'][m]['wall_s']:.0f}" for m in modes)
        out.append(f"| {f['name']} ({f['code']}) | {f['hours']} h | {f['obs_ha']:.0f} | {cells} |")
    if summary["errors"]:
        out += ["", "## Skipped", ""] + [f"- {e['mode']} {e['code']}: {e['error']}" for e in summary["errors"]]
    out += ["", "Reading the numbers: Jaccard = |pred ∩ obs| / |pred ∪ obs| of burn probability ≥ pmin against the final "
            "DARP perimeter; bias = predicted / observed area (> 1 over-prediction); recall low = too slow or wrong axis; "
            "precision low = burned where the real fire stopped. Ignition points/times in eval_fires.json are approximate.", ""]
    return "\n".join(out)


def write_outputs(out_dir: Path, rows_by_mode: dict[str, list[dict]], summary: dict, meta: dict) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    for m, rows in rows_by_mode.items():
        if rows:
            keys = list(dict.fromkeys(k for r in rows for k in r))
            with (out_dir / f"{m}.csv").open("w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=keys)
                w.writeheader(); w.writerows(rows)
    (out_dir / "summary.json").write_text(json.dumps({"meta": meta, **summary}, indent=2), encoding="utf-8")
    md = out_dir / "summary.md"
    md.write_text(render_markdown(summary, meta), encoding="utf-8")
    return md


# --- CLI -----------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Evaluate the base and tuned pipeline modes on historical free-burning fires")
    ap.add_argument("--modes", default=",".join(MODES), help="comma-separated subset of " + "/".join(MODES))
    ap.add_argument("--fire-set", type=Path, default=FIRE_SET)
    ap.add_argument("--fires", default="", help="comma-separated CODI_FINAL codes to restrict to")
    ap.add_argument("--limit", type=int, default=0, help="first N fires of the set only (0 = all)")
    ap.add_argument("--members", type=int, default=4)
    ap.add_argument("--pmin", type=float, default=0.5, help="burn probability counted as burned")
    ap.add_argument("--weather", choices=("historical", "archive"), default="historical")
    ap.add_argument("--spotting", action="store_true")
    ap.add_argument("--out", type=Path, default=None, help="output directory (default data/fire_spread/hindcast/eval_<ts>)")
    a = ap.parse_args(argv)

    modes = [m.strip() for m in a.modes.split(",") if m.strip()]
    bad = [m for m in modes if m not in MODES]
    if bad or not modes:
        raise SystemExit(f"unknown modes {bad}; choose from {MODES}")
    fire_set = load_fire_set(a.fire_set)
    if a.fires:
        want = set(a.fires.split(","))
        fire_set = [f for f in fire_set if f["code"] in want]
    if a.limit:
        fire_set = fire_set[: a.limit]
    if not fire_set:
        raise SystemExit("no fires selected")
    years = range(min(f["start"].year for f in fire_set), max(f["start"].year for f in fire_set) + 1)
    cases = match_perimeters(fire_set, hc.load_perimeters(years))

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M")
    out_dir = a.out or (OUT_ROOT / f"eval_{ts}")
    meta = {"timestamp": ts, "weather": a.weather, "members": a.members, "pmin": a.pmin, "spotting": a.spotting,
            "fire_set": str(a.fire_set), "modes": modes}
    rows_by_mode: dict[str, list[dict]] = {}
    t0 = time.perf_counter()
    for m in modes:
        rows_by_mode[m] = asyncio.run(run_mode(m, cases, a.weather, a.members, a.pmin, a.spotting))
        write_outputs(out_dir, rows_by_mode, summarise(rows_by_mode), meta)  # partial results survive a crash
    meta["total_s"] = round(time.perf_counter() - t0, 1)
    summary = summarise(rows_by_mode)
    md = write_outputs(out_dir, rows_by_mode, summary, meta)
    print("\n" + md.read_text(encoding="utf-8"))
    print(f"results: {out_dir}")
    return 0 if summary["paired_fires"] else 1


if __name__ == "__main__":
    sys.exit(main())
