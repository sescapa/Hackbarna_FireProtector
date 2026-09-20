"""Sunrise / sunset (UTC hours) for ELMFIRE's overnight spread-rate adjustment.

Same NOAA approximation ELMFIRE uses internally (elmfire_subs.f90 SUNRISE_SUNSET_CALCS),
computed here so the values are explicit in ``elmfire.data`` and independent of the
build (older builds default SUNRISE_HOUR to a fixed 13.0 UTC).
"""

from __future__ import annotations

import math
from datetime import date


def sunrise_sunset_utc(lat: float, lon: float, day: date) -> tuple[float, float]:
    """(sunrise, sunset) as fractional UTC hours in [0, 24). Polar day/night fall back
    to (0, 24) / (12, 12)."""
    leap = day.year % 4 == 0 and (day.year % 100 != 0 or day.year % 400 == 0)
    days = 366.0 if leap else 365.0
    doy = day.timetuple().tm_yday
    g = 2.0 * math.pi / days * (doy - 1)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    lat_r = math.radians(lat)
    cos_ha = math.cos(math.radians(90.833)) / (math.cos(lat_r) * math.cos(decl)) - math.tan(lat_r) * math.tan(decl)
    if cos_ha >= 1.0:
        return 12.0, 12.0
    if cos_ha <= -1.0:
        return 0.0, 24.0
    ha = math.degrees(math.acos(cos_ha))
    rise = (720.0 - 4.0 * (lon + ha) - eqtime) / 60.0
    sset = (720.0 - 4.0 * (lon - ha) - eqtime) / 60.0
    return rise % 24.0, sset % 24.0
