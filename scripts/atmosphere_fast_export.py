#!/usr/bin/env python3
"""Fast exporter for atmospherestore.ru catalog using httpx."""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

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
    record_error_product,
    prime_writer_from_export,
    release_process_lock,
    request_with_retries,
    update_summary,
    use_export_context,
)

LOGGER = logging.getLogger(__name__)

SITE_DOMAIN = "atmospherestore.ru"
SCRIPT_NAME = Path(__file__).stem
URL_MAP_PATH = Path("data/sites/atmospherestore.ru/atmospherestore.ru.URL-map.json")
EXPORT_PATH = Path("data/sites/atmospherestore.ru/exports/httpx_latest.json")
PARTIAL_PATH = Path("data/sites/atmospherestore.ru/temp/httpx_partial.jsonl")
LOCK_FILE = Path(f"/tmp/export_{SITE_DOMAIN.replace('.', '_')}.lock")
CONFIG_PATH = Path("config/settings.json")


def _build_soup(html: str) -> BeautifulSoup:
    parser = "lxml"
    try:
        return BeautifulSoup(html, parser)
    except Exception:  # pragma: no cover - fallback when lxml missing
        return BeautifulSoup(html, "html.parser")


def _safe_float(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    cleaned = value.strip().replace(" ", "").replace(",", ".")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _extract_product_id(url: str) -> Optional[str]:
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    product_id = params.get("product_id")
    if product_id:
        return product_id[0]
    return None


def _text_or_none(node: Optional[Any]) -> Optional[str]:
    if not node:
        return None
    text = node.get_text(strip=True)
    return text or None


def _extract_stock_value(container: BeautifulSoup, button: Optional[Any]) -> Optional[float]:
    """Extract numeric stock value from product container."""

    # Try explicit quantity counter
    span = container.select_one("#quantityCountText")
    if span and span.get_text(strip=True):
        candidate = _safe_float(re.sub(r"[^0-9,.-]", "", span.get_text()))
        if candidate is not None:
            return candidate

    # Fallback to attributes on the buy button
    if button:
        for attr in ("data-max", "data-stock", "data-quantity"):
            raw = button.get(attr)
            if raw is not None:
                value = _safe_float(str(raw))
                if value is not None:
                    return value

    # Check hidden inputs for quantity info
    quantity_input = container.select_one("input[name='quantity']")
    if quantity_input:
        for attr in ("data-max", "data-stock", "value"):
            raw = quantity_input.get(attr)
            if raw is not None:
                value = _safe_float(str(raw))
                if value is not None:
                    return value

    return None


def _parse_variations(container: BeautifulSoup) -> List[Dict[str, Any]]:
    variations: List[Dict[str, Any]] = []

    option_blocks = container.select(".product-options__item")
    for opt_index, option_block in enumerate(option_blocks):
        option_id = option_block.get("data-option-id")
        option_name = _text_or_none(option_block.select_one(".product-options__title"))
        candidates = option_block.select(".product-options__value")
        if not candidates:
            candidates = option_block.select("[data-variant-id]")
        if not candidates:
            candidates = option_block.select("li")
        if not candidates:
            continue

        for value_index, item in enumerate(candidates):
            variant_id = item.get("data-variant-id") or item.get("data-option-value-id")
            if not variant_id:
                variant_id = f"{option_id or opt_index}:{value_index}"

            label_node = item.select_one(".product-options__text")
            value_label = _text_or_none(label_node) or _text_or_none(item)
            if not value_label:
                continue

            price_raw = item.get("data-price") or item.get("data-value") or item.get("data-price-plus")
            price = _safe_float(price_raw)

            item_classes = item.get("class") or []
            disabled = "disabled" in item_classes or item.get("data-disabled") in {"1", "true", True}
            stock_attr = item.get("data-stock") or item.get("data-max")
            stock_value: Optional[float]
            if stock_attr is not None:
                stock_value = _safe_float(str(stock_attr))
            else:
                stock_value = None

            in_stock = not disabled and (stock_value is None or stock_value > 0)
            stock = stock_value if stock_value is not None else binary_stock(in_stock)

            variations.append(
                {
                    "type": "variant",
                    "value": value_label,
                    "variant_id": str(variant_id),
                    "price": price,
                    "stock": stock,
                    "stock_quantity": stock,
                    "in_stock": in_stock,
                    "attributes": {
                        key: value
                        for key, value in {
                            "option_id": option_id,
                            "option_name": option_name,
                        }.items()
                        if value is not None
                    },
                }
            )

    return variations


def _parse_product_html(html: str, source_url: str) -> Dict[str, Any]:
    soup = _build_soup(html)
    product_container = soup.find(id="product") or soup

    title_node = product_container.select_one("h1") or soup.select_one("title")
    if title_node is None:
        raise ValueError("Product title not found")

    canonical_node = soup.find("link", rel="canonical")
    canonical_url = canonical_node.get("href") if canonical_node else source_url
    product_id = _extract_product_id(canonical_url or source_url)

    price_node = product_container.select_one(".autocalc-product-price")
    price = _safe_float(price_node.get("data-value") if price_node else None)
    if price is None and price_node:
        price = _safe_float(price_node.get_text())

    currency_node = product_container.select_one(".products-full-list__price [itemprop='priceCurrency']")
    currency = currency_node.get("content") if currency_node else None

    button = product_container.select_one(".js-btn-add-cart")
    button_classes = button.get("class") if button else None
    button_disabled = False
    if button:
        button_disabled = bool(button.has_attr("disabled"))
        if button_classes:
            button_disabled = button_disabled or "disabled" in button_classes
    in_stock = bool(button) and not button_disabled

    base_stock_value = _extract_stock_value(product_container, button)
    if base_stock_value is not None and base_stock_value <= 0:
        in_stock = False

    variations = _parse_variations(product_container)

    if variations:
        in_stock = any(bool(variant.get("in_stock")) for variant in variations)
        total_stock = 0.0
        for variant in variations:
            stock_candidate = variant.get("stock")
            if stock_candidate is None:
                stock_candidate = binary_stock(variant.get("in_stock"))
            try:
                total_stock += float(stock_candidate)
            except (TypeError, ValueError):
                total_stock += 0.0
    else:
        if base_stock_value is not None:
            total_stock = float(base_stock_value)
        else:
            total_stock = binary_stock(in_stock)

    if base_stock_value is not None and base_stock_value > 0:
        in_stock = True

    meta_desc = soup.find("meta", attrs={"name": "description"})

    product: Dict[str, Any] = {
        "url": canonical_url or source_url,
        "original_url": source_url,
        "product_id": product_id,
        "name": _text_or_none(title_node),
        "price": price,
        "base_price": price,
        "currency": currency,
        "stock": total_stock,
        "stock_quantity": total_stock,
        "in_stock": in_stock,
        "variations": variations,
        "seo_h1": _text_or_none(product_container.select_one("h1")),
        "seo_title": _text_or_none(soup.select_one("title")),
        "seo_meta_description": meta_desc.get("content") if meta_desc else None,
        "site_domain": SITE_DOMAIN,
    }
    return product


def _is_candidate_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    if parsed.netloc != SITE_DOMAIN:
        return False
    if any(parsed.path.endswith(ext) for ext in (".xml", ".json", ".txt")):
        return False
    if parsed.path in {"/", ""}:
        return False
    return True


def _load_product_urls(limit: Optional[int] = None) -> List[str]:
    urls = load_url_map_with_fallback(
        SITE_DOMAIN,
        allowed_domains={SITE_DOMAIN},
        include_predicate=_is_candidate_url,
    )

    if not urls and URL_MAP_PATH.exists():  # legacy fallback
        LOGGER.warning("Fallback to legacy URL map %s", URL_MAP_PATH)
        urls = load_url_map(
            URL_MAP_PATH,
            allowed_domains={SITE_DOMAIN},
            include_predicate=_is_candidate_url,
        )

    if limit is not None and limit > 0:
        return urls[:limit]
    return urls


async def _fetch_product(client: Any, url: str) -> Optional[Dict[str, Any]]:
    try:
        response = await request_with_retries(
            client,
            "GET",
            url,
        )
    except NotFoundError as exc:
        LOGGER.info("Skipping %s (404 not found)", url)
        raise
    except Exception as exc:  # pragma: no cover - network failure
        LOGGER.warning("Failed to fetch %s: %s", url, exc)
        raise

    try:
        product = _parse_product_html(response.text, url)
    except ValueError as exc:
        LOGGER.debug("Skipping non-product page %s: %s", url, exc)
        return None

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
            "Total candidate URLs: %s (skipped: %s, to fetch: %s)",
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

        antibot_runtime = (
            create_antibot_runtime(
                enabled=True,
                config_path=CONFIG_PATH,
                concurrency=fallback_concurrency,
                timeout=antibot_timeout,
            )
            if use_antibot
            else None
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
            LOGGER.info("Dry run: skipping export")
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
    parser = argparse.ArgumentParser(description="Atmosphere Store fast exporter")
    parser.add_argument("--concurrency", type=int, default=10, help="Max concurrent requests")
    parser.add_argument("--limit", type=int, default=0, help="Limit product URLs")
    parser.add_argument("--dry-run", action="store_true", help="Skip writing exports")
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
        help="Start from scratch, ignoring partial progress",
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
