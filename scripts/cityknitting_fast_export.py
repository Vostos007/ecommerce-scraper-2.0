#!/usr/bin/env python3
"""Fast exporter for sittingknitting.ru (Bitrix) using httpx."""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from itertools import product
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from scripts.fast_export_base import (
    AsyncFetcher,
    HTTPClientConfig,
    NotFoundError,
    add_antibot_arguments,
    acquire_process_lock,
    create_antibot_runtime,
    binary_stock,
    export_products,
    finalize_antibot_runtime,
    make_cli_progress_callback,
    load_url_map,
    load_url_map_with_fallback,
    load_export_products,
    merge_products,
    prepare_incremental_writer,
    prime_writer_from_export,
    record_error_product,
    release_process_lock,
    request_with_retries,
    update_summary,
    use_export_context,
)

LOGGER = logging.getLogger(__name__)

SITE_DOMAIN = "sittingknitting.ru"
SCRIPT_NAME = Path(__file__).stem
URL_MAP_PATH = Path("data/sites/sittingknitting.ru/sittingknitting.ru.URL-map.json")
EXPORT_PATH = Path("data/sites/sittingknitting.ru/exports/httpx_latest.json")
PARTIAL_PATH = Path("data/sites/sittingknitting.ru/temp/httpx_partial.jsonl")
LOCK_FILE = Path(f"/tmp/export_{SITE_DOMAIN.replace('.', '_')}.lock")
CONFIG_PATH = Path("config/settings.json")
AJAX_FALLBACK_PATH = "/local/templates/sittingknitting/components/unlimtech/catalog.item/detail/ajax.php"
AJAX_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://sittingknitting.ru/",
}


