#!/usr/bin/env python3
"""Fast exporter for triskeli.ru catalog."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from scripts.fast_export_base import (
    AsyncFetcher,
    HTTPClientConfig,
    NotFoundError,
    add_antibot_arguments,
    acquire_process_lock,
    create_antibot_runtime,
    export_products,
    finalize_antibot_runtime,
    make_cli_progress_callback,
    load_url_map,
    load_url_map_with_fallback,
    load_export_products,
    merge_products,
    prepare_incremental_writer,
    prime_writer_from_export,
    release_process_lock,
    request_with_retries,
    use_export_context,
)

LOGGER = logging.getLogger(__name__)

SITE_DOMAIN = "triskeli.ru"
SCRIPT_NAME = Path(__file__).stem
URL_MAP_PATH = Path("data/sites/triskeli.ru/Triskeli 2025-09-27 Data.json")
FILTERED_MAP_PATH = Path("data/sites/triskeli.ru/Triskeli Data filtered.json")
ACTIVE_URL_PATH = Path("data/sites/triskeli.ru/triskeli_active_urls.json")
EXPORT_PATH = Path("data/sites/triskeli.ru/exports/httpx_latest.json")
PARTIAL_PATH = Path("data/sites/triskeli.ru/temp/httpx_partial.jsonl")
LOCK_FILE = Path(f"/tmp/export_{SITE_DOMAIN.replace('.', '_')}.lock")
CONFIG_PATH = Path("config/settings.json")


def _normalize_product_url(url: str) -> Optional[str]:
    candidate = url.strip()
    if not candidate:
        return None

    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"}:
        return None

    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host != SITE_DOMAIN:
        if parsed.netloc.lower() != SITE_DOMAIN:
            return None

    segments = [seg for seg in parsed.path.split("/") if seg]
    if len(segments) < 4 or segments[0] != "collection" or segments[2] != "product":
        return None

    category = segments[1]
    slug = segments[3]
    normalized = f"https://www.{SITE_DOMAIN}/collection/{category}/product/{slug.rstrip('/')}/"
    return normalized


def _load_product_urls(limit: Optional[int] = None) -> List[str]:
    def _read_json(path: Path) -> Optional[List[str]]:
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            LOGGER.warning("Failed to parse URL file %s", path)
            return None
        urls: List[str] = []
        if isinstance(payload, dict):
            values = payload.get("links")
            if isinstance(values, list):
                for item in values:
                    if isinstance(item, str):
                        urls.append(item)
                    elif isinstance(item, dict) and isinstance(item.get("url"), str):
                        urls.append(item["url"])
        if urls:
            return urls
        return None

    urls = load_url_map_with_fallback(
        SITE_DOMAIN,
        allowed_domains={SITE_DOMAIN, f"www.{SITE_DOMAIN}"},
        normalize=_normalize_product_url,
    )

    if not urls:
        urls = _read_json(ACTIVE_URL_PATH)
    if not urls:
        urls = _read_json(FILTERED_MAP_PATH)
    if not urls and URL_MAP_PATH.exists():
        LOGGER.warning("Fallback to legacy URL map %s", URL_MAP_PATH)
        urls = load_url_map(
            URL_MAP_PATH,
            allowed_domains={SITE_DOMAIN, f"www.{SITE_DOMAIN}"},
            normalize=_normalize_product_url,
        )

    if limit is not None and limit > 0:
        return urls[:limit]
    return urls


def _safe_float(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    cleaned = value.replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_product_payload(html: str) -> Optional[Dict[str, Any]]:
    match = re.search(r"(?:var\s+)?product\s*=", html)
    if not match:
        return None
    start = match.end()
    length = len(html)
    brace_level = 0
    json_start = None
    for index in range(start, length):
        char = html[index]
        if char == '{':
            json_start = index
            brace_level = 1
            break
        elif char in ' \t\n\r':
            continue
        else:
            return None

    if json_start is None:
        return None

    for index in range(json_start + 1, length):
        char = html[index]
        if char == '{':
            brace_level += 1
        elif char == '}':
            brace_level -= 1
            if brace_level == 0:
                json_text = html[json_start : index + 1]
                try:
                    return json.loads(json_text)
                except json.JSONDecodeError:
                    LOGGER.debug("Failed to decode product payload")
                    return None
    return None


def _extract_product(html: str, url: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    product_payload = _parse_product_payload(html)

    name_node = soup.select_one("h1")
    title_node = soup.select_one("title")
    meta_desc = soup.find("meta", attrs={"name": "description"})

    base_price: Optional[float] = None
    if product_payload:
        raw_price = product_payload.get("price_min") or product_payload.get("price_max")
        if isinstance(raw_price, (int, float, str)):
            base_price = _safe_float(str(raw_price))

    if base_price is None:
        price_node = soup.select_one("[data-product-price], .product-page__price")
        if price_node:
            base_price = _safe_float(price_node.get_text())

    variants_data = []
    total_stock = 0.0
    in_stock = False

    if product_payload and isinstance(product_payload.get("variants"), list):
        for variant in product_payload["variants"]:
            title = variant.get("title")
            price = _safe_float(str(variant.get("price")))
            quantity = _safe_float(str(variant.get("quantity"))) or 0.0
            available = bool(variant.get("available"))
            if price is None and base_price is not None:
                price = base_price
            in_stock_variant = available and quantity > 0
            total_stock += quantity
            if in_stock_variant:
                in_stock = True
            attributes: Dict[str, Any] = {}
            option_values = variant.get("option_values")
            if isinstance(option_values, list) and option_values:
                attributes = {"option": option_values[0].get("title")}
            variants_data.append(
                {
                    "type": "variant",
                    "value": title,
                    "price": price,
                    "stock": quantity,
                    "stock_quantity": quantity,
                    "in_stock": in_stock_variant,
                    "variant_id": variant.get("id"),
                    "sku": variant.get("sku"),
                    "attributes": attributes,
                }
            )

    if not variants_data:
        # fallback — простая карточка
        quantity = None
        stock_node = soup.select_one("[data-quantity], .product-availability")
        if stock_node:
            quantity = _safe_float(stock_node.get_text())
        if quantity is not None:
            total_stock = quantity
            in_stock = quantity > 0
        else:
            total_stock = 1.0 if base_price is not None else 0.0
            in_stock = total_stock > 0

    product: Dict[str, Any] = {
        "url": url,
        "original_url": url,
        "name": name_node.get_text(strip=True) if name_node else None,
        "price": base_price,
        "base_price": base_price,
        "currency": "RUB",
        "stock": round(total_stock, 3),
        "stock_quantity": round(total_stock, 3),
        "in_stock": in_stock,
        "variations": variants_data,
        "seo_h1": name_node.get_text(strip=True) if name_node else None,
        "seo_title": title_node.get_text(strip=True) if title_node else None,
        "seo_meta_description": meta_desc.get("content") if meta_desc else None,
        "site_domain": SITE_DOMAIN,
    }
    return product


async def _fetch_product(client: httpx.AsyncClient, url: str) -> Dict[str, Any]:
    response = await request_with_retries(
        client,
        "GET",
        url,
        follow_redirects=True,
    )
    product = _extract_product(response.text, url)
    product["scraped_at"] = datetime.now(timezone.utc).isoformat()
    return product


def _run(
    concurrency: int,
    limit: Optional[int],
    dry_run: bool,
    *,
    resume: bool,
    resume_window_hours: Optional[int],
    skip_existing: bool,
    use_antibot: bool,
    antibot_concurrency: Optional[int] = None,
    antibot_timeout: float = 90.0,
) -> None:
    urls = _load_product_urls(limit)
    if not urls:
        LOGGER.warning("URL map produced no product URLs")
        return

    writer, existing_partial = prepare_incremental_writer(
        PARTIAL_PATH,
        resume=resume,
        resume_window_hours=resume_window_hours,
    )

    if existing_partial:
        LOGGER.info("Resume: loaded %s products from partial", len(existing_partial))

    existing_products: List[Dict[str, Any]] = []
    if skip_existing:
        existing_products = load_export_products(EXPORT_PATH)
        if existing_products:
            prime_writer_from_export(writer, EXPORT_PATH, existing_products)

    urls_to_fetch = [url for url in urls if url not in writer.processed_urls]
    LOGGER.info(
        "Total URLs: %s (skipped: %s, to fetch: %s)",
        len(urls),
        len(urls) - len(urls_to_fetch),
        len(urls_to_fetch),
    )

    client_config = HTTPClientConfig(
        concurrency=max(concurrency, 1),
        timeout=25.0,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) triskeli-fast-export/1.0",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        },
    )
    fetcher = AsyncFetcher(client_config)
    failures: List[Dict[str, Any]] = []

    async def handler(client: httpx.AsyncClient, url: str) -> Optional[Dict[str, Any]]:
        try:
            product = await _fetch_product(client, url)
        except NotFoundError as exc:
            failures.append({"url": url, "status": exc.status_code, "error": str(exc)})
            return None
        except Exception as exc:  # noqa: BLE001
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            failures.append({"url": url, "status": status_code, "error": str(exc)})
            return None

        writer.append(product)
        await asyncio.sleep(0.05)
        return product

    fallback_concurrency = antibot_concurrency
    if fallback_concurrency is None or fallback_concurrency <= 0:
        fallback_concurrency = max(1, concurrency // 4) or 1

    antibot_runtime = create_antibot_runtime(
        enabled=use_antibot,
        config_path=CONFIG_PATH,
        concurrency=fallback_concurrency,
        timeout=antibot_timeout,
    )

    processed_products: List[Dict[str, Any]] = []
    total_attempts = len(urls_to_fetch)
    progress_callback = make_cli_progress_callback(
        site=SITE_DOMAIN,
        script=SCRIPT_NAME,
        total=total_attempts,
    )

    try:
        if urls_to_fetch:
            with use_export_context(antibot=antibot_runtime):
                processed_products = asyncio.run(
                    fetcher.run(
                        urls_to_fetch,
                        handler,
                        progress_callback=progress_callback,
                        progress_total=total_attempts,
                    )
                )
        products = writer.finalize()
        if skip_existing and existing_products:
            products = merge_products(existing_products, products)

        LOGGER.info("Collected %s products", len(products))

        if dry_run:
            writer.cleanup()
            LOGGER.info("Dry run: skipping export write")
            return

        success_ratio = (
            len(processed_products) / total_attempts if total_attempts > 0 else None
        )

        export_products(
            SITE_DOMAIN,
            EXPORT_PATH,
            products,
            success_rate=success_ratio,
        )
        writer.cleanup()

        if failures:
            failure_log = PARTIAL_PATH.parent / "triskeli_failures.json"
            failure_log.parent.mkdir(parents=True, exist_ok=True)
            failure_log.write_text(
                json.dumps(failures, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            LOGGER.warning("Failed to fetch %s URLs", len(failures))
            for failure in failures[:10]:
                LOGGER.warning(
                    "  %s -> %s %s",
                    failure.get("url"),
                    failure.get("status"),
                    failure.get("error"),
                )
    finally:
        writer.close()
        finalize_antibot_runtime(antibot_runtime)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Triskeli fast exporter")
    parser.add_argument("--concurrency", type=int, default=4, help="Max concurrent requests")
    parser.add_argument("--limit", type=int, default=0, help="Limit product URLs")
    parser.add_argument("--dry-run", action="store_true", help="Skip writing exports")
    parser.add_argument(
        "--resume",
        dest="resume",
        action="store_true",
        default=True,
        help="Resume from partial JSONL (default)",
    )
    parser.add_argument(
        "--no-resume",
        dest="resume",
        action="store_false",
        help="Ignore partial progress",
    )
    parser.add_argument(
        "--resume-window-hours",
        type=int,
        default=6,
        help="Reset partial if older than this many hours",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Seed processed URLs from existing export",
    )
    add_antibot_arguments(parser, default_enabled=True)
    return parser


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    acquire_process_lock(LOCK_FILE, logger=LOGGER)

    try:
        args = _build_parser().parse_args()
        limit = args.limit if args.limit > 0 else None
        _run(
            args.concurrency,
            limit,
            args.dry_run,
            resume=args.resume,
            resume_window_hours=args.resume_window_hours,
            skip_existing=args.skip_existing,
            use_antibot=args.use_antibot,
            antibot_concurrency=args.antibot_concurrency,
            antibot_timeout=args.antibot_timeout,
        )
    finally:
        release_process_lock(LOCK_FILE, logger=LOGGER)


if __name__ == "__main__":
    main()
