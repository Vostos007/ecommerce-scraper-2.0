#!/usr/bin/env python3
"""Fast exporter for manefa.ru (InSales)."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
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
    load_export_products,
    load_url_map,
    load_url_map_with_fallback,
    make_cli_progress_callback,
    merge_products,
    prepare_incremental_writer,
    prime_writer_from_export,
    record_error_product,
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
    if path.endswith("/") and len(path) > 1:
        path = path[:-1]

    segments = [segment for segment in path.split("/") if segment]
    if len(segments) < 2:
        return None

    if segments[0] == "product":
        slug = segments[1]
        if not slug:
            return None
        normalized = f"https://www.{SITE_DOMAIN}/product/{slug}"
        return normalized

    if segments[0] == "collection" and len(segments) >= 4 and segments[2] == "product":
        category = segments[1]
        slug = segments[3]
        normalized = f"https://www.{SITE_DOMAIN}/collection/{category}/product/{slug}"
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


PRODUCT_PATTERNS = (
    r"(?:var|let|const)\s+(?:window\.)?product\s*=",
    r"(?:window\.)?product\s*=",
    r"(?:var|let|const)\s+productData\s*=",
    r"(?:window\.)?productData\s*=",
    r"(?:var|let|const)\s+Product\s*=",
)

TRUTHY = {"1", "true", "yes", "y", "on", "да", "д"}


def _extract_json_after(pattern: re.Pattern[str], text: str) -> Optional[Dict[str, Any]]:
    match = pattern.search(text)
    if not match:
        return None

    start = match.end()
    length = len(text)
    brace_level = 0
    json_start = None

    for index in range(start, length):
        char = text[index]
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
        char = text[index]
        if char == "{":
            brace_level += 1
        elif char == "}":
            brace_level -= 1
            if brace_level == 0:
                json_text = text[json_start : index + 1]
                try:
                    return json.loads(json_text)
                except json.JSONDecodeError:
                    return None
    return None


def _parse_product_payload(html: str) -> Optional[Dict[str, Any]]:
    # First attempt fast regex on entire HTML
    for raw_pattern in PRODUCT_PATTERNS:
        pattern = re.compile(raw_pattern, re.IGNORECASE)
        payload = _extract_json_after(pattern, html)
        if payload:
            return payload

    # Fall back to scanning inline scripts
    soup = BeautifulSoup(html, "lxml")
    for script in soup.find_all("script"):
        text = script.string or script.get_text()
        if not text:
            continue
        for raw_pattern in PRODUCT_PATTERNS:
            pattern = re.compile(raw_pattern, re.IGNORECASE)
            payload = _extract_json_after(pattern, text)
            if payload:
                return payload

    return None


def _coerce_string(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _build_variation_entry(
    *,
    variant_id: Optional[str],
    sku: Optional[str],
    value: Optional[str],
    variant_type: Optional[str],
    price: Optional[float],
    stock: float,
    in_stock: bool,
    attributes: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "type": "variant",
        "value": value,
        "price": price,
        "stock": stock,
        "stock_quantity": stock,
        "in_stock": in_stock,
        "variant_id": variant_id or None,
        "variation_id": variant_id or None,
        "sku": sku or None,
        "variation_sku": sku or None,
        "variation_value": value or None,
        "variation_type": variant_type or None,
        "variation_price": price,
        "variation_stock": stock,
        "variation_in_stock": in_stock,
        "attributes": attributes,
        "variation_attributes": attributes,
    }


def _collect_variants_from_payload(
    payload: Dict[str, Any],
    base_price: Optional[float],
) -> Tuple[List[Dict[str, Any]], float, bool]:
    variants: List[Dict[str, Any]] = []
    total_stock = 0.0
    in_stock = False

    for variant in payload.get("variants", []) or []:
        raw_title = variant.get("title")
        price = _safe_float(str(variant.get("price")))
        quantity = _safe_float(str(variant.get("quantity")))
        available = variant.get("available")

        variant_stock = quantity if quantity is not None else 0.0
        total_stock += variant_stock

        in_stock_variant = bool(variant_stock and variant_stock > 0)
        if not in_stock_variant and isinstance(available, bool):
            in_stock_variant = available
        if in_stock_variant:
            in_stock = True

        if price is None and base_price is not None:
            price = base_price

        attributes: Dict[str, Any] = {}
        option_type = None
        option_value = None
        option_values = variant.get("option_values")
        if isinstance(option_values, list) and option_values:
            first = option_values[0]
            option_type = _coerce_string(
                first.get("option_name") or first.get("name") or first.get("option")
            )
            option_value = _coerce_string(first.get("title") or first.get("value"))
            if option_type and option_value:
                attributes[option_type.lower()] = option_value
            elif option_value:
                attributes["option"] = option_value

        variation_value = _coerce_string(raw_title) or option_value
        variant_entry = _build_variation_entry(
            variant_id=_coerce_string(variant.get("id")),
            sku=_coerce_string(variant.get("sku")),
            value=variation_value,
            variant_type=option_type
            or _coerce_string(variant.get("option_name"))
            or "variant",
            price=price,
            stock=variant_stock,
            in_stock=in_stock_variant,
            attributes=attributes,
        )
        variants.append(variant_entry)

    return variants, total_stock, in_stock


def _collect_variants_from_dom(
    soup: BeautifulSoup,
    base_price: Optional[float],
) -> Tuple[List[Dict[str, Any]], float, bool]:
    truthy = TRUTHY
    variants: List[Dict[str, Any]] = []
    total_stock = 0.0
    in_stock = False
    seen_ids: set[str] = set()

    # Attempt to parse JSON embedded in data-variants attributes first
    for node in soup.select("[data-variants], [data-product-variants], [data-variants-json]"):
        data = node.get("data-variants")
        if not data:
            data = node.get("data-product-variants") or node.get("data-variants-json")
        if not data:
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, list):
            for item in payload:
                if not isinstance(item, dict):
                    continue
                variant_id = str(item.get("id") or "")
                if not variant_id or variant_id in seen_ids:
                    continue
                seen_ids.add(variant_id)
                title = item.get("title") or item.get("value")
                price = _safe_float(str(item.get("price"))) or base_price
                quantity = _safe_float(str(item.get("quantity")))
                if quantity is None:
                    quantity = _safe_float(str(item.get("stock")))
                if quantity is None:
                    quantity = 0.0
                total_stock += quantity
                in_stock_variant = quantity > 0
                if not in_stock_variant:
                    available = item.get("available")
                    if isinstance(available, bool):
                        in_stock_variant = available
                    elif isinstance(available, str):
                        in_stock_variant = available.strip().lower() in truthy
                if in_stock_variant:
                    in_stock = True
                attributes = {}
                option_name = item.get("option_name") or item.get("name")
                option_value = item.get("option_value") or item.get("value")
                if option_name and option_value:
                    attributes[option_name.strip().lower()] = option_value
                variants.append(
                    _build_variation_entry(
                        variant_id=_coerce_string(variant_id),
                        sku=_coerce_string(item.get("sku") or item.get("barcode")),
                        value=_coerce_string(title or option_value),
                        variant_type=_coerce_string(option_name) or "variant",
                        price=price,
                        stock=quantity,
                        in_stock=in_stock_variant,
                        attributes=attributes,
                    )
                )

    if not variants:
        option_nodes = soup.select(
            "[data-variant-id], [data-variant], [data-offer-id], [data-offer], "
            "[data-sku], [data-swatch], [data-option-value], [data-option]"
        )
        for node in option_nodes:
            variant_id = (
                node.get("data-variant-id")
                or node.get("data-variant")
                or node.get("data-offer-id")
                or node.get("data-offer")
                or node.get("data-id")
                or node.get("value")
            )
            if not variant_id:
                continue
            if variant_id in seen_ids:
                continue
            seen_ids.add(variant_id)

            title = (
                node.get("data-title")
                or node.get("data-value")
                or node.get("title")
                or node.get_text(strip=True)
            )

            parent_option = node.find_parent(attrs={"data-option-name": True})
            option_name = node.get("data-option-name")
            if not option_name and parent_option is not None:
                option_name = parent_option.get("data-option-name")
            if not option_name:
                option_name = node.get("data-option")
            if not option_name:
                option_name = node.get("data-attribute-name")

            color = node.get("data-color") or node.get("data-swatch")

            price = _safe_float(node.get("data-price"))
            if price is None:
                price = base_price

            quantity = _safe_float(node.get("data-quantity"))
            if quantity is None:
                quantity = _safe_float(node.get("data-stock"))
            if quantity is None:
                quantity = _safe_float(node.get("data-qty"))
            if quantity is None:
                qty_text = node.get("data-available") or node.get("data-in-stock")
                if qty_text and qty_text.strip().lower() in truthy:
                    quantity = 1.0
            if quantity is None:
                quantity = 1.0 if (node.get("data-available") or "").lower() in truthy else 0.0

            total_stock += quantity
            in_stock_variant = quantity > 0
            available_attr = node.get("data-available") or node.get("data-in-stock")
            if not in_stock_variant and available_attr:
                in_stock_variant = available_attr.strip().lower() in truthy
            if in_stock_variant:
                in_stock = True

            attributes: Dict[str, Any] = {}
            if option_name and title:
                attributes[option_name.strip().lower()] = title
            elif color:
                attributes["color"] = color

            variants.append(
                _build_variation_entry(
                    variant_id=_coerce_string(variant_id),
                    sku=_coerce_string(
                        node.get("data-sku")
                        or node.get("data-article")
                        or node.get("data-item-code")
                        or node.get("data-sku-id")
                    ),
                    value=_coerce_string(title or color or variant_id),
                    variant_type=_coerce_string(option_name) or ("color" if color else "variant"),
                    price=price,
                    stock=quantity,
                    in_stock=in_stock_variant,
                    attributes=attributes,
                )
            )

    return variants, total_stock, in_stock


def _extract_product(html: str, url: str, api_product_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    product_payload = _parse_product_payload(html)

    # Debug: Always save payload if found, regardless of env var
    if product_payload:
        LOGGER.info(f"Found product payload with keys: {list(product_payload.keys())}")
        if os.environ.get("MANEFA_DEBUG_PAYLOADS") == "1":
            debug_dir = Path("data/sites/manefa.ru/debug")
            debug_dir.mkdir(parents=True, exist_ok=True)
            slug = url.rstrip("/").split("/")[-1] or "product"
            debug_path = debug_dir / f"{slug}.json"
            try:
                debug_path.write_text(json.dumps(product_payload, ensure_ascii=False, indent=2), encoding="utf-8")
                LOGGER.info(f"Saved debug payload to {debug_path}")
            except OSError:
                LOGGER.warning("Failed to write debug payload %s", debug_path)
    else:
        LOGGER.info("No product payload found - will try DOM parsing")

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

    if product_payload:
        payload_variants, payload_stock, payload_in_stock = _collect_variants_from_payload(
            product_payload,
            base_price,
        )
        variants_data.extend(payload_variants)
        total_stock += payload_stock
        if payload_in_stock:
            in_stock = True

    # Try to get variants from InSales API
    if api_product_data and api_product_data.get("variants"):
        api_variants, api_stock, api_in_stock = _collect_variants_from_payload(
            api_product_data,
            base_price,
        )
        if api_variants:
            LOGGER.info(f"Found {len(api_variants)} variants from InSales API")
            # Prefer API variants over payload/DOM variants
            variants_data = api_variants
            total_stock = api_stock
            in_stock = api_in_stock

    if not variants_data:
        LOGGER.info("No payload/API variants found, trying DOM parsing")
        dom_variants, dom_stock, dom_in_stock = _collect_variants_from_dom(
            soup,
            base_price,
        )
        LOGGER.info(f"DOM parsing found {len(dom_variants)} variants")
        variants_data.extend(dom_variants)
        total_stock += dom_stock
        if dom_in_stock:
            in_stock = True

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


async def _fetch_insales_product_data(client: httpx.AsyncClient, url: str) -> Optional[Dict[str, Any]]:
    """Fetch product variants data from InSales API."""
    # Extract product ID from URL
    match = re.search(r"/product/([^/]+)", url)
    if not match:
        return None

    slug = match.group(1)

    # Try to find product ID from page meta tags first
    try:
        response = await request_with_retries(
            client,
            "GET",
            url,
            max_retries=2,
            timeout=10.0,
            follow_redirects=True,
        )
        html = response.text

        # Look for product_id in meta tags
        id_match = re.search(r'"product_id"\s*:\s*(\d+)', html)
        if id_match:
            product_id = id_match.group(1)
        else:
            # Fallback: try to find product_id via meta tag
            meta_match = re.search(r'<meta[^>]*name=[\'"]product-id[\'"][^>]*content=[\'"](\d+)[\'"]', html)
            if meta_match:
                product_id = meta_match.group(1)
            else:
                LOGGER.warning(f"Could not extract product_id from {url}")
                return None
    except Exception as e:
        LOGGER.warning(f"Failed to extract product_id from {url}: {e}")
        return None

    # Construct API URL
    api_url = f"https://www.manefa.ru/products_by_id/{product_id}.json?accessories=true"

    try:
        api_response = await request_with_retries(
            client,
            "GET",
            api_url,
            max_retries=2,
            timeout=10.0,
            follow_redirects=True,
        )

        api_data = api_response.json()
        if api_data.get("status") == "ok" and api_data.get("products"):
            product_data = api_data["products"][0]
            LOGGER.info(f"Found {len(product_data.get('variants', []))} variants from API for {product_id}")
            return product_data

    except Exception as e:
        LOGGER.warning(f"Failed to fetch API data for product {product_id}: {e}")

    return None


async def _fetch_product(client: httpx.AsyncClient, url: str) -> Dict[str, Any]:
    response = await request_with_retries(
        client,
        "GET",
        url,
        max_retries=4,
        backoff_base=0.75,
        timeout=client.timeout,
        follow_redirects=True,
    )
    html = response.text
    final_url = str(response.url)

    # Try to fetch additional product data from InSales API
    api_product_data = await _fetch_insales_product_data(client, final_url)

    return _extract_product(html, final_url, api_product_data)


async def _run_export(
    *,
    limit: Optional[int] = None,
    concurrency: int = 16,
    resume: bool = False,
    antibot_enabled: bool = True,
    antibot_concurrency: Optional[int] = None,
    antibot_timeout: float = 90.0,
) -> None:
    urls = _load_product_urls(limit)
    if not urls:
        LOGGER.error("No product URLs found for %s", SITE_DOMAIN)
        return

    total_candidates = len(urls)
    progress_callback = make_cli_progress_callback(
        site=SITE_DOMAIN,
        script=SCRIPT_NAME,
        total=total_candidates,
    )

    writer, existing_partial = prepare_incremental_writer(
        PARTIAL_PATH,
        resume=resume,
        resume_window_hours=6,
    )

    if existing_partial:
        LOGGER.info("Resume: loaded %s partial products", len(existing_partial))

    existing_export_products: List[Dict[str, Any]] = []
    if resume:
        existing_export_products = load_export_products(EXPORT_PATH)
        if existing_export_products:
            prime_writer_from_export(
                writer,
                EXPORT_PATH,
                products=existing_export_products,
            )

    urls_to_fetch = [url for url in urls if url not in writer.processed_urls]
    if not urls_to_fetch:
        LOGGER.info("All URLs already processed for %s", SITE_DOMAIN)
    else:
        LOGGER.info(
            "Prepared %s URLs for fetch (skipped %s already processed)",
            len(urls_to_fetch),
            total_candidates - len(urls_to_fetch),
        )

    client_config = HTTPClientConfig(
        concurrency=max(concurrency, 1),
        timeout=30.0,
    )
    fetcher = AsyncFetcher(client_config)

    antibot_runtime = create_antibot_runtime(
        enabled=antibot_enabled,
        config_path=CONFIG_PATH,
        concurrency=antibot_concurrency or min(concurrency, 4),
        timeout=antibot_timeout,
    )

    processed_products: List[Dict[str, Any]] = []

    try:
        with use_export_context(antibot=antibot_runtime):

            async def handler(client: httpx.AsyncClient, url: str) -> Optional[Dict[str, Any]]:
                try:
                    product = await _fetch_product(client, url)
                except NotFoundError as exc:
                    record_error_product(
                        writer,
                        domain=SITE_DOMAIN,
                        url=url,
                        status_code=404,
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

                product["scraped_at"] = datetime.now(timezone.utc).isoformat()
                writer.append(product)
                return product

            if urls_to_fetch:
                processed_products = await fetcher.run(
                    urls_to_fetch,
                    handler,
                    progress_callback=progress_callback,
                    progress_total=total_candidates,
                )
    finally:
        if antibot_runtime is not None:
            await antibot_runtime.cleanup()
        writer.close()

    products = writer.finalize()
    writer.cleanup()

    if existing_export_products:
        products = merge_products(existing_export_products, products)

    success_ratio: Optional[float] = None
    if urls_to_fetch:
        success_ratio = (
            len(processed_products) / len(urls_to_fetch)
            if len(urls_to_fetch) > 0
            else None
        )

    export_products(
        SITE_DOMAIN,
        EXPORT_PATH,
        products,
        success_rate=success_ratio,
    )
    LOGGER.info(
        "Export finished for %s (%s products written)",
        SITE_DOMAIN,
        len(products),
    )


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=f"Fast exporter for {SITE_DOMAIN}")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of URLs to fetch")
    parser.add_argument("--concurrency", type=int, default=16, help="HTTP concurrency level")
    parser.add_argument("--resume", action="store_true", help="Resume export and append to existing data")
    parser.add_argument("--no-resume", action="store_false", dest="resume")
    parser.set_defaults(resume=False)
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
                antibot_enabled=bool(getattr(args, "use_antibot", True)),
                antibot_concurrency=getattr(args, "antibot_concurrency", None),
                antibot_timeout=float(getattr(args, "antibot_timeout", 90.0)),
            )
        )
    finally:
        release_process_lock(LOCK_FILE)


def main(argv: Optional[List[str]] = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    _safe_main(argv)


if __name__ == "__main__":
    main()
