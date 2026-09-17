#!/usr/bin/env python3
"""Scrape Bring a Trailer listings from a category page (default: Japanese).

Pulls completed-auction items via BaT's public WP REST endpoint, the same one
the site itself calls when you click "Show More" on a category page.
"""

from __future__ import annotations

import argparse
import csv
import html as html_module
import json
import random
import re
import ssl
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

try:
    import certifi
    _CERTIFI_PATH = certifi.where()
except ImportError:
    _CERTIFI_PATH = None

API_URL = "https://bringatrailer.com/wp-json/bringatrailer/1.0/data/listings-filter"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
MAX_PER_PAGE = 50  # BaT caps a single response here

# Category IDs as exposed in the BaT filter UI. "Japanese" is 9.
CATEGORY_IDS = {
    "japanese": 9,
    "german": 7,
    "italian": 8,
    "british": 4,
    "american": 3,
    "french": 6,
    "swedish": 18,
    "korean": 496,
}

# Verify TLS using certifi when available; fall back to an unverified context
# if certifi isn't installed (still works on systems lacking root certs).
if _CERTIFI_PATH:
    SSL_CTX = ssl.create_default_context(cafile=_CERTIFI_PATH)
else:
    SSL_CTX = ssl.create_default_context()
    SSL_CTX.check_hostname = False
    SSL_CTX.verify_mode = ssl.CERT_NONE


class RateLimited(Exception):
    """Server signaled we're going too fast (429 or explicit Retry-After)."""


def _request(url: str, timeout: float) -> bytes:
    req = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Referer": "https://bringatrailer.com/japanese/",
        },
    )
    try:
        with urlopen(req, context=SSL_CTX, timeout=timeout) as resp:
            return resp.read()
    except HTTPError as e:
        if e.code == 429:
            retry_after = e.headers.get("Retry-After") if e.headers else None
            raise RateLimited(retry_after or "")
        raise


def fetch_page(
    category_id: int,
    page: int,
    per_page: int,
    *,
    max_attempts: int = 5,
    base_delay: float = 1.0,
    timeout: float = 30.0,
) -> dict:
    params = {
        "page": page,
        "per_page": per_page,
        "get_items": 1,
        "get_stats": 0,
        "category": category_id,
        "items_type": "origin",
    }
    url = f"{API_URL}?{urlencode(params)}"

    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return json.loads(_request(url, timeout=timeout).decode("utf-8"))
        except RateLimited as e:
            # Honor Retry-After if the server gave one; otherwise fall through
            # to exponential backoff.
            try:
                wait = float(str(e)) if str(e) else 0.0
            except ValueError:
                wait = 0.0
            if wait <= 0:
                wait = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            print(f"  rate-limited, sleeping {wait:.1f}s (attempt {attempt}/{max_attempts})", file=sys.stderr)
            last_err = e
        except (HTTPError, URLError, TimeoutError, ConnectionError) as e:
            wait = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            print(f"  {type(e).__name__}: {e} — retry in {wait:.1f}s (attempt {attempt}/{max_attempts})", file=sys.stderr)
            last_err = e
        except json.JSONDecodeError as e:
            wait = base_delay * (2 ** (attempt - 1))
            print(f"  bad JSON: {e} — retry in {wait:.1f}s (attempt {attempt}/{max_attempts})", file=sys.stderr)
            last_err = e

        if attempt < max_attempts:
            time.sleep(wait)

    raise RuntimeError(f"giving up on page {page} after {max_attempts} attempts: {last_err}")


def scrape(
    category_id: int,
    count: int,
    *,
    end_before_ts: int | None = None,
) -> list[dict]:
    """Fetch up to `count` listings, newest-first by auction end time.

    If `end_before_ts` is set, listings whose auction ended on or after that
    timestamp are skipped — useful for "give me 50 cars from a year ago."
    """
    out: list[dict] = []
    seen: set = set()
    skipped = 0
    page = 1
    per_page = MAX_PER_PAGE  # constant per_page so page offsets don't overlap
    while len(out) < count:
        data = fetch_page(category_id, page, per_page)
        items = data.get("items", [])
        if not items:
            break
        for it in items:
            iid = it.get("id")
            if iid in seen:
                continue
            seen.add(iid)
            ts = it.get("timestamp_end")
            if end_before_ts is not None and isinstance(ts, (int, float)) and ts >= end_before_ts:
                skipped += 1
                continue
            out.append(it)
            if len(out) >= count:
                break
        if page >= data.get("pages_total", 1):
            break
        if end_before_ts is not None and page % 10 == 0:
            print(f"  page {page}: collected {len(out)}/{count}, skipped {skipped} too-recent")
        page += 1
        time.sleep(0.5)  # be polite
    if end_before_ts is not None:
        print(f"  done: collected {len(out)}, skipped {skipped} listings newer than cutoff")
    return out[:count]


