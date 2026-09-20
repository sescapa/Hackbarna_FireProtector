from datetime import date

import pytest

from fire_spread.solar import sunrise_sunset_utc


def test_barcelona_summer():
    rise, sset = sunrise_sunset_utc(41.39, 2.17, date(2026, 8, 1))
    # ~06:40 / 21:00 CEST -> 04:40 / 19:00 UTC
    assert rise == pytest.approx(4.67, abs=0.1)
    assert sset == pytest.approx(19.1, abs=0.2)


def test_winter_shorter_day():
    rise, sset = sunrise_sunset_utc(41.39, 2.17, date(2026, 12, 21))
    assert 8.9 < sset - rise < 9.3


def test_polar_cases():
    assert sunrise_sunset_utc(80.0, 0.0, date(2026, 6, 21)) == (0.0, 24.0)
    assert sunrise_sunset_utc(80.0, 0.0, date(2026, 12, 21)) == (12.0, 12.0)