def _build_soup(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:  # pragma: no cover - fallback when lxml missing
        return BeautifulSoup(html, "html.parser")


def _safe_float(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    cleaned = value.strip().replace(" ", "").replace(",", ".")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _text_or_none(node: Optional[Any]) -> Optional[str]:
    if not node:
        return None
    text = node.get_text(strip=True)
    return text or None


def _extract_js_var(html: str, name: str) -> Optional[str]:
    pattern = re.compile(rf"var\s+{re.escape(name)}\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)
    match = pattern.search(html)
    if match:
        return match.group(1)
    return None


def _sanitize(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return re.sub(r"\s+", " ", value).strip()


def _extract_properties(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    properties: List[Dict[str, Any]] = []
    for prop in soup.select("div.elementSkuProperty"):
        prop_name = prop.get("data-name")
        if not prop_name:
            continue
        values: List[str] = []
        for item in prop.select("li.elementSkuPropertyValue"):
            value = item.get("data-value") or item.get("data-name")
            if value:
                values.append(value.strip())
        if not values:
            continue
        properties.append(
            {
                "name": prop_name,
                "values": values,
                "level": prop.get("data-level") or str(len(properties) + 1),
                "highload": prop.get("data-highload", "N"),
            }
        )
    return properties


def _build_props_strings(properties: Sequence[Dict[str, Any]]) -> Tuple[str, str]:
    entries = [f"{prop['name']}:{value}" for prop in properties for value in prop["values"]]
    props_string = ";".join(entries)
    if props_string:
        props_string += ";"

    highload_entries = [prop["name"] for prop in properties if str(prop.get("highload", "N")).upper() == "Y"]
    highload_string = ";".join(highload_entries)
    if highload_string:
        highload_string += ";"

    return props_string, highload_string


async def _fetch_variations(
    client: Any,
    *,
    html: str,
    soup: BeautifulSoup,
    url: str,
) -> List[Dict[str, Any]]:
    container = soup.select_one("div.elementSku")
    if not container:
        return []

    properties = _extract_properties(soup)
    if not properties:
        return []

    combinations = list(product(*(prop["values"] for prop in properties)))
    if not combinations:
        return []

    max_variations = 200
    if len(combinations) > max_variations:
        LOGGER.warning(
            "CityKnitting variations truncated: %s -> %s", len(combinations), max_variations
        )
        combinations = combinations[:max_variations]

    props_string, highload_string = _build_props_strings(properties)
    site_id = _extract_js_var(html, "SITE_ID") or "s1"
    count_properties = _extract_js_var(html, "countTopProperties")
    ajax_path = _extract_js_var(html, "elementAjaxPath") or AJAX_FALLBACK_PATH
    ajax_url = urljoin(url, ajax_path)

    variations_map: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []

    for combo in combinations:
        attributes = {
            prop["name"]: value
            for prop, value in zip(properties, combo)
        }
        params_entries = [
            f"{prop['name']}:{value}"
            for prop, value in zip(properties, combo)
        ]
        params_string = ";".join(params_entries)
        if params_string:
            params_string += ";"

        payload = {
            "act": "selectSku",
            "props": props_string,
            "params": params_string,
            "level": properties[len(combo) - 1]["level"] if combo else "1",
            "iblock_id": container.get("data-iblock-id", ""),
            "prop_id": container.get("data-prop-id", ""),
            "product_id": container.get("data-product-id", ""),
            "highload": highload_string,
            "price-code": container.get("data-price-code", ""),
            "deactivated": container.get("data-deactivated", "N"),
            "siteId": site_id,
        }
        if count_properties:
            payload["countProperties"] = count_properties

        headers = dict(AJAX_HEADERS)
        headers["Referer"] = url

        try:
            response = await request_with_retries(
                client,
                "POST",
                ajax_url,
                data=payload,
                headers=headers,
            )
        except Exception as exc:  # pragma: no cover - network resilience
            LOGGER.debug("Variant request failed for %s: %s", combo, exc)
            continue

        try:
            data = response.json()
        except Exception:  # pragma: no cover - malformed payload
            continue
        if not data or not isinstance(data, list):
            continue

        product_payload = data[0].get("PRODUCT") if isinstance(data[0], dict) else None
        if not isinstance(product_payload, dict):
            continue

        variant_id = str(product_payload.get("ID", "")).strip()
        detail_url = product_payload.get("DETAIL_PAGE_URL") or ""
        detail_url = detail_url.replace("\\/", "/")
        variant_url = urljoin(url, detail_url) if detail_url else url

        price_value = None
        price_block = product_payload.get("PRICE", {})
        if isinstance(price_block, dict):
            price_value = price_block.get("RESULT_PRICE", {}).get("DISCOUNT_PRICE")
            if price_value is None:
                price_value = price_block.get("DISCOUNT_PRICE")
        price = None
        if isinstance(price_value, str):
            price = _safe_float(price_value)
        elif price_value is not None:
            try:
                price = float(price_value)
            except (TypeError, ValueError):
                price = None

        stock_raw = product_payload.get("CATALOG_QUANTITY")
        if isinstance(stock_raw, str):
            stock = _safe_float(stock_raw)
        elif stock_raw is None:
            stock = None
        else:
            try:
                stock = float(stock_raw)
            except (TypeError, ValueError):
                stock = None

        can_buy = product_payload.get("CAN_BUY") == "Y"
        in_stock = bool(stock and stock > 0) or can_buy
        if stock is None:
            stock = binary_stock(in_stock)

        attributes_clean = {
            key: _sanitize(value)
            for key, value in attributes.items()
            if value is not None
        }

        if attributes_clean:
            if len(attributes_clean) == 1:
                display_value = next(iter(attributes_clean.values()))
            else:
                display_value = " / ".join(
                    f"{key}: {value}" for key, value in attributes_clean.items()
                )
        else:
            display_value = _sanitize(product_payload.get("NAME")) or variant_id or "Variant"

        variation_type = "variant"
        if len(attributes_clean) == 1:
            prop_name = next(iter(attributes_clean)).lower()
            if "color" in prop_name or "tsvet" in prop_name:
                variation_type = "color"
            elif "razmer" in prop_name or "size" in prop_name:
                variation_type = "size"

        sku = None
        properties_block = product_payload.get("PROPERTIES")
        if isinstance(properties_block, dict):
            for key in ("CML2_ARTICLE", "ARTNUMBER", "SKU"):
                entry = properties_block.get(key)
                if isinstance(entry, dict):
                    sku_value = entry.get("VALUE")
                    if sku_value:
                        sku = _sanitize(str(sku_value))
                        break

        map_key = variant_id or display_value
        variation_payload = {
            "type": variation_type,
            "value": display_value,
            "price": price,
            "stock": stock,
            "in_stock": in_stock,
            "variant_id": variant_id or None,
            "sku": sku,
            "url": variant_url,
            "attributes": attributes_clean,
        }

        if map_key not in variations_map:
            order.append(map_key)
        variations_map[map_key] = variation_payload

        if len(properties) > 1:
            await asyncio.sleep(0)

    variations = [variations_map[key] for key in order]
    return variations


async def _parse_document(html: str, url: str, client: Any) -> Dict[str, Any]:
    soup = _build_soup(html)
    canonical_node = soup.find("link", rel="canonical")
    canonical_url = canonical_node.get("href") if canonical_node else url

    price_node = soup.select_one("[itemprop='price']")
    price = _safe_float(
        price_node.get("content") if price_node and price_node.has_attr("content") else None
    )
    if price is None and price_node:
        price = _safe_float(price_node.get_text())

    currency = None
    if price_node:
        currency = price_node.get("data-currency") or price_node.get("content_currency")
    container = soup.select_one("div.elementSku")
    if not currency and container:
        currency = container.get("data-currency")

    name_node = soup.select_one("[itemprop='name']") or soup.select_one("h1")

    availability = soup.select_one("[itemprop='availability']")
    availability_href = availability.get("href") if availability else ""
    in_stock_flag = bool(availability_href and "instock" in availability_href.lower())

    meta_desc = soup.find("meta", attrs={"name": "description"})

    variations = await _fetch_variations(client, html=html, soup=soup, url=url)

    if variations:
        total_stock = 0.0
        for variation in variations:
            stock_value = variation.get("stock")
            if stock_value is None:
                stock_value = binary_stock(variation.get("in_stock"))
            try:
                total_stock += float(stock_value)
            except (TypeError, ValueError):
                total_stock += 0.0
        in_stock_flag = any(bool(variant.get("in_stock")) for variant in variations)
        if price is None:
            prices = [variant.get("price") for variant in variations if variant.get("price") is not None]
            price = prices[0] if prices else None
    else:
        total_stock = binary_stock(in_stock_flag)

    product_data: Dict[str, Any] = {
        "url": canonical_url or url,
        "original_url": url,
        "product_id": container.get("data-product-id") if container else None,
        "name": _text_or_none(name_node),
        "price": price,
        "base_price": price,
        "currency": currency,
        "in_stock": in_stock_flag,
        "stock": total_stock,
        "stock_quantity": total_stock,
        "variations": variations,
        "seo_h1": _text_or_none(soup.select_one("h1")),
        "seo_title": _text_or_none(soup.select_one("title")),
        "seo_meta_description": meta_desc.get("content") if meta_desc else None,
        "site_domain": SITE_DOMAIN,
    }
    return product_data


def _is_product_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    if parsed.netloc != SITE_DOMAIN:
        return False
    if "/shop/" not in parsed.path:
        return False
    if not parsed.path.endswith(".html"):
        return False
    return True


def _load_product_urls(limit: Optional[int] = None) -> List[str]:
    urls = load_url_map_with_fallback(
        SITE_DOMAIN,
        allowed_domains={SITE_DOMAIN},
        include_predicate=_is_product_url,
    )

    if not urls and URL_MAP_PATH.exists():
        LOGGER.warning("Fallback to legacy URL map %s", URL_MAP_PATH)
        urls = load_url_map(
            URL_MAP_PATH,
            allowed_domains={SITE_DOMAIN},
            include_predicate=_is_product_url,
        )

    if limit is not None and limit > 0:
        return urls[:limit]
    return urls


async def _fetch_product(client: Any, url: str) -> Optional[Dict[str, Any]]:
    try:
        response = await request_with_retries(client, "GET", url)
    except NotFoundError as exc:
        LOGGER.info("Skipping %s (404 not found)", url)
        raise
    except Exception as exc:  # pragma: no cover - network failure
        LOGGER.warning("Failed to fetch %s: %s", url, exc)
        raise

    product = await _parse_document(response.text, url, client)
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
    writer, existing_products = prepare_incremental_writer(
        PARTIAL_PATH,
        resume=resume,
        resume_window_hours=resume_window_hours,
    )

    antibot_runtime: Optional[Any] = None

    try:
        if existing_products:
            LOGGER.info("Resume: existing partial with %s products", len(existing_products))

        existing_export_products: List[Dict[str, Any]] = []
        if skip_existing:
            existing_export_products = load_export_products(EXPORT_PATH)
            if existing_export_products:
                prime_writer_from_export(
                    writer,
                    EXPORT_PATH,
                    products=existing_export_products,
                )

        urls_to_fetch = [url for url in urls if url not in writer.processed_urls]
        LOGGER.info(
            "Total product URLs: %s (skipped: %s, to fetch: %s)",
            len(urls),
            len(urls) - len(urls_to_fetch),
            len(urls_to_fetch),
        )
        if not urls_to_fetch and skip_existing:
            LOGGER.info("No pending URLs after seeding, keeping existing export untouched")
            if existing_export_products:
                update_summary(
                    SITE_DOMAIN,
                    existing_export_products,
                    export_file=EXPORT_PATH.name,
                    status="ok",
                )
            writer.cleanup()
            return

        fetcher = AsyncFetcher(
            HTTPClientConfig(
                concurrency=concurrency,
                timeout=30.0,
                max_retries=4,
                backoff_base=0.5,
                headers={
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) fast-export/1.0",
                    "Accept": "text/html,application/xhtml+xml",
                },
            )
        )

        async def handler(client: httpx.AsyncClient, url: str) -> Optional[Dict[str, Any]]:
            try:
                product = await _fetch_product(client, url)
            except NotFoundError as exc:
                record_error_product(
                    writer,
                    domain=SITE_DOMAIN,
                    url=url,
                    status_code=exc.status_code,
                    message=str(exc),
                    logger=LOGGER,
                )
                return None
            except Exception as exc:  # pragma: no cover - defensive
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                record_error_product(
                    writer,
                    domain=SITE_DOMAIN,
                    url=url,
                    status_code=status_code,
                    message=str(exc),
                    logger=LOGGER,
                )
                return None

            if product is not None:
                writer.append(product)
            return product

        fallback_concurrency = antibot_concurrency
        if fallback_concurrency is None or fallback_concurrency <= 0:
            fallback_concurrency = max(1, concurrency // 4) or 1

        if use_antibot:
            antibot_runtime = create_antibot_runtime(
                enabled=True,
                config_path=CONFIG_PATH,
                concurrency=fallback_concurrency,
                timeout=antibot_timeout,
            )

        total_attempts = len(urls_to_fetch)
        progress_callback = make_cli_progress_callback(
            site=SITE_DOMAIN,
            script=SCRIPT_NAME,
            total=total_attempts,
        )

        processed_products: List[Dict[str, Any]] = []
        with use_export_context(antibot=antibot_runtime):
            if urls_to_fetch:
                processed_products = asyncio.run(
                    fetcher.run(
                        urls_to_fetch,
                        handler,
                        progress_callback=progress_callback,
                        progress_total=total_attempts,
                    )
                )

        products = writer.finalize()
        if skip_existing and existing_export_products:
            products = merge_products(existing_export_products, products)
        LOGGER.info("Parsed %s products", len(products))

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
    finally:
        writer.close()
        finalize_antibot_runtime(antibot_runtime)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="City Knitting fast exporter")
    parser.add_argument("--concurrency", type=int, default=48, help="Max concurrent requests")
    parser.add_argument("--limit", type=int, default=0, help="Limit product count")
    parser.add_argument("--dry-run", action="store_true", help="Skip writing files")
    parser.add_argument(
        "--resume",
        dest="resume",
        action="store_true",
        default=True,
        help="Resume from partial progress (default)",
    )
    parser.add_argument(
        "--no-resume",
        dest="resume",
        action="store_false",
        help="Restart from scratch, ignoring partial data",
    )
    parser.add_argument(
        "--resume-window-hours",
        type=int,
        default=6,
        help="Reset partial progress if older than this many hours (default: 6)",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Treat existing export entries as already processed",
    )
    add_antibot_arguments(parser, default_enabled=True)
    return parser


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    acquire_process_lock(LOCK_FILE, logger=LOGGER)

    try:
        args = _build_arg_parser().parse_args()
        limit = args.limit if args.limit > 0 else None
        antibot_concurrency = args.antibot_concurrency
        if antibot_concurrency is None or antibot_concurrency <= 0:
            antibot_concurrency = max(1, args.concurrency // 4) or 1

        _run(
            args.concurrency,
            limit,
            args.dry_run,
            resume=args.resume,
            resume_window_hours=args.resume_window_hours,
            skip_existing=args.skip_existing,
            use_antibot=args.use_antibot,
            antibot_concurrency=antibot_concurrency,
            antibot_timeout=args.antibot_timeout,
        )
    finally:
        release_process_lock(LOCK_FILE, logger=LOGGER)


if __name__ == "__main__":
    main()
