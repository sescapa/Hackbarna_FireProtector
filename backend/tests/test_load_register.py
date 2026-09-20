"""Container-side register loader: the pure selection logic mirrors setup_db.sh."""

from scripts import load_register as lr

LISTING = """
<pre>
 12345 <A HREF="/geodades/inspire-edificis/inspire-edificis-beuda-etrs89-geo.gml">inspire-edificis-beuda-etrs89-geo.gml</A>
   999 <A HREF="/geodades/inspire-edificis/inspire-edificis-lladurs-etrs89-geo.gml">inspire-edificis-lladurs-etrs89-geo.gml</A>
 55555 <A HREF="/geodades/inspire-edificis/inspire-edificis-olot-etrs89-geo.gml">inspire-edificis-olot-etrs89-geo.gml</A>
 77777 <A HREF="/geodades/inspire-edificis/inspire-edificis-nowhere-etrs89-geo.gml">x</A>
 88888 <A HREF="/geodades/other/readme.txt">readme</A>
</pre>
"""


def test_parse_listing():
    assert lr.parse_listing(LISTING) == {"beuda": 12345, "lladurs": 999, "olot": 55555, "nowhere": 77777}


def test_select_pending_smallest_first_known_and_not_loaded():
    sizes = lr.parse_listing(LISTING)
    known = {"beuda", "lladurs", "olot"}
    notes = []
    work = lr.select_pending(sizes, known, loaded=set(), log=notes.append)
    assert work == [("lladurs", 999), ("beuda", 12345), ("olot", 55555)]
    assert notes == ["1 file(s) have no entry in the slug map and were skipped: nowhere"]
    assert lr.select_pending(sizes, known, loaded={"lladurs"}, log=notes.append) == [("beuda", 12345), ("olot", 55555)]
    assert lr.select_pending(sizes, known, loaded=set(), limit=1, log=notes.append) == [("lladurs", 999)]
    notes.clear()
    assert lr.select_pending(sizes, known, loaded=set(), only="olot, ghost", log=notes.append) == [("olot", 55555)]
    assert notes[0] == "--only named unknown slug(s): ghost"


def test_slug_guard():
    assert lr.SLUG_RE.match("sant-joan-2") and not lr.SLUG_RE.match("x'; DROP TABLE") and not lr.SLUG_RE.match("Olot")


def test_image_ships_what_the_loader_needs():
    """The Dockerfile must copy the extractors, slug map and schema the loader resolves."""
    from pathlib import Path

    backend = Path(__file__).resolve().parents[1]
    dockerfile = (backend / "Dockerfile").read_text()
    for needle in ("COPY extract_buildings.py extract_forests.py ./", "COPY data/municipality_slug_map.csv",
                   "COPY db/init ./db/init", "COPY scripts/load_register.py"):
        assert needle in dockerfile, needle
    ignore = (backend / ".dockerignore").read_text().splitlines()
    for needle in ("!db/init/", "!data/municipality_slug_map.csv", "!scripts/load_register.py"):
        assert needle in ignore, needle
    for rel in ("extract_buildings.py", "extract_forests.py", "data/municipality_slug_map.csv", "db/init/01_schema.sql"):
        assert (backend / rel).exists(), rel
