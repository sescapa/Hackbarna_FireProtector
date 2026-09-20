#!/usr/bin/env python
"""One-off ETL: build the Catalonia-wide 50 m static tier for ELMFIRE.

Run inside the container (needs GDAL CLI tools), from ``backend/``:

    scripts/setup_fire_data.sh   # = docker compose run --rm api python -m scripts.fire_spread.prepare_static_data

Outputs ``data/fire_spread/catalonia/{dem,slp,asp,fbfm40,cc,ch,cbh,cbd}.tif`` (Int16 COGs on one
common grid: EPSG:25831, 50 m, x 260000-540000, y 4480000-4760000 = 5600x5600),
``zhr.gpkg`` + ``zhr.geojson`` and ``manifest.json``.

Sources are hardcoded on purpose (reproducible recipe, no fallbacks):
  * DEM      Copernicus DEM GLO-30 (AWS open data, no auth)
  * Fuel     ZAFM-DW 2026 Spain raster, Zenodo 10.5281/zenodo.21978709 (CC BY 4.0)
  * Canopy   ICGC/CREAF "Variables biofisiques de l'arbrat" v1.1 2016-17 (CC BY 4.0), fetched
             from the ICGC datacloud directory (cc, hmitjana, bf rasters).
  * Zones    Interior/Bombers ZHR 2014 shapefile (metadata only)
  * Burns    DARP "Base cartogràfica d'incendis forestals" per-year shapefiles -> burnyear.tif
             (latest burn year per cell; the pipeline remaps fuel in recently burned areas)
  * Agri     OpenStreetMap landuse -> agri.tif (1 orchard/vineyard/olive/greenhouse, 2 farmland); the
             pipeline turns ZAFM's blanket GR4 into NB3 where class 1 (permanent woody crops)
  * Barriers OpenStreetMap roads + waterways (Geofabrik Catalonia extract) -> barrier.tif, the
             width (m) of the widest linear fire break crossing each cell. ELMFIRE stops surface
             spread where width > 1.5 x flame length (USE_BARRIERS).

Steps can be re-run individually: ``--steps dem,fuel``. ``--res 30`` builds the tier at 30 m
(ELMFIRE's usual resolution; the pipeline follows whatever resolution the tier has).
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import rasterio

# Relative to the cwd (backend/ on the host, /srv in the container), like fire_spread.settings.
DATA = Path("data/fire_spread")
CRS = "EPSG:25831"
RES = 50.0
XMIN, YMIN, XMAX, YMAX = 260_000.0, 4_480_000.0, 540_000.0, 4_760_000.0
NODATA = -9999
TE = f"{XMIN:.0f} {YMIN:.0f} {XMAX:.0f} {YMAX:.0f}"

GLO30_URL = "https://copernicus-dem-30m.s3.amazonaws.com/{t}/{t}.tif"
GLO30_TILES = [f"Copernicus_DSM_COG_10_N{lat}_00_E00{lon}_00_DEM" for lat in (40, 41, 42) for lon in (0, 1, 2, 3)]
ZENODO = "https://zenodo.org/api/records/21978709/files/{name}/content"
ZAFM_TIF = "ESP_3035_ZAFM_DYNAMIC_WORLD_2026.tif"
ZAFM_LEGEND = "zafm_legend.csv"
ZAFM_TABLE = "burgan_models_table.csv"
ICGC_BASE = "https://datacloud.icgc.cat/datacloud/variables-biofisiques-arbrat/tif_unzip/"
ICGC_FILES = {  # variable -> filename (v1r1, 2016-2017 LiDAR campaign)
    "cc": "variables-biofisiques-arbrat-v1r1-cc-2016-2017.tif",
    "hm": "variables-biofisiques-arbrat-v1r1-hmitjana-2016-2017.tif",
    "bf": "variables-biofisiques-arbrat-v1r1-bf-2016-2017.tif",
}
DARP_BURNS_URL = "http://www.gencat.cat/agricultura/sig/bases/incendis{yy:02d}.zip"
BURN_YEARS = range(2012, 2025)  # 13 y of history; the pipeline only uses the last 6
ZHR_URL = "https://interior.gencat.cat/web/.content/home/serveis/bases_cartografiques/ZHR/ZHR2014.zip"
OSM_PBF_URL = "https://download.geofabrik.de/europe/spain/cataluna-latest.osm.pbf"
# Barrier width (m) by OSM class: paved width incl. verges for roads, channel width for water.
ROAD_WIDTH_M = {"motorway": 30, "trunk": 25, "primary": 14, "secondary": 10, "tertiary": 8,
                "unclassified": 6, "residential": 6, "service": 4, "track": 4}
WATER_WIDTH_M = {"river": 20, "canal": 10, "stream": 3}

COG_OPTS = ["-of", "COG", "-co", "COMPRESS=DEFLATE", "-co", "BLOCKSIZE=512", "-co", "NUM_THREADS=ALL_CPUS"]


def sh(*cmd: str) -> None:
    print("$", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def download(url: str, dest: Path, optional: bool = False) -> Path | None:
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  cached {dest.name}")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  GET {url}", flush=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=120) as r, tmp.open("wb") as f:
            shutil.copyfileobj(r, f, length=1 << 20)
    except urllib.error.HTTPError as e:
        if optional and e.code == 404:
            print(f"  (missing, skipped: {e.code})")
            return None
        raise
    tmp.rename(dest)
    return dest


def to_cog_int16(src: Path, dst: Path, nodata: int = NODATA) -> None:
    sh("gdal_translate", "-q", "-ot", "Int16", "-a_nodata", str(nodata), *COG_OPTS, str(src), str(dst))


# --- steps --------------------------------------------------------------------------


def step_zhr(raw: Path, out: Path) -> dict:
    z = download(ZHR_URL, raw / "zhr" / "ZHR2014.zip")
    ext = raw / "zhr" / "unzipped"
    if not ext.exists():
        with zipfile.ZipFile(z) as zf:
            zf.extractall(ext)
    shps = list(ext.rglob("*.shp"))
    if not shps:
        raise SystemExit("ZHR zip contains no .shp")
    shp = shps[0]
    sh("ogr2ogr", "-f", "GPKG", "-overwrite", "-t_srs", CRS, str(out / "zhr.gpkg"), str(shp))
    sh("ogr2ogr", "-f", "GeoJSON", "-overwrite", "-t_srs", "EPSG:4326", "-lco", "COORDINATE_PRECISION=6",
       str(out / "zhr.geojson"), str(shp))
    # Rasterised union = Catalonia coverage mask for all layers.
    mask = out / "mask.tif"
    sh("gdal_rasterize", "-q", "-burn", "1", "-ot", "Byte", "-a_nodata", "0", "-tr", str(RES), str(RES),
       "-te", *TE.split(), "-co", "COMPRESS=DEFLATE", "-co", "TILED=YES", str(out / "zhr.gpkg"), str(mask))
    return {"source": ZHR_URL, "shapefile": shp.name, "license": "Generalitat de Catalunya open data"}


def step_burns(raw: Path, out: Path) -> dict:
    d = raw / "incendis"
    burn = out / "burnyear.tmp.tif"
    sh("gdal_rasterize", "-q", "-burn", "0", "-ot", "Int16", "-a_nodata", "0", "-tr", str(RES), str(RES),
       "-te", *TE.split(), "-co", "COMPRESS=DEFLATE", "-co", "TILED=YES", "-init", "0",
       str(out / "zhr.gpkg"), str(burn))  # empty grid on the target extent
    years_done = []
    for year in BURN_YEARS:
        z = download(DARP_BURNS_URL.format(yy=year % 100), d / f"incendis{year}.zip", optional=True)
        if z is None:
            continue
        ext = d / str(year)
        if not ext.exists():
            with zipfile.ZipFile(z) as zf:
                zf.extractall(ext)
        shps = list(ext.rglob("*.shp"))
        if not shps:
            continue
        # Burn the year into the existing raster; ascending order so the latest fire wins.
        sh("gdal_rasterize", "-q", "-burn", str(year), str(shps[0]), str(burn))
        years_done.append(year)
    to_cog_int16(burn, out / "burnyear.tif", nodata=0)
    burn.unlink()
    return {"source": "DARP Base cartogràfica d'incendis forestals (agricultura.gencat.cat)", "years": years_done,
            "license": "Generalitat de Catalunya open data", "semantics": "latest burn year per 50 m cell, 0 = none"}


def _load_mask(out: Path) -> np.ndarray | None:
    p = out / "mask.tif"
    if not p.exists():
        return None
    with rasterio.open(p) as src:
        return src.read(1) > 0


def _apply_mask(path: Path, mask: np.ndarray | None) -> None:
    """Set cells outside the coverage mask to NODATA (rewrites the COG in place)."""
    if mask is None:
        return
    tmp = path.with_suffix(".masked.tif")
    with rasterio.open(path) as src:
        a = src.read(1)
        prof = src.profile
    a = np.where(mask, a, NODATA).astype(np.int16)
    prof.update(driver="GTiff", nodata=NODATA, compress="deflate", tiled=True)
    with rasterio.open(tmp, "w", **prof) as dst:
        dst.write(a, 1)
    to_cog_int16(tmp, path)
    tmp.unlink()


def step_dem(raw: Path, out: Path) -> dict:
    d = raw / "dem"
    # All-sea tiles (e.g. N40_E001) do not exist in the dataset.
    tiles = [p for t in GLO30_TILES if (p := download(GLO30_URL.format(t=t), d / f"{t}.tif", optional=True))]
    vrt = d / "glo30.vrt"
    sh("gdalbuildvrt", "-q", str(vrt), *map(str, tiles))
    dem_f = d / "dem_25831_f32.tif"
    sh("gdalwarp", "-q", "-overwrite", "-t_srs", CRS, "-te", *TE.split(), "-tr", str(RES), str(RES),
       "-r", "bilinear", "-ot", "Float32", "-dstnodata", str(NODATA), "-multi", "-wo", "NUM_THREADS=ALL_CPUS",
       "-co", "COMPRESS=DEFLATE", "-co", "TILED=YES", str(vrt), str(dem_f))
    slp_f, asp_f = d / "slp_f32.tif", d / "asp_f32.tif"
    sh("gdaldem", "slope", "-q", "-s", "1.0", "-compute_edges", "-co", "COMPRESS=DEFLATE", str(dem_f), str(slp_f))
    sh("gdaldem", "aspect", "-q", "-zero_for_flat", "-compute_edges", "-co", "COMPRESS=DEFLATE", str(dem_f), str(asp_f))
    mask = _load_mask(out)
    for src, name in ((dem_f, "dem"), (slp_f, "slp"), (asp_f, "asp")):
        to_cog_int16(src, out / f"{name}.tif")
        _apply_mask(out / f"{name}.tif", mask)
    return {
        "source": "Copernicus DEM GLO-30 (AWS s3://copernicus-dem-30m)", "tiles": GLO30_TILES,
        "native_resolution_m": 30, "license": "Copernicus DEM licence (free use with attribution)",
        "derived": {"slp": "gdaldem slope (deg)", "asp": "gdaldem aspect -zero_for_flat (deg)"},
    }


def step_fuel(raw: Path, out: Path) -> dict:
    d = raw / "fuel"
    tif = download(ZENODO.format(name=ZAFM_TIF), d / ZAFM_TIF)
    legend = download(ZENODO.format(name=ZAFM_LEGEND), d / ZAFM_LEGEND)
    download(ZENODO.format(name=ZAFM_TABLE), d / ZAFM_TABLE)
    codes = {int(r["value"]) for r in csv.DictReader(legend.open(encoding="utf-8")) if r["value"].isdigit()}
    bad = {c for c in codes if c != 0 and not (91 <= c <= 99 or 101 <= c <= 204)}
    if bad:  # the legend is already Scott & Burgan / ELMFIRE codes; refuse anything else
        raise SystemExit(f"unexpected fuel codes in {ZAFM_LEGEND}: {sorted(bad)} - add a remap")
    warped = d / "fbfm40_25831.tif"
    sh("gdalwarp", "-q", "-overwrite", "-t_srs", CRS, "-te", *TE.split(), "-tr", str(RES), str(RES),
       "-r", "mode", "-ot", "Int16", "-srcnodata", "0", "-dstnodata", str(NODATA), "-multi",
       "-wo", "NUM_THREADS=ALL_CPUS", "-co", "COMPRESS=DEFLATE", "-co", "TILED=YES", str(tif), str(warped))
    mask = _load_mask(out)
    with rasterio.open(warped) as src:
        a = src.read(1)
        prof = src.profile
    if mask is not None:
        a = np.where(mask & (a == NODATA), 99, a)  # inside Catalonia but no fuel -> NB9
        a = np.where(mask, a, NODATA)
    tmp = d / "fbfm40_masked.tif"
    prof.update(nodata=NODATA, compress="deflate", tiled=True)
    with rasterio.open(tmp, "w", **prof) as dst:
        dst.write(a.astype(np.int16), 1)
    to_cog_int16(tmp, out / "fbfm40.tif")
    vals, counts = np.unique(a[a != NODATA], return_counts=True)
    return {
        "source": "ZAFM-DW 2026 Spain (Zenodo 10.5281/zenodo.21978709)", "file": ZAFM_TIF,
        "native_resolution_m": 10, "license": "CC BY 4.0", "resampling": "mode",
        "code_scheme": "Scott & Burgan FBFM40 (identity; nodata inside coverage -> 99)",
        "histogram": {int(v): int(c) for v, c in zip(vals, counts)},
    }


def step_canopy(raw: Path, out: Path) -> dict:
    d = raw / "icgc_biofisiques"
    fcc = download(ICGC_BASE + ICGC_FILES["cc"], d / ICGC_FILES["cc"])
    hm = download(ICGC_BASE + ICGC_FILES["hm"], d / ICGC_FILES["hm"])
    bf = download(ICGC_BASE + ICGC_FILES["bf"], d / ICGC_FILES["bf"])
    with rasterio.open(out / "fbfm40.tif") as src:
        fbfm = src.read(1)
        prof = src.profile
    prof.update(driver="GTiff", dtype="int16", nodata=NODATA, compress="deflate", tiled=True, count=1)

    def warp(src: Path, name: str, resampling: str = "average") -> np.ndarray:
        dst = raw / "icgc_biofisiques" / f"{name}_25831_50m.tif"
        sh("gdalwarp", "-q", "-overwrite", "-t_srs", CRS, "-te", *TE.split(), "-tr", str(RES), str(RES),
           "-r", resampling, "-ot", "Float32", "-dstnodata", "-9999", "-co", "COMPRESS=DEFLATE", str(src), str(dst))
        with rasterio.open(dst) as s:
            a = s.read(1).astype(np.float32)
        a[a <= -9999] = 0.0
        return a

    fcc_a, hm_a, bf_a = warp(fcc, "fcc"), warp(hm, "hm"), warp(bf, "bf")
    forest = (fbfm >= 141) & (fbfm <= 189)  # SH*, TU*, TL* : canopy only matters there
    cc = np.clip(fcc_a, 0, 100)
    ch_m = np.clip(hm_a, 0, 60)
    cbh_m = np.where(ch_m > 0, np.maximum(0.4 * ch_m, 0.3), 0.0)
    depth = np.maximum(ch_m - cbh_m, 0.5)
    bf_kg_m2 = bf_a * 0.1  # t/ha -> kg/m2
    cbd = np.where(ch_m > 0, np.clip(bf_kg_m2 / depth, 0.01, 0.40), 0.0)
    for name, arr in (
        ("cc", cc), ("ch", ch_m * 10.0), ("cbh", cbh_m * 10.0), ("cbd", cbd * 100.0),
    ):
        _write_layer(out, name, np.where(forest, np.round(arr), 0), fbfm, prof)
    return {
        "source": "ICGC/CREAF Variables biofisiques de l'arbrat v1.1 (2016-17), 20 m", "license": "CC BY 4.0",
        "url": ICGC_BASE, "files": {"cc": fcc.name, "hm": hm.name, "bf": bf.name},
        "derived": {"cc": "FCC %", "ch": "HM x10 m", "cbh": "max(0.4*HM, 0.3) x10 m",
                    "cbd": "BF(t/ha->kg/m2)/(HM-CBH) clamped 0.01-0.40 x100 kg/m3", "applied_where": "fbfm40 141-189"},
    }


def step_barriers(raw: Path, out: Path) -> dict:
    pbf = download(OSM_PBF_URL, raw / "osm" / "cataluna-latest.osm.pbf")
    lines = raw / "osm" / "barriers.gpkg"
    road_case = " ".join(f"WHEN '{k}' THEN {v}" for k, v in ROAD_WIDTH_M.items())
    water_case = " ".join(f"WHEN '{k}' THEN {v}" for k, v in WATER_WIDTH_M.items())
    road_in = ",".join(f"'{k}'" for k in ROAD_WIDTH_M)
    water_in = ",".join(f"'{k}'" for k in WATER_WIDTH_M)
    # Widest feature last so it wins where several cross one cell (gdal_rasterize overwrites).
    sql = (f"SELECT geometry, CASE WHEN highway IN ({road_in}) THEN CASE highway {road_case} END "
           f"ELSE CASE waterway {water_case} END END AS width FROM lines "
           f"WHERE highway IN ({road_in}) OR waterway IN ({water_in}) ORDER BY width")
    sh("ogr2ogr", "-f", "GPKG", "-overwrite", "-t_srs", CRS, "-dialect", "SQLITE", "-sql", sql,
       "-nln", "barriers", str(lines), str(pbf))
    tmp = out / "barrier.tmp.tif"
    sh("gdal_rasterize", "-q", "-a", "width", "-ot", "Float32", "-a_nodata", "0", "-init", "0",
       "-tr", str(RES), str(RES), "-te", *TE.split(), "-co", "COMPRESS=DEFLATE", "-co", "TILED=YES",
       "-l", "barriers", str(lines), str(tmp))
    sh("gdal_translate", "-q", *COG_OPTS, str(tmp), str(out / "barrier.tif"))
    tmp.unlink()
    return {"source": OSM_PBF_URL, "license": "ODbL", "semantics": "width (m) of the widest OSM road/waterway per cell",
            "road_width_m": ROAD_WIDTH_M, "water_width_m": WATER_WIDTH_M}


def step_agri(raw: Path, out: Path) -> dict:
    pbf = download(OSM_PBF_URL, raw / "osm" / "cataluna-latest.osm.pbf")
    gpkg = raw / "osm" / "agri.gpkg"
    woody = "'orchard','vineyard','greenhouse_horticulture','plant_nursery'"
    # class 2 first, class 1 last (rasterize overwrites) so woody crops win where polygons overlap
    sql = (f"SELECT geometry, CASE WHEN landuse IN ({woody}) THEN 1 ELSE 2 END AS cls FROM multipolygons "
           f"WHERE landuse IN ({woody},'farmland','farmyard') ORDER BY cls DESC")
    sh("ogr2ogr", "-f", "GPKG", "-overwrite", "-t_srs", CRS, "-dialect", "SQLITE", "-sql", sql, "-nln", "agri",
       str(gpkg), str(pbf))
    tmp = out / "agri.tmp.tif"
    sh("gdal_rasterize", "-q", "-a", "cls", "-ot", "Byte", "-a_nodata", "0", "-init", "0", "-tr", str(RES), str(RES),
       "-te", *TE.split(), "-co", "COMPRESS=DEFLATE", "-co", "TILED=YES", "-l", "agri", str(gpkg), str(tmp))
    sh("gdal_translate", "-q", *COG_OPTS, str(tmp), str(out / "agri.tif"))
    tmp.unlink()
    return {"source": OSM_PBF_URL, "license": "ODbL",
            "classes": {"1": "orchard/vineyard/greenhouse/nursery -> NB3", "2": "farmland/farmyard -> GR4"}}


def _write_layer(out: Path, name: str, arr: np.ndarray, fbfm: np.ndarray, prof: dict) -> None:
    a = np.where(fbfm == NODATA, NODATA, arr).astype(np.int16)
    tmp = out / f"{name}.tmp.tif"
    with rasterio.open(tmp, "w", **prof) as dst:
        dst.write(a, 1)
    to_cog_int16(tmp, out / f"{name}.tif")
    tmp.unlink()


def step_manifest(out: Path, sources: dict) -> None:
    layers = {}
    for name in ("dem", "slp", "asp", "fbfm40", "cc", "ch", "cbh", "cbd"):
        p = out / f"{name}.tif"
        if p.exists():
            with rasterio.open(p) as src:
                a = src.read(1)
                valid = a[a != NODATA]
                layers[name] = {
                    "min": int(valid.min()) if valid.size else None,
                    "max": int(valid.max()) if valid.size else None,
                    "coverage_fraction": round(float(valid.size / a.size), 4),
                }
    prev = json.loads((out / "manifest.json").read_text()) if (out / "manifest.json").exists() else {}
    manifest = {
        "grid": {"crs": CRS, "cellsize_m": RES, "xmin": XMIN, "ymin": YMIN, "xmax": XMAX, "ymax": YMAX,
                 "width": int((XMAX - XMIN) / RES), "height": int((YMAX - YMIN) / RES), "nodata": NODATA},
        "units": {"dem": "m", "slp": "deg", "asp": "deg", "fbfm40": "Scott & Burgan code", "cc": "%",
                  "ch": "m x10", "cbh": "m x10", "cbd": "kg/m3 x100"},
        "generated": datetime.now(timezone.utc).isoformat(),
        "sources": {**prev.get("sources", {}), **sources},
        "layers": layers,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest["layers"], indent=1))


def main() -> int:
    global RES
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", type=Path, default=DATA / "raw")
    ap.add_argument("--out", type=Path, default=DATA / "catalonia")
    ap.add_argument("--steps", default="zhr,dem,fuel,canopy,burns,barriers,agri,manifest")
    ap.add_argument("--res", type=float, default=RES, help="cell size in m (default 50; 30 = ELMFIRE's usual)")
    a = ap.parse_args()
    RES = a.res
    a.out.mkdir(parents=True, exist_ok=True)
    a.raw.mkdir(parents=True, exist_ok=True)
    steps = [s.strip() for s in a.steps.split(",")]
    sources: dict = {}
    for step in steps:
        print(f"== {step}", flush=True)
        if step == "zhr":
            sources["zhr"] = step_zhr(a.raw, a.out)
        elif step == "dem":
            sources["dem"] = step_dem(a.raw, a.out)
        elif step == "fuel":
            sources["fbfm40"] = step_fuel(a.raw, a.out)
        elif step == "burns":
            sources["burns"] = step_burns(a.raw, a.out)
        elif step == "canopy":
            sources["canopy"] = step_canopy(a.raw, a.out)
        elif step == "barriers":
            sources["barriers"] = step_barriers(a.raw, a.out)
        elif step == "agri":
            sources["agri"] = step_agri(a.raw, a.out)
        elif step == "manifest":
            step_manifest(a.out, sources)
        else:
            print(f"unknown step {step}", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
