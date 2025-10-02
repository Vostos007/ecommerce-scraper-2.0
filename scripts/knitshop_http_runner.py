#!/usr/bin/env python3
"""Resilient HTTP fallback scraper for knitshop.ru.

Features:
- Rotates through all configured HTTP proxies (config/proxies_https.txt).
- Discovers catalog/product URLs on-the-fly using the knitshop site config.
- Detects newly added or removed product URLs vs cached list.
- Fetches product pages sequentially with backoff and persists results.
- Writes summary statistics plus optional cache update.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from dataclasses import dataclass
from itertools import cycle
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Set, Tuple

from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.scraper_engine import ScraperEngine  # noqa: E402
from utils.data_paths import get_site_paths  # noqa: E402


PROXY_FILE = Path("config/proxies_https.txt")
KNITSHOP_PATHS = get_site_paths("knitshop.ru")
CACHE_FILE = KNITSHOP_PATHS.cache_file
FALLBACK_EXPORT = KNITSHOP_PATHS.exports_dir / "http_fallback.json"
SUMMARY_EXPORT = KNITSHOP_PATHS.exports_dir / "http_fallback_summary.json"


@dataclass
class ProxyStats:
    success: int = 0
    failures: int = 0


def load_proxies() -> List[str]:
    if not PROXY_FILE.exists():
        raise FileNotFoundError(f"Proxy file not found: {PROXY_FILE}")

    proxies: List[str] = []
    for line in PROXY_FILE.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue

        parts = raw.split(":")
        if len(parts) == 4:
            host, port, user, password = parts
            proxies.append(f"http://{user}:{password}@{host}:{port}")
        elif len(parts) == 2:
            host, port = parts
            proxies.append(f"http://{host}:{port}")
        else:
            print(f"[warn] Skip unsupported proxy format: {raw}")

    if not proxies:
        raise RuntimeError("Proxy list is empty")

    print(f"[info] Loaded {len(proxies)} HTTP proxies")
    return proxies


def proxy_cycle(proxies: Iterable[str]) -> Iterator[str]:
    return cycle(proxies)


def load_site_config() -> Tuple[str, dict]:
    sites_path = ROOT / "config" / "sites.json"
    data = json.loads(sites_path.read_text(encoding="utf-8"))
    for site in data.get("sites", []):
        if site.get("domain") == "knitshop.ru":
            overrides = site.get("overrides", {}).get("scraping", {})
            return site.get("base_url", "https://www.knitshop.ru"), overrides
    raise RuntimeError("knitshop.ru entry not found in config/sites.json")


def load_cached_urls(limit: Optional[int]) -> List[str]:
    if not CACHE_FILE.exists():
        return []
    urls = [line.strip() for line in CACHE_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]
    if limit:
        return urls[:limit]
    return urls


def http_get(url: str, proxy: str, timeout: int, headers: dict) -> Optional[httpx.Response]:
    try:
        with httpx.Client(proxy=proxy, timeout=timeout, headers=headers, verify=False, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return resp
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] proxy {proxy} failed for {url}: {exc}")
        return None


def get_with_retries(
    url: str,
    proxies: Iterable[str],
    timeout: int,
    headers: dict,
    proxy_stats: dict,
    max_attempts: int,
) -> Optional[httpx.Response]:
    for _ in range(max_attempts):
        proxy = next(proxies)
        resp = http_get(url, proxy, timeout, headers)
        if resp is not None:
            proxy_stats[proxy].success += 1
            return resp
        proxy_stats[proxy].failures += 1
        time.sleep(1.5)  # brief backoff before trying next proxy
    return None


def discover_urls(
    engine: ScraperEngine,
    base_url: str,
    scraping_config: dict,
    proxies: Iterator[str],
    proxy_stats: dict,
    timeout: int,
    delay: float,
) -> Set[str]:
    catalog_patterns = scraping_config.get("catalog_patterns", [])
    category_urls = scraping_config.get("category_urls", [])
    product_patterns = scraping_config.get("product_patterns", ["/catalog/"])
    pagination = scraping_config.get("pagination", {})
    start_page = max(int(pagination.get("start", 1)), 1)
    max_pages = max(int(pagination.get("max_pages", 100)), 1)
    include_base = bool(pagination.get("include_base", True))
    param = pagination.get("param", "PAGEN_1")

    seeds: Set[str] = set()
    normalized_base = base_url.rstrip("/") + "/"

    def normalize_seed(url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme and parsed.netloc:
            return url
        return urljoin(normalized_base, url.lstrip("/"))

    seeds.add(normalized_base)

    for entry in category_urls:
        seeds.add(normalize_seed(entry))

    for pattern in catalog_patterns:
        seeds.add(normalize_seed(pattern))

    headers = {
        "User-Agent": engine.antibot.get_random_user_agent() if hasattr(engine.antibot, "get_random_user_agent") else "Mozilla/5.0",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
    }

    discovered: Set[str] = set()

    for seed in seeds:
        consecutive_empty = 0
        page_number = start_page
        while page_number <= max_pages:
            url = seed
            if page_number > start_page or not include_base:
                delimiter = "&" if "?" in seed else "?"
                url = f"{seed}{delimiter}{param}={page_number}"

            resp = get_with_retries(url, proxies, timeout, headers, proxy_stats, max_attempts=len(proxy_stats))
            if resp is None:
                consecutive_empty += 1
                if consecutive_empty >= 3:
                    break
                page_number += 1
                continue

            links = extract_product_links(resp.text, url, product_patterns)
            new_links = 0
            for link in links:
                if "knitshop.ru" not in link:
                    continue
                if link not in discovered:
                    discovered.add(link)
                    new_links += 1

            if new_links == 0:
                consecutive_empty += 1
                if consecutive_empty >= 2:
                    break
            else:
                consecutive_empty = 0

            page_number += 1
            time.sleep(delay)

    return discovered


def fetch_products(
    urls: List[str],
    engine: ScraperEngine,
    proxies: Iterator[str],
    proxy_stats: dict,
    timeout: int,
    delay: float,
    max_products: int,
) -> Tuple[List[dict], List[str]]:
    headers = {
        "User-Agent": engine.antibot.get_random_user_agent() if hasattr(engine.antibot, "get_random_user_agent") else "Mozilla/5.0",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
    }

    results: List[dict] = []
    failed: List[str] = []

    for url in urls:
        if len(results) >= max_products:
            break

        resp = get_with_retries(url, proxies, timeout, headers, proxy_stats, max_attempts=len(proxy_stats))
        if resp is None:
            failed.append(url)
            continue

        try:
            product = engine.parser.parse_product_page(resp.text, url)
            if product:
                results.append(product)
                print(f"[ok] {len(results):03d} | {product.get('name', 'unknown')} -> {url}")
            else:
                failed.append(url)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] Parser failed for {url}: {exc}")
            failed.append(url)

        time.sleep(delay)

    return results, failed


def extract_product_links(html: str, source_url: str, patterns: List[str]) -> Set[str]:
    soup = BeautifulSoup(html, "html.parser")
    links: Set[str] = set()
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        if not href or href.startswith("#"):
            continue
        absolute = urljoin(source_url, href)
        if "knitshop.ru" not in absolute:
            continue
        normalized = absolute.split("#", 1)[0]
        if any(pattern in normalized for pattern in patterns):
            links.add(normalized)
    return links


def main() -> int:
    parser = argparse.ArgumentParser(description="Reliable HTTP fallback scraper for knitshop.ru")
    parser.add_argument("--max-products", type=int, default=100, help="Products to fetch (default 100)")
    parser.add_argument("--discovery-limit", type=int, default=0, help="Limit cached URLs appended to discovered set (0 = all)")
    parser.add_argument("--timeout", type=int, default=45, help="Per-request timeout in seconds")
    parser.add_argument("--delay", type=float, default=0.7, help="Base delay between requests")
    parser.add_argument("--update-cache", action="store_true", help="Overwrite cached URL list with discovered set")
    args = parser.parse_args()

    proxies = load_proxies()
    proxy_iter = proxy_cycle(proxies)
    proxy_stats = {proxy: ProxyStats() for proxy in proxies}

    engine = ScraperEngine(config_path="config/settings.json")
    base_url, scraping_config = load_site_config()

    print("[info] Discovering product URLs...")
    discovered = discover_urls(
        engine=engine,
        base_url=base_url,
        scraping_config=scraping_config,
        proxies=proxy_iter,
        proxy_stats=proxy_stats,
        timeout=args.timeout,
        delay=args.delay,
    )

    cached_urls = set(load_cached_urls(args.discovery_limit))
    all_urls = list(discovered | cached_urls)
    all_urls.sort()

    new_urls = discovered - cached_urls
    removed_urls = cached_urls - discovered

    print(f"[info] Discovered {len(discovered)} URLs (new={len(new_urls)}, removed={len(removed_urls)})")
    print(f"[info] Total URLs to fetch: {len(all_urls)}")

    print("[info] Fetching product pages...")
    results, failures = fetch_products(
        urls=all_urls,
        engine=engine,
        proxies=proxy_iter,
        proxy_stats=proxy_stats,
        timeout=args.timeout,
        delay=args.delay,
        max_products=args.max_products,
    )

    FALLBACK_EXPORT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    SUMMARY_EXPORT.write_text(
        json.dumps(
            {
                "total_products": len(results),
                "failures": failures,
                "new_urls": sorted(new_urls),
                "removed_urls": sorted(removed_urls),
                "proxy_stats": {proxy: stats.__dict__ for proxy, stats in proxy_stats.items()},
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(
        f"[done] Products={len(results)} failures={len(failures)} new_urls={len(new_urls)} "
        f"removed_urls={len(removed_urls)} -> {FALLBACK_EXPORT.name}"
    )

    if args.update_cache:
        CACHE_FILE.write_text("\n".join(sorted(discovered)), encoding="utf-8")
        print(f"[info] Updated cache with {len(discovered)} URLs -> {CACHE_FILE.name}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
