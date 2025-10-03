#!/usr/bin/env python3
"""Fast exporter for manefa.ru (InSales)."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from scripts.fast_export_base import (
    AsyncFetcher,
    HTTPClientConfig,
    add_antibot_arguments,
    acquire_process_lock,
    create_antibot_runtime,
    export_products,
    finalize_antibot_runtime,
    load_export_products,
    load_url_map,
    load_url_map_with_fallback,
    make_cli_progress_callback,
    prepare_incremental_writer,
    prime_writer_from_export,
    release_process_lock,
    request_with_retries,
    use_export_context,
)

LOGGER = logging.getLogger(__name__)

SITE_DOMAIN = "manefa.ru"
SCRIPT_NAME = Path(__file__).stem
URL_MAP_PATH = Path("data/sites/manefa.ru/www.manefa.ru_.2025-10-03T17_33_39.626Z.json")
ACTIVE_URL_PATH = Path("data/sites/manefa.ru/manefa_active_urls.json")
FILTERED_MAP_PATH = Path("data/sites/manefa.ru/manefa_filtered_urls.json")
EXPORT_PATH = Path("data/sites/manefa.ru/exports/httpx_latest.json")
PARTIAL_PATH = Path("data/sites/manefa.ru/temp/httpx_partial.jsonl")
CONFIG_PATH = Path("config/settings.json")
LOCK_FILE = Path(f"/tmp/export_{SITE_DOMAIN.replace('.', '_')}.lock")


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

    path = parsed.path or "/"
    if not path.startswith("/"):
        path = f"/{path}"
    path = re.sub(r"/+", "/", path)
    if not path.endswith("/"):
        path = f"{path}/"

    segments = [segment for segment in path.split("/") if segment]
    if len(segments) < 2:
        return None

    if segments[0] == "product":
        slug = segments[1]
        if not slug:
            return None
        normalized = f"https://www.{SITE_DOMAIN}/product/{slug}/"
        return normalized

    if segments[0] == "collection" and len(segments) >= 4 and segments[2] == "product":
        category = segments[1]
        slug = segments[3]
        normalized = f"https://www.{SITE_DOMAIN}/collection/{category}/product/{slug}/"
        return normalized

    return None


def _read_links_file(path: Path) -> Optional[List[str]]:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        LOGGER.warning("Failed to parse URL file %s", path)
        return None

    urls: List[str] = []
    values = []
    if isinstance(payload, dict):
        values = payload.get("links") or []
    elif isinstance(payload, list):
        values = payload

    for item in values:
        if isinstance(item, str):
            urls.append(item)
        elif isinstance(item, dict) and isinstance(item.get("url"), str):
            urls.append(item["url"])

    return urls or None


def _load_product_urls(limit: Optional[int] = None) -> List[str]:
    urls = load_url_map_with_fallback(
        SITE_DOMAIN,
        allowed_domains={SITE_DOMAIN, f"www.{SITE_DOMAIN}"},
        normalize=_normalize_product_url,
    )

    if not urls:
        urls = _read_links_file(ACTIVE_URL_PATH)
    if not urls:
        urls = _read_links_file(FILTERED_MAP_PATH)
    if not urls and URL_MAP_PATH.exists():
        urls = load_url_map(
            URL_MAP_PATH,
            allowed_domains={SITE_DOMAIN, f"www.{SITE_DOMAIN}"},
            normalize=_normalize_product_url,
        )

    if limit is not None and limit > 0:
        return urls[:limit]
    return urls


def _safe_float(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None

    cleaned = (
        value.strip()
        .replace("\xa0", "")
        .replace(" ", "")
        .replace(",", ".")
    )

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
        if char == "{":
            json_start = index
            brace_level = 1
            break
        if char in " \t\n\r":
            continue
        return None

    if json_start is None:
        return None

    for index in range(json_start + 1, length):
        char = html[index]
        if char == "{":
            brace_level += 1
        elif char == "}":
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
        price_node = soup.select_one("span.js-product-price")
        if price_node:
            price_match = re.search(r"([0-9]+[0-9\s.,]*)", price_node.get_text())
            if price_match:
                base_price = _safe_float(price_match.group(1))

    variants_data: List[Dict[str, Any]] = []
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

            variant_in_stock = available and quantity > 0
            if variant_in_stock:
                in_stock = True

            total_stock += quantity

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
                    "in_stock": variant_in_stock,
                    "variant_id": variant.get("id"),
                    "sku": variant.get("sku"),
                    "attributes": attributes,
                }
            )

    if not variants_data:
        stock_node = soup.select_one("span.js-stock-prod-2")
        quantity = _safe_float(stock_node.get_text()) if stock_node else None
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


aSYNC_TIMEOUT = 30.0


async def _fetch_product(client: httpx.AsyncClient, url: str) -> Dict[str, Any]:
    response = await request_with_retries(
        client,
        "GET",
        url,
        max_retries=4,
        backoff_base=0.75,
        timeout=client.timeout,
    )
    html = response.text
    return _extract_product(html, url)


async def _run_export(
    *,
    limit: Optional[int] = None,
    concurrency: int = 16,
    resume: bool = False,
    antibot_enabled: bool = True,
) -> None:
    urls = _load_product_urls(limit)
    if not urls:
        LOGGER.error("No product URLs found for %s", SITE_DOMAIN)
        return

    async with AsyncFetcher(
        HTTPClientConfig(concurrency=concurrency, timeout=ASYNC_TIMEOUT),
        make_cli_progress_callback(total=len(urls)),
    ) as fetcher:
        async with use_export_context(
            site=SITE_DOMAIN,
            config_path=CONFIG_PATH,
            export_path=EXPORT_PATH,
            partial_path=PARTIAL_PATH,
        ) as context:
            if antibot_enabled:
                context.antibot = await create_antibot_runtime(CONFIG_PATH)

            existing_data = {}
            if resume and EXPORT_PATH.exists():
                existing_data = load_export_products(EXPORT_PATH)

            writer = prepare_incremental_writer(EXPORT_PATH, append=resume)
            try:
                if resume and existing_data:
                    prime_writer_from_export(writer, existing_data)

                async def _fetch(url: str) -> Dict[str, Any]:
                    return await _fetch_product(fetcher.client, url)

                await export_products(
                    urls,
                    fetch_callback=_fetch,
                    writer=writer,
                    fetcher=fetcher,
                    antibot=context.antibot,
                )

            finally:
                writer.close()
                if context.antibot is not None:
                    await finalize_antibot_runtime(context.antibot)

    LOGGER.info("Export finished for %s", SITE_DOMAIN)


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=f"Fast exporter for {SITE_DOMAIN}")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of URLs to fetch")
    parser.add_argument("--concurrency", type=int, default=16, help="HTTP concurrency level")
    parser.add_argument("--resume", action="store_true", help="Resume export and append to existing data")
    parser.add_argument("--no-resume", action="store_false", dest="resume")
    parser.set_defaults(resume=False)
    parser.add_argument("--no-antibot", action="store_true", dest="disable_antibot")
    add_antibot_arguments(parser)
    return parser.parse_args(argv)


def _safe_main(argv: Optional[List[str]] = None) -> None:
    acquire_process_lock(LOCK_FILE)
    try:
        args = _parse_args(argv)
        asyncio.run(
            _run_export(
                limit=args.limit,
                concurrency=max(args.concurrency, 1),
                resume=bool(args.resume),
                antibot_enabled=not args.disable_antibot,
            )
        )
    finally:
        release_process_lock(LOCK_FILE)


def main(argv: Optional[List[str]] = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    _safe_main(argv)


if __name__ == "__main__":
    main()
