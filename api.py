#!/usr/bin/env python3
"""Amulet Market API — a fast, read-only JSON API over the Lazada commerce
snapshot (market.db). Zero dependencies: Python stdlib http.server only, so it
runs anywhere with `python3 api.py` and no pip install.

This is the surface a commercial app talks to. It NEVER crawls and NEVER touches
the research corpus — it only reads the self-contained snapshot built by
export_snapshot.py. Re-run that script to refresh the data; the API picks up the
new market.db on its next start.

    python3 api.py                 # http://127.0.0.1:8787
    python3 api.py --port 9000
    python3 api.py --host 0.0.0.0  # expose on LAN (prototype only — no auth)

Endpoints
    GET /                    human-readable index + live counts
    GET /health              {ok, item_count, snapshot_source_last_seen}
    GET /items               search / filter / paginate  (see params below)
    GET /items/{id}          one listing, full detail + terms
    GET /terms               search-term facet with counts
    GET /provinces           seller-province facet with counts
    GET /stats               summary: totals, price distribution, top facets
    GET /image/{id}          locally-archived product image (JPEG), if present

/items query params
    q            substring match on title (case-insensitive)
    term         exact search-term tag (e.g. ตะกรุด) — repeatable via comma
    province     exact seller province
    market       th | sg
    min_price    THB (or listing currency) lower bound, inclusive
    max_price    upper bound, inclusive
    has_image    1 = only rows with a hotlink image url
    has_photo    1 = only rows with a locally-archived image (servable via /image)
    sort         price_asc | price_desc | newest | oldest  (default: newest)
    limit        default 50, max 200
    offset       default 0
Response: {"total": N, "limit": L, "offset": O, "count": len, "items": [...]}
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "market.db"
IMG_DIR = HERE / "images"

MAX_LIMIT = 200
DEFAULT_LIMIT = 50
SORTS = {
    "price_asc": "price_value ASC NULLS LAST",
    "price_desc": "price_value DESC NULLS LAST",
    "newest": "last_seen DESC",
    "oldest": "first_seen ASC",
}

ITEM_COLS = ("id, source_identifier, title, price_value, price_currency, province, "
             "sold_text, market, source_url, image_url, image_file, first_seen, last_seen")


def _conn() -> sqlite3.Connection:
    # read-only; one short-lived connection per request keeps threads simple
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _item_dict(row: sqlite3.Row, base_url: str) -> dict:
    d = dict(row)
    # surface a ready-to-use link to the served archive image, when we have one
    d["image_local_url"] = (f"{base_url}/image/{d['id']}" if d.get("image_file") else None)
    return d


def _build_items_query(qs: dict) -> tuple[str, str, list]:
    """Return (where_sql, join_sql, params) shared by the count and page queries."""
    where = ["1=1"]
    params: list = []
    join = ""

    q = _one(qs, "q")
    if q:
        where.append("i.title LIKE ?")
        params.append(f"%{q}%")

    terms = _csv(qs, "term")
    if terms:
        placeholders = ",".join("?" * len(terms))
        join = ("JOIN (SELECT DISTINCT item_id FROM item_terms "
                f"WHERE term IN ({placeholders})) t ON t.item_id = i.id")
        # join params bind BEFORE where params (see return); kept separate for that.
        term_params = terms
    else:
        term_params = []

    province = _one(qs, "province")
    if province:
        where.append("i.province = ?")
        params.append(province)

    market = _one(qs, "market")
    if market:
        where.append("i.market = ?")
        params.append(market)

    min_price = _num(qs, "min_price")
    if min_price is not None:
        where.append("i.price_value >= ?")
        params.append(min_price)

    max_price = _num(qs, "max_price")
    if max_price is not None:
        where.append("i.price_value <= ?")
        params.append(max_price)

    if _one(qs, "has_image") == "1":
        where.append("i.image_url LIKE 'http%'")
    if _one(qs, "has_photo") == "1":
        where.append("i.image_file IS NOT NULL")

    where_sql = " AND ".join(where)
    # term_params (for the JOIN) must bind before the WHERE params
    return where_sql, join, term_params + params


def _one(qs: dict, key: str) -> str | None:
    v = qs.get(key)
    return v[0].strip() if v and v[0].strip() else None


def _csv(qs: dict, key: str) -> list[str]:
    out: list[str] = []
    for v in qs.get(key, []):
        out.extend(p.strip() for p in v.split(",") if p.strip())
    return out


def _num(qs: dict, key: str) -> float | None:
    v = _one(qs, key)
    if v is None:
        return None
    try:
        return float(v)
    except ValueError:
        return None


class Handler(BaseHTTPRequestHandler):
    server_version = "AmuletMarketAPI/1.0"

    # -- plumbing ----------------------------------------------------------
    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")  # prototype: open CORS
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _base_url(self) -> str:
        host = self.headers.get("Host") or f"127.0.0.1:{self.server.server_address[1]}"
        return f"http://{host}"

    def log_message(self, fmt, *args):  # quieter default logging
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)
        try:
            if path == "/":
                return self._index()
            if path == "/health":
                return self._health()
            if path == "/items":
                return self._items(qs)
            if path.startswith("/items/"):
                return self._item(path.rsplit("/", 1)[-1])
            if path == "/terms":
                return self._facet("item_terms", "term")
            if path == "/provinces":
                return self._provinces()
            if path == "/stats":
                return self._stats()
            if path.startswith("/image/"):
                return self._image(path.rsplit("/", 1)[-1])
            return self._send_json({"error": "not found", "path": path}, 404)
        except Exception as e:  # never leak a stack trace to the client
            return self._send_json({"error": "internal", "detail": str(e)[:200]}, 500)

    # -- endpoints ---------------------------------------------------------
    def _health(self):
        with _conn() as con:
            n = con.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            ts = con.execute(
                "SELECT value FROM meta WHERE key='snapshot_source_last_seen'"
            ).fetchone()
        self._send_json({"ok": True, "item_count": n,
                         "snapshot_source_last_seen": ts[0] if ts else None})

    def _items(self, qs):
        where_sql, join, params = _build_items_query(qs)

        limit = min(int(_num(qs, "limit") or DEFAULT_LIMIT), MAX_LIMIT)
        limit = max(limit, 1)
        offset = max(int(_num(qs, "offset") or 0), 0)
        sort = SORTS.get(_one(qs, "sort") or "newest", SORTS["newest"])

        with _conn() as con:
            total = con.execute(
                f"SELECT COUNT(*) FROM items i {join} WHERE {where_sql}", params
            ).fetchone()[0]
            rows = con.execute(
                f"SELECT {ITEM_COLS} FROM items i {join} WHERE {where_sql} "
                f"ORDER BY {sort} LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
        base = self._base_url()
        self._send_json({
            "total": total, "limit": limit, "offset": offset, "count": len(rows),
            "items": [_item_dict(r, base) for r in rows],
        })

    def _item(self, raw_id):
        try:
            item_id = int(raw_id)
        except ValueError:
            return self._send_json({"error": "bad id"}, 400)
        with _conn() as con:
            row = con.execute(
                f"SELECT {ITEM_COLS}, raw_metadata FROM items WHERE id=?", (item_id,)
            ).fetchone()
            if not row:
                return self._send_json({"error": "not found", "id": item_id}, 404)
            terms = [r[0] for r in con.execute(
                "SELECT term FROM item_terms WHERE item_id=?", (item_id,))]
        d = _item_dict(row, self._base_url())
        d["terms"] = terms
        try:
            d["raw_metadata"] = json.loads(d.get("raw_metadata") or "null")
        except (ValueError, TypeError):
            pass
        self._send_json(d)

    def _facet(self, table, col):
        with _conn() as con:
            rows = con.execute(
                f"SELECT {col} AS v, COUNT(*) AS n FROM {table} "
                f"GROUP BY {col} ORDER BY n DESC"
            ).fetchall()
        self._send_json({"count": len(rows),
                         "facets": [{"value": r["v"], "items": r["n"]} for r in rows]})

    def _provinces(self):
        with _conn() as con:
            rows = con.execute(
                "SELECT province AS v, COUNT(*) AS n FROM items "
                "WHERE province IS NOT NULL GROUP BY province ORDER BY n DESC"
            ).fetchall()
        self._send_json({"count": len(rows),
                         "facets": [{"value": r["v"], "items": r["n"]} for r in rows]})

    def _stats(self):
        with _conn() as con:
            c = con.cursor()
            total = c.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            with_img = c.execute(
                "SELECT COUNT(*) FROM items WHERE image_url LIKE 'http%'").fetchone()[0]
            with_photo = c.execute(
                "SELECT COUNT(*) FROM items WHERE image_file IS NOT NULL").fetchone()[0]
            pmin, pmax, pavg, pmed = c.execute(
                "SELECT MIN(price_value), MAX(price_value), AVG(price_value), "
                "(SELECT price_value FROM items WHERE price_value IS NOT NULL "
                " ORDER BY price_value LIMIT 1 OFFSET "
                " (SELECT COUNT(*)/2 FROM items WHERE price_value IS NOT NULL)) "
                "FROM items"
            ).fetchone()
            markets = {r[0]: r[1] for r in c.execute(
                "SELECT market, COUNT(*) FROM items GROUP BY market")}
            top_terms = [{"value": r[0], "items": r[1]} for r in c.execute(
                "SELECT term, COUNT(*) FROM item_terms GROUP BY term "
                "ORDER BY 2 DESC LIMIT 15")]
            top_prov = [{"value": r[0], "items": r[1]} for r in c.execute(
                "SELECT province, COUNT(*) FROM items WHERE province IS NOT NULL "
                "GROUP BY province ORDER BY 2 DESC LIMIT 15")]
            src_ts = c.execute(
                "SELECT value FROM meta WHERE key='snapshot_source_last_seen'"
            ).fetchone()
        self._send_json({
            "item_count": total,
            "with_image_url": with_img,
            "with_local_photo": with_photo,
            "price": {"min": pmin, "max": pmax,
                      "avg": round(pavg, 2) if pavg else None, "median": pmed,
                      "currency": "THB"},
            "by_market": markets,
            "top_terms": top_terms,
            "top_provinces": top_prov,
            "snapshot_source_last_seen": src_ts[0] if src_ts else None,
        })

    def _image(self, raw_id):
        try:
            item_id = int(raw_id)
        except ValueError:
            return self._send_json({"error": "bad id"}, 400)
        path = IMG_DIR / f"{item_id}.jpg"
        if not path.exists():
            return self._send_json({"error": "no archived image", "id": item_id}, 404)
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _index(self):
        with _conn() as con:
            n = con.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            ts = con.execute(
                "SELECT value FROM meta WHERE key='snapshot_source_last_seen'"
            ).fetchone()
        html = INDEX_HTML.format(n=n, ts=(ts[0] if ts else "—"))
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


INDEX_HTML = """<!doctype html><meta charset=utf-8>
<title>Amulet Market API</title>
<style>
 body{{font-family:system-ui,-apple-system,sans-serif;max-width:760px;margin:2rem auto;
      padding:0 1rem;line-height:1.6;font-size:18px;color:#1a1a1a}}
 h1{{font-size:1.7rem;margin-bottom:.2rem}} code{{background:#f2efe9;padding:.1em .4em;border-radius:4px}}
 a{{color:#8a5a1a}} .muted{{color:#666;font-size:.9em}}
 li{{margin:.35rem 0}} .pill{{background:#8a5a1a;color:#fff;padding:.1em .6em;border-radius:1em;font-size:.8em}}
</style>
<h1>🪬 Amulet Market API</h1>
<p><span class=pill>{n} listings</span> &nbsp;<span class=muted>snapshot through {ts}</span></p>
<p>Read-only JSON over the excavated Lazada amulet market. Try these:</p>
<ul>
 <li><a href="/stats">/stats</a> — totals, price distribution, top facets</li>
 <li><a href="/items?limit=5">/items?limit=5</a> — first 5 listings</li>
 <li><a href="/items?term=ตะกรุด&sort=price_desc&limit=5">/items?term=ตะกรุด&sort=price_desc</a> — priciest takrut</li>
 <li><a href="/items?q=หลวงพ่อ&has_photo=1&limit=5">/items?q=หลวงพ่อ&has_photo=1</a> — with servable photos</li>
 <li><a href="/items?min_price=5000&limit=5">/items?min_price=5000</a> — ฿5,000+</li>
 <li><a href="/terms">/terms</a> · <a href="/provinces">/provinces</a> — facets</li>
 <li><a href="/health">/health</a></li>
</ul>
<p class=muted>Filters on /items: q, term, province, market, min_price, max_price,
 has_image, has_photo, sort (price_asc·price_desc·newest·oldest), limit, offset.
 Item detail: <code>/items/{{id}}</code>. Archived photo: <code>/image/{{id}}</code>.</p>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="Amulet Market read API")
    ap.add_argument("--host", default="127.0.0.1")
    # Default comes from $PORT so a supervisor can assign one, with --port still
    # winning when given explicitly. Without this the server binds 8787 no matter
    # what it was told to use, which reads as "started fine" and then answers on
    # the wrong port.
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT") or 8787))
    a = ap.parse_args()
    if not DB_PATH.exists():
        raise SystemExit("market.db missing — run: python3 export_snapshot.py")
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"Amulet Market API → http://{a.host}:{a.port}  (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