def normalize(item: dict) -> dict:
    """Pick the fields most people actually want from a listing."""
    ts_end = item.get("timestamp_end")
    end_iso = (
        datetime.fromtimestamp(ts_end, tz=timezone.utc).isoformat()
        if isinstance(ts_end, (int, float))
        else None
    )
    return {
        "id": item.get("id"),
        "title": item.get("title"),
        "url": item.get("url"),
        "current_bid": item.get("current_bid"),
        "current_bid_formatted": item.get("current_bid_formatted"),
        "sold_text": item.get("sold_text"),
        "no_reserve": item.get("noreserve"),
        "premium": item.get("premium"),
        "comments": item.get("comments"),
        "views": item.get("views"),
        "watchers": item.get("watchers"),
        "country_code": item.get("country_code"),
        "thumbnail_url": item.get("thumbnail_url"),
        "cover_image_url": full_size_image_url(item.get("thumbnail_url")),
        "excerpt": item.get("excerpt"),
        "end_timestamp": ts_end,
        "end_iso": end_iso,
        # Filled in by enrich_with_detail()
        "description": None,
        "essentials": None,
        "gallery_image_urls": [],
        # Filled in by download_all_images()
        "image_paths": [],
    }


def full_size_image_url(thumb_url: str | None) -> str | None:
    """BaT thumbnails embed a `?resize=W,H` query — drop it to get the original."""
    if not thumb_url:
        return None
    parts = urlparse(thumb_url)
    return urlunparse(parts._replace(query=""))


def download_image(url: str, dest: Path, *, max_attempts: int = 4, base_delay: float = 1.0) -> bool:
    """Download an image with retry/backoff. Returns True on success."""
    if dest.exists() and dest.stat().st_size > 0:
        return True
    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            data = _request(url, timeout=30.0)
            dest.write_bytes(data)
            return True
        except RateLimited as e:
            try:
                wait = float(str(e)) if str(e) else 0.0
            except ValueError:
                wait = 0.0
            if wait <= 0:
                wait = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            print(f"  image rate-limited, sleeping {wait:.1f}s", file=sys.stderr)
            last_err = e
        except (HTTPError, URLError, TimeoutError, ConnectionError) as e:
            wait = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            print(f"  image {type(e).__name__}: {e} — retry in {wait:.1f}s", file=sys.stderr)
            last_err = e
        if attempt < max_attempts:
            time.sleep(wait)
    print(f"  failed to download {url}: {last_err}", file=sys.stderr)
    return False


def download_all_images(rows: list[dict], img_dir: Path) -> None:
    """Download every URL in each row's gallery_image_urls (falls back to cover_image_url).

    Files land at img_dir/<listing_id>/NN.<ext> so a single listing's photos stay
    grouped together in numeric order.
    """
    img_dir.mkdir(parents=True, exist_ok=True)
    total_imgs = sum(max(1, len(r.get("gallery_image_urls") or [])) for r in rows)
    print(f"downloading ~{total_imgs} images across {len(rows)} listings to {img_dir}/ ...")
    ok = 0
    fail = 0
    for i, row in enumerate(rows, 1):
        urls = list(row.get("gallery_image_urls") or [])
        if not urls and row.get("cover_image_url"):
            urls = [row["cover_image_url"]]
        if not urls:
            continue
        listing_dir = img_dir / str(row["id"])
        listing_dir.mkdir(parents=True, exist_ok=True)
        for n, url in enumerate(urls, 1):
            ext = Path(urlparse(url).path).suffix.lower() or ".jpg"
            dest = listing_dir / f"{n:02d}{ext}"
            if download_image(url, dest):
                row["image_paths"].append(str(dest.relative_to(img_dir.parent)))
                ok += 1
            else:
                fail += 1
            time.sleep(0.15)  # polite per image
        print(f"  [{i}/{len(rows)}] {row['id']}: {len(row['image_paths'])} imgs")
    print(f"downloaded {ok} images ({fail} failed)")


