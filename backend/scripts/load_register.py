#!/usr/bin/env python
"""Load the asset register into Postgres from inside the API container.

The container-side twin of ``scripts/setup_db.sh``: same sources, same extractors, same
resume bookkeeping (``protection.load_log``), same staging-table load with ``HEADER MATCH``
-- but nothing runs on the host except Docker, so it works the same on Linux, macOS and
Windows. ``scripts/setup_db_container.sh`` drives it; by hand, from ``backend/``:

    docker compose run --rm api python -m scripts.load_register                # everything pending
    docker compose run --rm api python -m scripts.load_register --limit 10     # 10 smallest pending
    docker compose run --rm api python -m scripts.load_register --only olot,beuda
    docker compose run --rm api python -m scripts.load_register --forests-only

Per municipality the INSPIRE GML is streamed from datacloud.ide.cat to a temporary file,
parsed by ``extract_buildings.py`` into a CSV, and COPYed through a staging table into
``protection.asset_specs`` in one transaction with its ``load_log`` row, so a municipality
is either fully loaded and logged or not logged at all. Already-logged municipalities are
skipped, so an interrupted run resumes. The forests are replaced wholesale.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import psycopg

HERE = Path(__file__).resolve().parent.parent  # /srv in the container, backend/ on the host
BASE_URL = "https://datacloud.ide.cat/geodades/inspire-edificis"
FOREST_URL = ("https://geoserveis.ide.cat/servei/catalunya/inspire/ogc/features/collections/"
              "inspire:AM.ForestManagementArea/items?f=application%2Fgeo%2Bjson&limit=5000")
SLUG_MAP = HERE / "data" / "municipality_slug_map.csv"
EXTRACTOR = HERE / "extract_buildings.py"
FOREST_EXTRACTOR = HERE / "extract_forests.py"
SCHEMA_DIR = HERE / "db" / "init"
SLUG_RE = re.compile(r"^[a-z0-9-]+$")
LISTING_RE = re.compile(r'>\s*(\d+)\s*<A HREF="[^"]*?/inspire-edificis-([^"/]+?)-etrs89-geo\.gml"')

STAGE_SQL = """
CREATE TEMP TABLE stage (
    source_id       text,
    name            text,
    asset_type      text,
    latitude        double precision,
    longitude       double precision,
    municipality_id text
) ON COMMIT DROP;
"""
INSERT_SQL = """
INSERT INTO protection.asset_specs
    (source_id, name, asset_type, latitude, longitude, municipality_id)
SELECT source_id, name, asset_type, latitude, longitude, municipality_id FROM stage
ON CONFLICT (source_id) DO NOTHING
"""
LOG_SQL = """
INSERT INTO protection.load_log (slug, n_rows, gml_bytes)
VALUES (%(slug)s, (SELECT count(*) FROM stage), %(bytes)s)
ON CONFLICT (slug) DO UPDATE
    SET n_rows = EXCLUDED.n_rows, gml_bytes = EXCLUDED.gml_bytes, loaded_at = now()
