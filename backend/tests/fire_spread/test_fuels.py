import pytest

from fire_spread import fuels


def _rows(text):
    return {int(r.split(",")[0]): r.split(",") for r in text.strip().splitlines()}


def test_base_table_is_elmfires():
    rows = _rows(fuels.fuel_model_table("scott_burgan"))
    assert len(rows) == 56 and rows[1][1] == "FBFM01" and rows[147][1] == "SH7"
    assert rows[145][3] == "0.16529"  # untouched S&B values


def test_mediterranean_overrides_and_units():
    rows = _rows(fuels.fuel_model_table("mediterranean"))
    assert len(rows) == 56  # same codes, no new numbers
    sh5 = rows[145]
    assert sh5[1] == "SH5-med" and sh5[2] == ".FALSE."
    assert float(sh5[3]) == pytest.approx(3.0 * 0.020481, rel=1e-3)   # t/ha -> lb/ft2
    assert float(sh5[8]) == pytest.approx(55 * 30.48, abs=1)          # 1/cm -> 1/ft
    assert sh5[9] == "9999"                                            # no herbaceous class
    assert float(sh5[11]) == pytest.approx(0.9 * 3.28084, abs=1e-3)   # m -> ft
    assert sh5[12] == "25" and float(sh5[13]) == pytest.approx(19000 * 0.429923, abs=1)
    assert rows[104][2] == ".TRUE." and rows[102][1] == "GR2"          # dynamic kept; untouched codes remain
    # Mediterranean beds are lighter and shallower than the US chaparral originals
    base = _rows(fuels.fuel_model_table("scott_burgan"))
    for code in (145, 147):
        assert float(rows[code][3]) < float(base[code][3]) and float(rows[code][11]) < float(base[code][11])


def test_unknown_set():
    with pytest.raises(ValueError):
        fuels.fuel_model_table("nope")