# ---------------------------------------------------------------------------
# Listing detail page scraping (gallery photos + description + essentials)
# ---------------------------------------------------------------------------

# Match wp-content/uploads images served from BaT.
_IMG_PATTERN = re.compile(
    r'<img[^>]*\bsrc="(https://bringatrailer\.com/wp-content/uploads/[^"]+\.(?:jpg|jpeg|png|webp))[^"]*"',
    re.IGNORECASE,
)


def _strip_query(url: str) -> str:
    parts = urlparse(url)
    return urlunparse(parts._replace(query=""))


def _slice_post_excerpt(html: str) -> str:
    """Extract the listing's post-excerpt block (the main body with curated photos)."""
    start = html.find('<div class="post-excerpt"')
    if start < 0:
        return ""
    # The body lives between post-excerpt opening and the BaT Essentials block.
    end = html.find('<div class="essentials"', start)
    if end < 0:
        end = html.find('<section', start)
    return html[start:end] if end > start else html[start:]


def _strip_html(fragment: str) -> str:
    text = re.sub(r'<br\s*/?>', '\n', fragment, flags=re.IGNORECASE)
    text = re.sub(r'</p\s*>', '\n\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html_module.unescape(text)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n[ \t]+', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def parse_gallery_images(body_html: str) -> list[str]:
    """Return de-duplicated, full-resolution gallery image URLs in document order."""
    seen: set[str] = set()
    urls: list[str] = []
    for m in _IMG_PATTERN.finditer(body_html):
        base = _strip_query(m.group(1))
        if base in seen:
            continue
        seen.add(base)
        urls.append(base)
    return urls


def parse_essentials(html: str) -> dict:
    """Parse the 'BaT Essentials' info block into a structured dict."""
    out: dict = {"details": [], "extras": {}}
    start = html.find('<div class="essentials"')
    if start < 0:
        return out
    # Find a reasonable end — next big sibling block.
    rest = html[start:start + 20000]

    # Single-line key/value pairs like "<strong>Seller</strong>: ..."
    for label, key in [("Seller", "seller"), ("Location", "location"),
                       ("Lot", "lot"), ("Private Party or Dealer", "seller_type"),
                       ("Chassis", "chassis")]:
        m = re.search(
            rf'<strong>{re.escape(label)}</strong>\s*[:#]?\s*(.*?)(?:</div>|<div|<strong>)',
            rest, re.DOTALL,
        )
        if m:
            out[key] = _strip_html(m.group(1)).strip(' :#').strip()

    # The <ul> bullet list under "Listing Details"
    m = re.search(
        r'<strong>Listing Details</strong>.*?<ul[^>]*>(.*?)</ul>', rest, re.DOTALL,
    )
    if m:
        out["details"] = [_strip_html(li) for li in re.findall(r'<li[^>]*>(.*?)</li>', m.group(1), re.DOTALL)]

    # "Additional charges from this dealer" and similar extras
    for m in re.finditer(
        r'<div class="item additional"[^>]*>\s*<strong>(.*?)</strong>\s*[:]?\s*(.*?)</div>',
        rest, re.DOTALL,
    ):
        k = _strip_html(m.group(1)).rstrip(':')
        v = _strip_html(m.group(2))
        if k and v:
            out["extras"][k] = v

    return out


def fetch_listing_detail(url: str, *, max_attempts: int = 4, base_delay: float = 1.0) -> dict:
    """Fetch a listing page and pull out the gallery, description, and essentials."""
    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            html = _request(url, timeout=30.0).decode("utf-8", errors="replace")
            body = _slice_post_excerpt(html)
            return {
                "description": _strip_html(body) if body else None,
                "gallery_image_urls": parse_gallery_images(body or html),
                "essentials": parse_essentials(html),
            }
        except RateLimited as e:
            try:
                wait = float(str(e)) if str(e) else 0.0
            except ValueError:
                wait = 0.0
            if wait <= 0:
                wait = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            print(f"  detail rate-limited, sleeping {wait:.1f}s", file=sys.stderr)
            last_err = e
        except (HTTPError, URLError, TimeoutError, ConnectionError) as e:
            wait = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            print(f"  detail {type(e).__name__}: {e} — retry in {wait:.1f}s", file=sys.stderr)
            last_err = e
        if attempt < max_attempts:
            time.sleep(wait)
    print(f"  failed to fetch detail {url}: {last_err}", file=sys.stderr)
    return {"description": None, "gallery_image_urls": [], "essentials": {}}


def enrich_with_detail(rows: list[dict]) -> None:
    """Populate description, essentials, and gallery URLs for each row by hitting its listing page."""
    print(f"fetching detail pages for {len(rows)} listings...")
    for i, row in enumerate(rows, 1):
        if not row.get("url"):
            continue
        detail = fetch_listing_detail(row["url"])
        row["description"] = detail.get("description")
        row["essentials"] = detail.get("essentials")
        row["gallery_image_urls"] = detail.get("gallery_image_urls") or []
        print(f"  [{i}/{len(rows)}] {row['id']}: {len(row['gallery_image_urls'])} gallery imgs")
        time.sleep(0.3)  # polite between listing pages


def _flatten_for_csv(row: dict) -> dict:
    """CSV needs scalars — turn lists/dicts into joined strings."""
    out: dict = {}
    for k, v in row.items():
        if isinstance(v, list):
            out[k] = " | ".join(str(x) for x in v)
        elif isinstance(v, dict):
            # Pull out a few useful essentials, then dump the rest as JSON
            if k == "essentials":
                out["seller"] = v.get("seller", "")
                out["location"] = v.get("location", "")
                out["lot"] = v.get("lot", "")
                out["chassis"] = v.get("chassis", "")
                out["seller_type"] = v.get("seller_type", "")
                out["details"] = " | ".join(v.get("details") or [])
                out["extras"] = json.dumps(v.get("extras") or {}, ensure_ascii=False)
            else:
                out[k] = json.dumps(v, ensure_ascii=False)
        else:
            out[k] = v
    return out


def write_outputs(
    items: list[dict],
    out_dir: Path,
    *,
    fetch_images: bool = True,
    fetch_detail: bool = True,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = [normalize(it) for it in items]

    if fetch_detail:
        enrich_with_detail(rows)

    if fetch_images:
        download_all_images(rows, out_dir / "images")

    json_path = out_dir / "listings.json"
    with json_path.open("w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, ensure_ascii=False)

    csv_path = out_dir / "listings.csv"
    csv_rows = [_flatten_for_csv(r) for r in rows]
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(csv_rows[0].keys()))
        writer.writeheader()
        writer.writerows(csv_rows)

    raw_path = out_dir / "listings.raw.json"
    with raw_path.open("w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)

    print(f"wrote {len(rows)} listings:")
    print(f"  {json_path}")
    print(f"  {csv_path}")
    print(f"  {raw_path}  (full upstream payload)")


def main() -> int:
    parser = argparse.ArgumentParser(description="Bring a Trailer listing scraper")
    parser.add_argument(
        "--category",
        default="japanese",
        help=f"Category slug or numeric id. Known slugs: {', '.join(CATEGORY_IDS)}",
    )
    parser.add_argument("--count", type=int, default=50, help="How many listings to fetch")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).parent / "data",
        help="Output directory",
    )
    parser.add_argument(
        "--no-images",
        action="store_true",
        help="Skip downloading images",
    )
    parser.add_argument(
        "--no-detail",
        action="store_true",
        help="Skip per-listing detail scrape (description, essentials, gallery)",
    )
    parser.add_argument(
        "--older-than-days",
        type=int,
        default=None,
        help="Only collect listings whose auction ended at least N days ago",
    )
    args = parser.parse_args()

    cat = args.category.lower()
    if cat in CATEGORY_IDS:
        category_id = CATEGORY_IDS[cat]
    else:
        try:
            category_id = int(cat)
        except ValueError:
            print(f"unknown category: {args.category}", file=sys.stderr)
            return 2

    end_before_ts = None
    if args.older_than_days is not None:
        end_before_ts = int(time.time()) - args.older_than_days * 86400
        cutoff_iso = datetime.fromtimestamp(end_before_ts, tz=timezone.utc).date().isoformat()
        print(f"filtering to listings ended before {cutoff_iso} ({args.older_than_days} days ago)")

    print(f"fetching {args.count} listings from category={args.category} (id={category_id})...")
    items = scrape(category_id, args.count, end_before_ts=end_before_ts)
    if not items:
        print("no items returned", file=sys.stderr)
        return 1
    write_outputs(
        items,
        args.out,
        fetch_images=not args.no_images,
        fetch_detail=not args.no_detail,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
