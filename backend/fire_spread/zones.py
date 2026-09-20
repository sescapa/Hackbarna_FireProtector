"""Fire-regime zone lookup (Bombers/Interior ZHR 2014) — metadata only.

``prepare_static_data.py`` writes ``zhr.geojson`` (EPSG:4326) next to the rasters. ZHR2014
attributes: ``ZHR`` (zone id), ``Hectares``, ``TIPUS_<type>`` (hectares burnable under each
*incendi tipus* — C1..C3 convective, T1..T3 topographic, TE1 topographic-wind, V1..V3 wind,
0 = none), ``PERILL_IND`` / ``PERILL_EXP`` (hazard indices) and the ``PREVIT*``/``DISID*``
design-fire flags.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import shapely
from shapely.geometry import shape

FIRE_TYPE_LABELS = {
    "C": "convectiu", "T": "topogràfic", "TE": "topogràfic-vent", "V": "vent",
}


@lru_cache(maxsize=4)
def _load(path: str) -> list[tuple[shapely.Geometry, dict]]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    return [(shape(f["geometry"]), f.get("properties") or {}) for f in doc.get("features", [])]


def zone_info(data_dir: Path, lat: float, lon: float) -> dict:
    """ZoneInfo dict for the zone containing (lat, lon), or {} when unknown."""
    path = Path(data_dir) / "zhr.geojson"
    if not path.exists():
        return {}
    pt = shapely.Point(lon, lat)
    for geom, props in _load(str(path)):
        if geom.contains(pt):
            return describe_zone(props)
    return {}


def describe_zone(props: dict) -> dict:
    """Summarise ZHR attributes: id as name, dominant fire type, share of each design fire."""
    zid = props.get("ZHR") or props.get("NOM") or props.get("NAME")
    tipus = {
        k[len("TIPUS_"):]: float(v)
        for k, v in props.items()
        if k.upper().startswith("TIPUS_") and k.upper() != "TIPUS_0" and isinstance(v, (int, float)) and v > 0
    }
    total = sum(tipus.values())
    design = {k: round(v / total, 3) for k, v in sorted(tipus.items(), key=lambda kv: -kv[1])} if total else {}
    dominant = None
    if design:
        code = next(iter(design))
        family = code.rstrip("0123456789")
        dominant = f"{code} ({FIRE_TYPE_LABELS.get(family, family)})"
    return {
        "name": None if zid is None else f"ZHR {zid}",
        "dominantFireType": dominant,
        "designFires": design,
        "properties": props,
    }
