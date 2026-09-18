# Amulet Market API

A standalone, read-only JSON API over the **excavated Lazada amulet market** —
the 4,380 commerce listings harvested by the manuscript-crawler's Lazada source.
Built to be the data backend a commercial app can talk to.

**Status: raw-data prototype.** It exposes the scraped listings as-is. The
commercial/legal model is *deliberately deferred* — see **Before you ship**.

## Design

Two things are kept apart on purpose:

- **The harvester** (the Playwright scraper) stays in `manuscript-crawler`. It's
  slow and anti-bot-fragile — it belongs in a background job, never a request path.
- **This service** serves a fast, self-contained **snapshot** (`market.db`). It
  never crawls and never touches the research corpus.

```
manuscript-crawler/crawler/catalog.db   (source of truth, read-only)
        │  export_snapshot.py  (carve out method='commerce' + copy archived images)
        ▼
market.db  +  images/          (self-contained, this folder owns it)
        │  api.py  (stdlib http.server, zero deps)
        ▼
JSON API  ─────────────►  your app
```

## Run it

```bash
python3 export_snapshot.py     # build market.db + images/  (re-run to refresh)
python3 api.py                 # serve on http://127.0.0.1:8787
```

Or double-click **`Amulet API.command`** (builds on first run, opens the browser).

Zero dependencies — Python 3 standard library only.

## Endpoints

| Method + path | What |
|---|---|
| `GET /` | Human-readable index with live counts + example links |
| `GET /health` | `{ok, item_count, snapshot_source_last_seen}` |
| `GET /items` | Search / filter / paginate (params below) |
| `GET /items/{id}` | One listing, full detail + terms + parsed raw metadata |
| `GET /terms` | Search-term facet with counts |
| `GET /provinces` | Seller-province facet with counts |
| `GET /stats` | Totals, price distribution (min/max/avg/median), top facets |
| `GET /image/{id}` | Locally-archived product photo (JPEG), when present |

### `/items` query params

`q` (title substring) · `term` (exact tag, comma-separated = OR) · `province` ·
`market` (`th`/`sg`) · `min_price` · `max_price` · `has_image` (hotlink url present) ·
`has_photo` (servable archived photo present) ·
`sort` (`price_asc`·`price_desc`·`newest`·`oldest`) · `limit` (≤200) · `offset`.

Response: `{total, limit, offset, count, items:[…]}`. Each item includes
`image_url` (hotlinked Lazada CDN, may rot) and `image_local_url` (this server's
`/image/{id}`, present for the ~180 archived photos).

## Refreshing the data

Re-run the crawler in `manuscript-crawler` (`python3 fresh_market_crawl.py`),
then re-run `python3 export_snapshot.py` here and restart the API. The snapshot
rebuild is idempotent and wipes/repopulates `market.db` and `images/` each time.

## Data at a glance

- 4,380 listings, all Lazada Thailand, all priced (฿1 – ฿186,700, median ฿125)
- 2,397 with a hotlink image url · 180 with a bundled, servable photo
- 39 search terms · 2,439 with a seller province (Bangkok leads at 1,070)

## Before you ship (the deferred question)

This serves Lazada's listing data verbatim — titles, prices, deep links, and
hotlinked CDN images. robots.txt permitted the *crawl*, but that is **not** a
license to **redistribute** listing data in a commercial product; Alibaba's ToS
restricts commercial reuse regardless of robots.txt. Before any public/paid
launch, pick a posture: (a) an official Lazada **affiliate** integration with
attribution, (b) sell a **derived** product (price index, trends, taxonomy) and
stop republishing listings verbatim, or (c) use this only as reference to seed
**your own** inventory. Today's build is fine as a private prototype.

---

Contact: Nan · nan@motdang.net · Sponsor: [Ko-fi](https://ko-fi.com/defiantchiangmai) · [Patreon](https://www.patreon.com/nanobotco)