"""


def info(msg: str) -> None:
    print(f"==> {msg}", flush=True)


def warn(msg: str) -> None:
    print(f" warn {msg}", file=sys.stderr, flush=True)


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL is not set (docker compose sets it for the api service)")
    return url


def apply_schema(conn: psycopg.Connection) -> None:
    for f in sorted(SCHEMA_DIR.glob("*.sql")):
        conn.execute(f.read_text(encoding="utf-8"))
    conn.commit()


def parse_listing(html: str) -> dict[str, int]:
    """slug -> GML bytes from the datacloud directory listing."""
    return {slug: int(size) for size, slug in LISTING_RE.findall(html)}


def select_pending(sizes: dict[str, int], known: set[str], loaded: set[str], only: str = "", limit: int = 0,
                   log=warn) -> list[tuple[str, int]]:
    """(slug, gml_bytes) still to load, smallest first - the same selection as setup_db.sh:
    listed, in the slug map (extract_buildings.py needs the INE code), not yet in load_log."""
    pending = sorted(set(sizes) & known - loaded, key=lambda s: sizes[s])
    if only:
        wanted = {s.strip() for s in only.split(",") if s.strip()}
        unmatched = wanted - set(sizes)
        if unmatched:
            log(f"--only named unknown slug(s): {', '.join(sorted(unmatched))}")
        pending = [s for s in pending if s in wanted]
    if limit > 0:
        pending = pending[:limit]
    unknown = sorted(set(sizes) - known)
    if unknown:
        log(f"{len(unknown)} file(s) have no entry in the slug map and were skipped: {', '.join(unknown[:5])}")
    return [(s, sizes[s]) for s in pending]


def pending_municipalities(conn: psycopg.Connection, client: httpx.Client, only: str, limit: int) -> tuple[list[tuple[str, int]], int]:
    sizes = parse_listing(client.get(f"{BASE_URL}/", timeout=120).raise_for_status().text)
    with SLUG_MAP.open(encoding="utf-8") as fh:
        known = {r["slug"] for r in csv.DictReader(fh)}
    loaded = {row[0] for row in conn.execute("SELECT slug FROM protection.load_log")}
    return select_pending(sizes, known, loaded, only, limit), len(loaded)


def stream_to_file(client: httpx.Client, url: str, dst: Path, timeout: float) -> None:
    with client.stream("GET", url, timeout=timeout) as r, dst.open("wb") as out:
        r.raise_for_status()
        for chunk in r.iter_bytes(1 << 16):
            out.write(chunk)


def extract(script: Path, src: Path, dst: Path) -> None:
    """Run an extractor (a plain script reading a path, writing CSV to stdout)."""
    with dst.open("wb") as out:
        res = subprocess.run([sys.executable, str(script), str(src)], stdout=out, stderr=subprocess.PIPE)
    if res.returncode != 0:
        lines = res.stderr.decode(errors="replace").strip().splitlines()
        raise RuntimeError(lines[-1] if lines else "extractor failed")


def copy_csv(cur: psycopg.Cursor, sql: str, csv_path: Path) -> None:
    with cur.copy(sql) as cp, csv_path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            cp.write(chunk)


def load_one(slug: str, nbytes: int, dsn: str, tmp_dir: Path) -> tuple[str, int | None, str]:
    """Returns (slug, rows loaded or None on failure, message)."""
    if not SLUG_RE.match(slug):
        return slug, None, "suspicious slug"
    gml, csv_path = tmp_dir / f"{slug}.gml", tmp_dir / f"{slug}.csv"
    try:
        with httpx.Client(follow_redirects=True) as client:
            for attempt in range(3):
                try:
                    stream_to_file(client, f"{BASE_URL}/inspire-edificis-{slug}-etrs89-geo.gml", gml, timeout=3600)
                    break
                except httpx.HTTPError as e:
                    if attempt == 2:
                        raise
                    time.sleep(2)
        extract(EXTRACTOR, gml, csv_path)
        with psycopg.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute(STAGE_SQL)
            copy_csv(cur, "COPY stage FROM STDIN WITH (FORMAT csv, HEADER MATCH)", csv_path)
            cur.execute(INSERT_SQL)
            cur.execute(LOG_SQL, {"slug": slug, "bytes": nbytes})
            rows = cur.execute("SELECT n_rows FROM protection.load_log WHERE slug = %s", (slug,)).fetchone()[0]
            conn.commit()
        return slug, int(rows), ""
    except Exception as e:  # one municipality must not stop the batch
        return slug, None, str(e)[:200]
    finally:
        gml.unlink(missing_ok=True)
        csv_path.unlink(missing_ok=True)


def load_forests(dsn: str, tmp_dir: Path) -> int:
    info("Loading the public forests of Catalonia")
    geojson, csv_path = tmp_dir / "forests.geojson", tmp_dir / "forest_areas.csv"
    try:
        with httpx.Client(follow_redirects=True) as client:
            stream_to_file(client, FOREST_URL, geojson, timeout=600)
        extract(FOREST_EXTRACTOR, geojson, csv_path)
        with psycopg.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM protection.forest_areas")
            copy_csv(cur, "COPY protection.forest_areas FROM STDIN WITH (FORMAT csv, HEADER MATCH)", csv_path)
            n = cur.execute("SELECT count(*) FROM protection.forest_areas").fetchone()[0]
            conn.commit()
        print(f"    {n} forests loaded", flush=True)
        return int(n)
    finally:
        geojson.unlink(missing_ok=True)
        csv_path.unlink(missing_ok=True)


def summary(conn: psycopg.Connection) -> None:
    assets = conn.execute("SELECT count(*) FROM protection.asset_specs").fetchone()[0]
    munis = conn.execute("SELECT count(*) FROM protection.load_log").fetchone()[0]
    forests, area = conn.execute("SELECT count(*), coalesce(sum(area_ha), 0) FROM protection.forest_areas").fetchone()
    info("Database summary")
    print(f"    assets         {assets:,}\n    municipalities {munis:,}\n    forests        {forests:,}\n    forest area    {float(area):,.0f} ha", flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Load the FireProtector asset register (container-side setup_db)")
    ap.add_argument("--limit", type=int, default=0, help="only the N smallest pending municipalities")
    ap.add_argument("--only", default="", help="comma-separated municipality slugs")
    ap.add_argument("--workers", type=int, default=int(os.environ.get("WORKERS", 4)))
    ap.add_argument("--no-forests", action="store_true")
    ap.add_argument("--forests-only", action="store_true")
    a = ap.parse_args(argv)
    if a.no_forests and a.forests_only:
        ap.error("--no-forests and --forests-only leave nothing to load")
    for p in (SLUG_MAP, EXTRACTOR, FOREST_EXTRACTOR):
        if not p.exists():
            sys.exit(f"missing {p} (is the image built from the current tree?)")
    dsn = database_url()
    failed: list[str] = []
    with tempfile.TemporaryDirectory(prefix="register-") as td:
        tmp_dir = Path(td)
        with psycopg.connect(dsn) as conn:
            info("Applying schema (protection.asset_specs, protection.forest_areas)")
            apply_schema(conn)
            if not a.forests_only:
                info("Fetching the municipality list from datacloud.ide.cat")
                with httpx.Client(follow_redirects=True) as client:
                    work, already = pending_municipalities(conn, client, a.only, a.limit)
        if a.forests_only:
            info("Skipping the building register (--forests-only)")
        elif not work:
            info(f"Nothing to load -- all {already} municipalities are already in the database.")
        else:
            info(f"Loading {len(work)} municipalities ({already} already done), {a.workers} at a time")
            t0 = time.time()
            with ThreadPoolExecutor(max_workers=max(1, a.workers)) as pool:
                for i, (slug, rows, msg) in enumerate(pool.map(lambda w: load_one(w[0], w[1], dsn, tmp_dir), work), 1):
                    if rows is None:
                        warn(f"{slug}: {msg}")
                        failed.append(slug)
                    else:
                        print(f"[{i:4d}/{len(work):4d}] {slug:<34} {rows:9,d} buildings", flush=True)
            dt = int(time.time() - t0)
            info(f"Finished in {dt // 60}m {dt % 60}s")
        forests_failed = False
        if a.no_forests:
            info("Skipping the forests (--no-forests)")
        else:
            try:
                load_forests(dsn, tmp_dir)
            except Exception as e:
                warn(f"forests: {str(e)[:200]}")
                forests_failed = True
        with psycopg.connect(dsn) as conn:
            summary(conn)
    if failed:
        warn(f"{len(failed)} municipalities failed: {','.join(failed)[:200]}")
        warn("Re-run to retry just those.")
    if forests_failed:
        warn("the forests were not loaded; any rows already in the table are untouched. Retry with --forests-only.")
    return 1 if failed or forests_failed else 0


if __name__ == "__main__":
    sys.exit(main())
