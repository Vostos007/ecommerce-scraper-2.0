#!/usr/bin/env python3
"""Fast exporter for ili-ili.com catalog with httpx + Antibot fallback."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
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
    binary_stock,
    make_cli_progress_callback,
    create_antibot_runtime,
    export_products,
    finalize_antibot_runtime,
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

SITE_DOMAIN = "ili-ili.com"
SCRIPT_NAME = Path(__file__).stem
ROOT_PATH = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT_PATH / "config" / "settings.json"
URL_MAP_PATH = (
    ROOT_PATH / "data" / "sites" / SITE_DOMAIN / f"{SITE_DOMAIN}.sitemap.json"
)
EXPORT_PATH = (
    ROOT_PATH / "data" / "sites" / SITE_DOMAIN / "exports" / "httpx_latest.json"
)
PARTIAL_PATH = (
    ROOT_PATH / "data" / "sites" / SITE_DOMAIN / "temp" / "httpx_partial.jsonl"
)

ANTIBOT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) ili-ili-fast-export/1.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru,en;q=0.8",
}

LOCK_FILE = Path(f"/tmp/export_{SITE_DOMAIN.replace('.', '_')}.lock")


def _build_soup(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:  # pragma: no cover - fallback when lxml is missing
        return BeautifulSoup(html, "html.parser")


def _safe_float(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    cleaned = re.sub(r"[^0-9,.-]", "", value)
    cleaned = cleaned.replace(",", ".")
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


def _extract_product_id(url: str) -> Optional[str]:
    parsed = urlparse(url)
    segments = [segment for segment in parsed.path.split("/") if segment]
    for segment in reversed(segments):
        if segment.isdigit():
            return segment
        match = re.search(r"(\d+)", segment)
        if match:
            return match.group(1)
    return None


def _parse_variations(
    soup: BeautifulSoup, url: str, base_price: Optional[float]
) -> List[Dict[str, Any]]:
    select = soup.select_one("select.js_select2_custom_card")
    if not select:
        return []

    variations: List[Dict[str, Any]] = []
    for option in select.find_all("option"):
        variant_id = option.get("data-offer-name")
        if not variant_id:
            continue
        qty_value = option.get("data-qty")
        stock = _safe_float(qty_value) if qty_value is not None else None
        in_stock = bool(stock and stock > 0)
        if stock is None:
            stock = binary_stock(in_stock)

        color = option.get("data-color") or option.get_text(strip=True) or variant_id
        measure = option.get("data-measure")

        attributes = {}
        if color:
            attributes["color"] = color
        if measure:
            attributes["measure"] = measure

        variation = {
            "type": "color" if option.get("data-color") else "variant",
            "value": color,
            "price": base_price,
            "stock": stock,
            "in_stock": in_stock,
            "variant_id": variant_id,
            "sku": variant_id,
            "url": url,
            "attributes": attributes,
        }
        variations.append(variation)

    return variations


def _parse_product_html(html: str, url: str) -> Dict[str, Any]:
    soup = _build_soup(html)

    canonical = soup.find("link", rel="canonical")
    canonical_url = canonical.get("href") if canonical else url

    name_node = soup.select_one("h1")
    price_node = soup.select_one(".product__price")
    price = _safe_float(price_node.get_text()) if price_node else None

    currency = None
    if price_node and price_node.get_text():
        if "₽" in price_node.get_text():
            currency = "RUB"

    counter_input = soup.select_one(".product__counter__field")
    base_stock = None
    if counter_input and counter_input.has_attr("data-max"):
        base_stock = _safe_float(counter_input.get("data-max"))

    add_button = soup.select_one(".product__add")
    in_stock_flag = bool(add_button)

    variations = _parse_variations(soup, canonical_url or url, price)

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
        in_stock_flag = any(bool(var.get("in_stock")) for var in variations)
    else:
        if base_stock is not None:
            total_stock = base_stock
            in_stock_flag = bool(total_stock > 0)
        else:
            total_stock = binary_stock(in_stock_flag)

    meta_desc = soup.find("meta", attrs={"name": "description"})

    product: Dict[str, Any] = {
        "url": canonical_url or url,
        "original_url": url,
        "product_id": _extract_product_id(url),
        "name": _text_or_none(name_node),
        "price": price,
        "base_price": price,
        "currency": currency,
        "stock": total_stock,
        "stock_quantity": total_stock,
        "in_stock": in_stock_flag,
        "variations": variations,
        "seo_h1": _text_or_none(name_node),
        "seo_title": _text_or_none(soup.select_one("title")),
        "seo_meta_description": meta_desc.get("content") if meta_desc else None,
        "site_domain": SITE_DOMAIN,
    }
    return product


def _normalize_product_url(url: str) -> Optional[str]:
    candidate = url.strip()
    if not candidate:
        return None
    candidate = candidate.split("#", 1)[0]
    candidate = candidate.split("?", 1)[0]
    if not candidate.endswith("/"):
        candidate += "/"
    return candidate


def _is_product_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    if parsed.netloc != SITE_DOMAIN:
        return False
    return bool(re.search(r"/\d+/?$", parsed.path))


def _load_product_urls(limit: Optional[int] = None) -> List[str]:
    urls = load_url_map_with_fallback(
        SITE_DOMAIN,
        allowed_domains={SITE_DOMAIN},
        include_predicate=_is_product_url,
        normalize=_normalize_product_url,
    )

    if not urls and URL_MAP_PATH.exists():
        LOGGER.warning("Fallback to legacy URL map %s", URL_MAP_PATH)
        urls = load_url_map(
            URL_MAP_PATH,
            allowed_domains={SITE_DOMAIN},
            include_predicate=_is_product_url,
            normalize=_normalize_product_url,
        )

    if limit is not None and limit > 0:
        return urls[:limit]
    return urls


async def _fetch_product(
    client: httpx.AsyncClient,
    url: str,
) -> Dict[str, Any]:
    response = await request_with_retries(
        client,
        "GET",
        url,
        headers=ANTIBOT_HEADERS,
        follow_redirects=True,
    )
    product = _parse_product_html(response.text, url)
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
    skip_health_check: bool = False,
    health_check_timeout: float = 20.0,
) -> None:
    urls = _load_product_urls(limit)
    writer, existing_products = prepare_incremental_writer(
        PARTIAL_PATH,
        resume=resume,
        resume_window_hours=resume_window_hours,
    )

    try:
        if existing_products:
            LOGGER.info(
                "Resume: existing partial with %s products", len(existing_products)
            )

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
            LOGGER.info(
                "No pending URLs after seeding, keeping existing export untouched"
            )
            if existing_export_products:
                update_summary(
                    SITE_DOMAIN,
                    existing_export_products,
                    export_file=EXPORT_PATH.name,
                    status="ok",
                )
            writer.cleanup()
            return
        base_concurrency = max(1, concurrency)
        fallback_concurrency = antibot_concurrency
        if fallback_concurrency is None or fallback_concurrency <= 0:
            derived_limit = base_concurrency // 4
            if derived_limit <= 0:
                derived_limit = 1
            fallback_concurrency = min(derived_limit, 8)

        antibot_runtime = create_antibot_runtime(
            enabled=use_antibot,
            config_path=CONFIG_PATH,
            concurrency=fallback_concurrency,
            timeout=antibot_timeout,
        )

        if antibot_runtime is not None:
            manager = antibot_runtime.manager
            if getattr(manager, "backoff", None):
                manager.backoff.max_attempts = min(manager.backoff.max_attempts, 3)
                timeout_strategy = manager.backoff.error_strategies.get("timeout", {})
                timeout_strategy["max_attempts"] = min(
                    timeout_strategy.get("max_attempts", 3), 2
                )
                timeout_strategy["base_delay"] = max(
                    timeout_strategy.get("base_delay", 2.0), 2.0
                )
                manager.backoff.error_strategies["timeout"] = timeout_strategy

            if skip_health_check:
                LOGGER.info(
                    "Skipping pre-flight health check for %s (explicit override)",
                    SITE_DOMAIN,
                )
            else:
                try:
                    LOGGER.info(
                        "Checking %s availability (timeout=%.1fs)...",
                        SITE_DOMAIN,
                        health_check_timeout,
                    )
                    is_healthy = asyncio.run(
                        manager.check_domain_health(
                            SITE_DOMAIN, timeout=health_check_timeout
                        )
                    )
                except Exception as exc:  # pragma: no cover - defensive
                    LOGGER.warning(
                        "Health check raised %s; continuing without pre-flight guarantee",
                        exc,
                    )
                else:
                    if not is_healthy:
                        LOGGER.warning(
                            "Domain %s did not pass health check (continuing anyway)",
                            SITE_DOMAIN,
                        )
                    else:
                        LOGGER.info(
                            "Domain %s responded successfully, starting export...",
                            SITE_DOMAIN,
                        )

        fetcher = AsyncFetcher(
            HTTPClientConfig(
                concurrency=base_concurrency,
                timeout=30.0,
                max_retries=4,
                headers=dict(ANTIBOT_HEADERS),
            )
        )

        async def handler(
            client: httpx.AsyncClient, url: str
        ) -> Optional[Dict[str, Any]]:
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
                status_code = getattr(
                    getattr(exc, "response", None), "status_code", None
                )
                record_error_product(
                    writer,
                    domain=SITE_DOMAIN,
                    url=url,
                    status_code=status_code,
                    message=str(exc),
                    logger=LOGGER,
                )
                return None

            writer.append(product)
            return product

        processed_products: List[Dict[str, Any]] = []
        total_attempts = len(urls_to_fetch)
        progress_callback = make_cli_progress_callback(
            site=SITE_DOMAIN,
            script=SCRIPT_NAME,
            total=total_attempts,
        )

        try:
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
        finally:
            finalize_antibot_runtime(antibot_runtime)

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


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Ili-Ili fast exporter")
    parser.add_argument(
        "--concurrency", type=int, default=24, help="Max concurrent requests"
    )
    parser.add_argument("--limit", type=int, default=0, help="Limit product URLs")
    parser.add_argument(
        "--dry-run", action="store_true", help="Skip writing export files"
    )
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
        help="Ignore partial progress and start fresh",
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
    parser.add_argument(
        "--skip-health-check",
        action="store_true",
        help="Do not abort export when pre-flight health check fails",
    )
    parser.add_argument(
        "--health-timeout",
        type=float,
        default=20.0,
        help="Timeout (seconds) for pre-flight health check (default: 20)",
    )
    add_antibot_arguments(parser, default_enabled=True, default_concurrency=-1)
    return parser


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    acquire_process_lock(LOCK_FILE, logger=LOGGER)

    try:
        args = _build_arg_parser().parse_args()
        limit = args.limit if args.limit > 0 else None
        antibot_concurrency = args.antibot_concurrency
        if antibot_concurrency is not None and antibot_concurrency <= 0:
            antibot_concurrency = None
        _run(
            args.concurrency,
            limit,
            args.dry_run,
            resume=args.resume,
            resume_window_hours=args.resume_window_hours,
            skip_existing=args.skip_existing,
            use_antibot=args.use_antibot,
            antibot_concurrency=antibot_concurrency,
            antibot_timeout=float(args.antibot_timeout),
            skip_health_check=args.skip_health_check,
            health_check_timeout=float(args.health_timeout),
        )
    finally:
        release_process_lock(LOCK_FILE, logger=LOGGER)


if __name__ == "__main__":
    main()
