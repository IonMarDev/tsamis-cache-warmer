#!/usr/bin/env python3
"""
LiteSpeed cache warmer for giorgostsamis.gr (replacement for the LSCache Crawler,
which the host does not provide).

Reads the Rank Math sitemap index, visits every URL as a cookieless guest
(desktop + mobile User-Agent, because "Cache Mobile" is ON) so LiteSpeed stores
a cached copy before real customers arrive.

Gentle by design: one request at a time, pause between requests, backs off
when the server is slow or returns errors, stops after too many errors.

Python 3.8+, standard library only.

Usage:
  python3 warm_cache.py                       # defaults below
  python3 warm_cache.py --delay 2 --max-minutes 90 --only desktop
"""
import argparse
import gzip
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

SITE = "https://giorgostsamis.gr"
SITEMAP = SITE + "/sitemap_index.xml"

UA = {
    "desktop": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/130.0 Safari/537.36 WebionCacheWarmer/1.0",
    # contains "Mobile" and "Android" -> matches the LSCache mobile rules
    "mobile": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Mobile Safari/537.36 WebionCacheWarmer/1.0",
}

# Sitemaps warmed first (most visited / most expensive pages)
PRIORITY = ["page-sitemap", "product_cat-sitemap", "product_brand", "brand", "product-sitemap"]
# Sitemaps never warmed
SKIP_SITEMAPS = re.compile(r"(post_tag|author|local-sitemap|news-sitemap)", re.I)
# URLs never warmed (must not be cached / not useful)
SKIP_URLS = re.compile(r"/(cart|checkout|my-account|wishlist|wp-admin)(/|$)|[?&](add-to-cart|s)=", re.I)

NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


def fetch(url, ua, timeout=60):
    req = urllib.request.Request(url, headers={
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip",
        "Accept-Language": "el-GR,el;q=0.9",
    })
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        return r.status, r.headers.get("x-litespeed-cache", "-"), time.monotonic() - t0, body


PRODUCT_SITEMAP = re.compile(r"/product-sitemap\d*\.xml", re.I)


def sitemap_urls(index_url):
    """Returns a list of (is_product, url), priority sitemaps first."""
    _, _, _, body = fetch(index_url, UA["desktop"])
    if index_url.endswith(".gz"):
        body = gzip.decompress(body)
    root = ET.fromstring(body)
    children = [e.text.strip() for e in root.findall("sm:sitemap/sm:loc", NS)]
    if children:
        children = [c for c in children if not SKIP_SITEMAPS.search(c)]
        rank = lambda u: next((i for i, p in enumerate(PRIORITY) if p in u), len(PRIORITY))
        urls = []
        for c in sorted(children, key=rank):  # sorted() is stable: product-sitemap1..N keep their order
            try:
                is_product = bool(PRODUCT_SITEMAP.search(c))
                urls += [(is_product, u) for _, u in sitemap_urls(c)]
            except Exception as e:  # one broken child sitemap must not stop the run
                print(f"! sitemap error {c}: {e}", flush=True)
        return urls
    is_product = bool(PRODUCT_SITEMAP.search(index_url))
    return [(is_product, e.text.strip()) for e in root.findall("sm:url/sm:loc", NS)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sitemap", default=SITEMAP)
    ap.add_argument("--delay", type=float, default=1.5, help="seconds between requests")
    ap.add_argument("--slow", type=float, default=8.0, help="response time (s) that triggers back-off")
    ap.add_argument("--backoff", type=float, default=30.0, help="pause (s) after slow/error response")
    ap.add_argument("--max-errors", type=int, default=20, help="abort after this many errors")
    ap.add_argument("--max-minutes", type=float, default=150, help="stop after this many minutes")
    ap.add_argument("--only", choices=["desktop", "mobile"], help="warm only one device type")
    ap.add_argument("--limit", type=int, default=0, help="max URLs (0 = all), useful for testing")
    ap.add_argument("--rotate", type=int, default=1,
                    help="split product URLs into N chunks and warm one chunk per run "
                         "(pages, categories, brands are warmed every run)")
    ap.add_argument("--interval-hours", type=float, default=6,
                    help="hours between scheduled runs; picks the chunk from the current time")
    ap.add_argument("--chunk", type=int, default=-1, help="force a specific chunk (0..N-1)")
    a = ap.parse_args()

    start = time.monotonic()
    seen, priority, products = set(), [], []
    for is_product, u in sitemap_urls(a.sitemap):
        if u in seen or SKIP_URLS.search(u):
            continue
        seen.add(u)
        (products if is_product else priority).append(u)

    chunk_info = "all products"
    if a.rotate > 1:
        chunk = a.chunk if a.chunk >= 0 else int(time.time() // (a.interval_hours * 3600)) % a.rotate
        size = -(-len(products) // a.rotate)  # ceil division
        products = products[chunk * size:(chunk + 1) * size]
        chunk_info = f"product chunk {chunk + 1}/{a.rotate}"

    urls = priority + products
    if a.limit:
        urls = urls[: a.limit]
    devices = [a.only] if a.only else ["desktop", "mobile"]
    print(f"{len(priority)} pages/categories/brands + {len(products)} products ({chunk_info}) "
          f"-> {len(urls)} URLs x {len(devices)} devices", flush=True)

    stats = {"hit": 0, "miss": 0, "other": 0, "errors": 0}
    slowest = []
    done = 0
    for url in urls:
        for dev in devices:
            if (time.monotonic() - start) / 60 > a.max_minutes:
                print("time budget reached, stopping", flush=True)
                return report(stats, done, start, slowest)
            try:
                status, ls, secs, _ = fetch(url, UA[dev])
                key = ls if ls in ("hit", "miss") else "other"
                stats[key] += 1
                done += 1
                if ls == "miss":
                    slowest.append((secs, dev, url))
                print(f"{status} {ls:5} {secs:5.2f}s {dev:7} {url}", flush=True)
                if secs > a.slow:
                    print(f"  slow response, pausing {a.backoff:.0f}s", flush=True)
                    time.sleep(a.backoff)
            except urllib.error.HTTPError as e:
                stats["errors"] += 1
                print(f"{e.code} ERROR {dev:7} {url}", flush=True)
                if e.code in (429, 500, 502, 503, 504, 508):
                    time.sleep(a.backoff)
            except Exception as e:
                stats["errors"] += 1
                print(f"ERR {dev:7} {url} {e}", flush=True)
                time.sleep(a.backoff)
            if stats["errors"] >= a.max_errors:
                print("too many errors, aborting to protect the server", flush=True)
                report(stats, done, start, slowest)
                sys.exit(1)
            time.sleep(a.delay)
    report(stats, done, start, slowest)


def report(stats, done, start, slowest):
    mins = (time.monotonic() - start) / 60
    print(f"\nDone {done} requests in {mins:.1f} min | hit={stats['hit']} miss(now cached)={stats['miss']} "
          f"other={stats['other']} errors={stats['errors']}")
    if slowest:
        print("Slowest uncached pages:")
        for secs, dev, url in sorted(slowest, reverse=True)[:10]:
            print(f"  {secs:5.2f}s {dev:7} {url}")


if __name__ == "__main__":
    main()
