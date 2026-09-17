# japanese-scrapper

Scrapes listings from Bring a Trailer category pages (default: Japanese, `https://bringatrailer.com/japanese/`).

## How it works

The site's category page calls a public WP REST endpoint when paginating:

```
GET https://bringatrailer.com/wp-json/bringatrailer/1.0/data/listings-filter
    ?page=N&per_page=50&get_items=1&get_stats=0
    &category=<id>&items_type=origin
```

`scrape.py` hits that endpoint directly — no HTML parsing for the listing list, no headless browser. `per_page` is capped at 50 server-side. Default sort is by auction-end timestamp, newest first. Category 9 = Japanese (the full id table is in `CATEGORY_IDS` in `scrape.py`).

For each listing the scraper then fetches the listing's HTML page to pull out:
- `description` — text from the `<div class="post-excerpt">` body
- `gallery_image_urls` — the curated body images (~5–10 per listing); strips the `?resize=W,H` query so we get full-resolution originals
- `essentials` — the "BaT Essentials" sidebar (seller, location, lot, chassis, seller_type, details bullet list, extras)

The 100+ photos in BaT's full gallery (slideshow) are intentionally **not** scraped — only the curated body images.

## Outputs

Each run writes to `<out>/`:
- `listings.json` — cleaned rows (one per listing)
- `listings.csv` — flattened scalar version (lists joined by ` | `, essentials broken into columns)
- `listings.raw.json` — unmodified upstream API payload, useful when adding new fields later
- `images/<listing_id>/NN.<ext>` — gallery images in document order

## Common invocations

```bash
python3 scrape.py                                       # 50 newest Japanese
python3 scrape.py --count 200                           # auto-paginates
python3 scrape.py --category german --count 100
python3 scrape.py --count 50 --older-than-days 365 \
                  --out data-older-1y                   # 50 ended >=1y ago
python3 scrape.py --no-images                           # skip image download
python3 scrape.py --no-detail                           # API-only, fast
```

## Gotchas / things future-you will trip on

- **`?page=N` in the browser doesn't paginate.** The `/japanese/?page=2` URL returns the same HTML as page 1 — pagination only works through the REST endpoint above. Don't try to scrape `/japanese/page/N/`.
- **Don't vary `per_page` across pages.** The server paginates by offset = `(page-1) * per_page`, so changing `per_page` mid-run causes overlap. Keep it at 50 (`MAX_PER_PAGE`) for every page; trim to `count` at the end.
- **TLS certs.** This Mac's stock Python doesn't trust system roots. The scraper uses `certifi` when available and falls back to an unverified context otherwise. Install once with `python3 -m pip install --user certifi`.
- **`--older-than-days` walks back from "now" page-by-page.** With ~25 Japanese listings/day, getting to 1y ago takes ~120 pages of skipping (~3 min just for that phase). The script logs progress every 10 pages.
- **Run output is buffered.** When backgrounding via `python3 scrape.py | tail` or similar, you won't see progress until the process exits. Use `python3 -u scrape.py > log 2>&1` for live progress.
- **Politeness sleeps:** 0.5s between API pages, 0.3s between listing-detail pages, 0.15s between image downloads. Each network call has retry/backoff with `Retry-After` honored on 429s. If you crank up `--count` into the thousands, BaT will probably tolerate it but watch for rate-limiting.
- **`scrape.py` is stdlib + `certifi`.** No `requests`, no BeautifulSoup. Keep it that way unless there's a real reason.

## Layout

```
japanese-scrapper/
├── scrape.py              # the scraper
├── CLAUDE.md              # this file
├── data/                  # most-recent run (e.g. newest 50)
└── data-older-1y/         # second run (50 from >=1 year ago)
```
