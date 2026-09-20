"""Evaluation execution: fire set, pairing, aggregates and the report (pure parts)."""

import json
from datetime import timezone

import pytest

from scripts.fire_spread import evaluate as ev


def _row(code, mode, jaccard, bias, wall, **kw):
    pred = 100.0 * bias
    r = {"code": code, "date": "2022-06-15", "municipality": code, "mode": mode, "start_utc": "2022-06-15T12:00:00+00:00",
         "hours": 24, "pred_ha": pred, "obs_ha": 100.0, "tp_ha": jaccard * 100, "jaccard": jaccard,
         "sorensen": round(2 * jaccard / (1 + jaccard), 3), "bias": bias, "recall": jaccard, "precision": jaccard / bias,
         "obs_in_grid_ha": 100.0, "elmfire_s": wall * 0.8, "prep_s": wall * 0.2, "wall_s": wall, "name": f"Fire {code}"}
    r.update(kw)
    return r


def test_curated_fire_set_is_valid():
    fires = ev.load_fire_set()
    assert len(fires) >= 5
    codes = [f["code"] for f in fires]
    assert len(set(codes)) == len(codes)
    for f in fires:
        assert f["start"].tzinfo == timezone.utc and 1 <= f["hours"] <= 48
        assert f["code"].isdigit() and len(f["code"]) == 10  # DARP CODI_FINAL
        assert f["notes"]


def test_load_fire_set_validation(tmp_path):
    p = tmp_path / "fires.json"
    p.write_text(json.dumps({"fires": [{"code": "2022250092", "start_utc": "2022-06-15T13:00:00Z", "hours": 30, "ignition": [41.9, 1.1]}]}))
    f = ev.load_fire_set(p)[0]
    assert f["ignition"] == (41.9, 1.1) and f["start"].hour == 13 and f["name"] == "2022250092"
    p.write_text(json.dumps({"fires": [{"code": "x", "start_utc": "2022-06-15T13:00:00Z", "hours": 72}]}))
    with pytest.raises(ValueError, match="1-48"):
        ev.load_fire_set(p)
    p.write_text(json.dumps({"fires": [{"code": "x", "start_utc": "not a date", "hours": 6}]}))
    with pytest.raises(ValueError, match="entry 0"):
        ev.load_fire_set(p)
    p.write_text(json.dumps({"fires": []}))
    with pytest.raises(ValueError, match="no fires"):
        ev.load_fire_set(p)


def test_match_perimeters():
    fires = [{"code": "1"}, {"code": "2"}]
    pairs = ev.match_perimeters(fires, [{"code": "2", "x": 1}, {"code": "1", "x": 2}])
    assert [p[1]["x"] for p in pairs] == [2, 1]
    with pytest.raises(SystemExit, match="not found for \\['3'\\]"):
        ev.match_perimeters([{"code": "3"}], [{"code": "1"}])


def test_summarise_pairs_modes_and_reports_timing():
    rows = {
        "base": [_row("a", "base", 0.20, 3.0, 60), _row("b", "base", 0.40, 1.2, 80), _row("c", "base", 0.10, 5.0, 100),
                 {"code": "d", "mode": "base", "error": "weather gap"}],
        "tuned": [_row("a", "tuned", 0.30, 2.0, 70), _row("b", "tuned", 0.35, 1.0, 90), _row("c", "tuned", 0.10, 4.0, 110),
                  _row("d", "tuned", 0.50, 1.0, 50)],
    }
    s = ev.summarise(rows)
    assert s["paired_fires"] == ["a", "b", "c"]  # d failed in base -> excluded from the paired aggregates
    assert s["per_mode"]["base"]["fires_scored"] == 3 and s["per_mode"]["tuned"]["fires_scored"] == 4
    assert s["per_mode"]["tuned"]["fires_paired"] == 3
    assert s["per_mode"]["base"]["jaccard"]["mean"] == pytest.approx(0.2333, abs=1e-3)
    assert s["per_mode"]["tuned"]["jaccard"]["median"] == 0.30
    assert s["per_mode"]["base"]["bias"]["median"] == 3.0 and s["per_mode"]["tuned"]["bias"]["p25"] == 1.5
    assert s["per_mode"]["base"]["timing"]["wall_s"] == {"n": 3, "mean": 80.0, "median": 80.0, "p25": 70.0, "p75": 90.0, "min": 60.0, "max": 100.0}
    assert s["per_mode"]["tuned"]["timing"]["elmfire_s"]["median"] == pytest.approx(72.0)
    assert s["jaccard_wins"] == {"tuned": 1, "base": 1, "ties": 1}
    assert s["errors"] == [{"mode": "base", "code": "d", "error": "weather gap"}]
    assert [f["code"] for f in s["fires"]] == ["a", "b", "c"] and s["fires"][0]["modes"]["tuned"]["pred_ha"] == 200.0

    md = ev.render_markdown(s, {"timestamp": "t", "weather": "historical", "members": 4, "pmin": 0.5})
    assert "| base | 3 | 0.233 / 0.200 |" in md and "| tuned | 3 |" in md
    assert "Jaccard wins per fire: tuned 1, base 1, ties 1." in md
    assert "| Fire a (a) | 24 h | 100 | 300 / 0.20 / 3.00 / 60 | 200 / 0.30 / 2.00 / 70 |" in md
    assert "- base d: weather gap" in md


def test_summarise_single_mode_and_empty():
    s = ev.summarise({"tuned": [_row("a", "tuned", 0.3, 1.0, 10)]})
    assert s["jaccard_wins"] == {} and s["per_mode"]["tuned"]["jaccard"]["n"] == 1
    md = ev.render_markdown(s, {})
    assert "| tuned | 1 |" in md
    s = ev.summarise({"base": [], "tuned": []})
    assert s["paired_fires"] == [] and s["per_mode"]["base"]["jaccard"] == {"n": 0}
    assert "| base | 0 | –" in ev.render_markdown(s, {})


def test_write_outputs(tmp_path):
    rows = {"base": [_row("a", "base", 0.2, 3.0, 60)], "tuned": [_row("a", "tuned", 0.3, 2.0, 70), {"code": "b", "mode": "tuned", "error": "x"}]}
    md = ev.write_outputs(tmp_path / "eval", rows, ev.summarise(rows), {"timestamp": "t"})
    assert md.exists() and (tmp_path / "eval" / "base.csv").exists()
    tuned = (tmp_path / "eval" / "tuned.csv").read_text().splitlines()
    assert tuned[0].startswith("code,date,municipality,mode") and tuned[0].endswith(",error") and len(tuned) == 3
    doc = json.loads((tmp_path / "eval" / "summary.json").read_text())
    assert doc["meta"] == {"timestamp": "t"} and doc["paired_fires"] == ["a"]
