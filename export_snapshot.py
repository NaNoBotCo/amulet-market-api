#!/usr/bin/env python3
"""export_snapshot — carve the Lazada commerce slice out of the manuscript
catalog into a *self-contained* market.db that the read API serves.

Why a snapshot and not a live pointer at the manuscript DB:
  * The API must never touch the research corpus or the fragile crawl store
    (external drive, WAL locks during crawls). A snapshot decouples them.
  * A commercial app wants a small, stable, indexed dataset it fully owns.

What it does, re-runnably (safe to run any time the crawl has refreshed):
  1. Reads method='commerce' rows + their search-term tags from the source
     catalog.db (read-only, never written).
  2. Writes a clean, indexed market.db here: items + item_terms + meta.
  3. Copies any locally-archived product images (content-addressed in the
     crawl store) into ./images/<id>.jpg so the API can serve real bytes —
     hotlinked Lazada CDN urls rot and get anti-bot'd.

Usage:
    python3 export_snapshot.py                 # default source path
    python3 export_snapshot.py --source /path/to/catalog.db
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# The source catalog lives in the sibling manuscript-crawler project. This is a
# READ-ONLY input; nothing here ever writes back to it.
DEFAULT_SOURCE = (HERE.parent / "manuscript-crawler" / "crawler" / "catalog.db")
DEFAULT_STORE = (HERE.parent / "manuscript-crawler" / "crawler" / "store")
OUT_DB = HERE / "market.db"
IMG_DIR = HERE / "images"

_SOLD_RE = re.compile(r"([\d.,]+\s*[KkMm]?)\s*(?:sold|ขายแล้ว)", re.I)

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE items (
    id                INTEGER PRIMARY KEY,   -- stable id, inherited from source catalog
    source_identifier TEXT NOT NULL,          -- Lazada product id
    title             TEXT,
    price_value       REAL,
    price_currency    TEXT,
    province          TEXT,                   -- seller province (TH), else NULL
    sold_text         TEXT,                   -- "1.4K sold" as displayed, else NULL
    market            TEXT,                   -- th | sg
    source_url        TEXT,                   -- deep link back to the listing
    image_url         TEXT,                   -- hotlinked CDN url (may rot)
    image_file        TEXT,                   -- local filename in images/ if archived, else NULL
    first_seen        TEXT,
    last_seen         TEXT,
    raw_metadata      TEXT
);

CREATE TABLE item_terms (
    item_id INTEGER NOT NULL REFERENCES items(id),
    term    TEXT NOT NULL
);

CREATE INDEX idx_items_price    ON items(price_value);
CREATE INDEX idx_items_province ON items(province);
CREATE INDEX idx_items_market   ON items(market);
CREATE INDEX idx_terms_term     ON item_terms(term);
CREATE INDEX idx_terms_item     ON item_terms(item_id);
"""


def _sold_from_meta(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        meta = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if meta.get("sold"):
        return str(meta["sold"])
    for line in meta.get("lines", []) or []:
        m = _SOLD_RE.search(line)
        if m:
            return m.group(0).strip()
    return None


def build(source_db: Path, store_dir: Path) -> dict:
    if not source_db.exists():
        sys.exit(f"source catalog not found: {source_db}")

    # Read-only connection to the research catalog — immutable mode so a live
    # crawl holding a write lock can't block us and we can never corrupt it.
    src = sqlite3.connect(f"file:{source_db}?mode=ro&immutable=1", uri=True)
    src.row_factory = sqlite3.Row

    rows = src.execute(
        "SELECT id, source_identifier, title_thai, price_value, price_currency, "
        "       location_text, source_url, image_url, image_sha256, "
        "       first_seen, last_seen, raw_metadata "
        "FROM items WHERE method='commerce'"
    ).fetchall()

    # term tags per item
    terms_by_item: dict[int, list[str]] = {}
    for r in src.execute(
        "SELECT item_id, term_raw FROM tags "
        "WHERE method='commerce' AND item_id IS NOT NULL"
    ):
        terms_by_item.setdefault(r[0], []).append(r[1])

    src_snapshot_ts = src.execute(
        "SELECT MAX(last_seen) FROM items WHERE method='commerce'"
    ).fetchone()[0]
    src.close()

    if OUT_DB.exists():
        OUT_DB.unlink()
    out = sqlite3.connect(OUT_DB)
    out.executescript(SCHEMA)

    IMG_DIR.mkdir(exist_ok=True)
    # wipe prior copied images so a rebuild stays clean
    for f in IMG_DIR.glob("*.jpg"):
        f.unlink()

    n_items = n_terms = n_img = 0
    for r in rows:
        market = "th"
        try:
            market = (json.loads(r["raw_metadata"]) or {}).get("market", "th")
        except (ValueError, TypeError):
            pass

        image_file = None
        sha = r["image_sha256"]
        if sha:
            cand = store_dir / sha[:2] / sha
            if cand.exists():
                image_file = f"{r['id']}.jpg"
                shutil.copyfile(cand, IMG_DIR / image_file)
                n_img += 1

        out.execute(
            "INSERT INTO items (id, source_identifier, title, price_value, "
            "price_currency, province, sold_text, market, source_url, image_url, "
            "image_file, first_seen, last_seen, raw_metadata) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["id"], r["source_identifier"], r["title_thai"], r["price_value"],
             r["price_currency"], r["location_text"], _sold_from_meta(r["raw_metadata"]),
             market, r["source_url"], r["image_url"], image_file,
             r["first_seen"], r["last_seen"], r["raw_metadata"]),
        )
        n_items += 1
        for term in terms_by_item.get(r["id"], []):
            out.execute("INSERT INTO item_terms (item_id, term) VALUES (?,?)",
                        (r["id"], term))
            n_terms += 1

    meta = {
        "snapshot_source_last_seen": src_snapshot_ts or "",
        "item_count": str(n_items),
        "term_link_count": str(n_terms),
        "local_image_count": str(n_img),
        "source_db": str(source_db),
    }
    out.executemany("INSERT INTO meta (key, value) VALUES (?,?)", meta.items())
    out.commit()
    out.close()
    return {"items": n_items, "term_links": n_terms, "images_copied": n_img}


def main() -> int:
    ap = argparse.ArgumentParser(description="Export Lazada commerce snapshot")
    ap.add_argument("--source", type=Path, default=DEFAULT_SOURCE,
                    help="path to the manuscript catalog.db (read-only)")
    ap.add_argument("--store", type=Path, default=DEFAULT_STORE,
                    help="path to the crawl image store (content-addressed)")
    a = ap.parse_args()
    stats = build(a.source, a.store)
    print(f"market.db built: {stats['items']} items · {stats['term_links']} term-links · "
          f"{stats['images_copied']} images copied -> images/")
    print(f"-> {OUT_DB}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
