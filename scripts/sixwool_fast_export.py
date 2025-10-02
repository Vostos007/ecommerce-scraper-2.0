#!/usr/bin/env python3
"""Fast exporter for 6wool.ru leveraging Antibot fallback + FlareSolverr."""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from parsers.variation_parser import VariationParser

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
    load_export_products,
    load_url_map,
    load_url_map_with_fallback,
    merge_products,
    prepare_incremental_writer,
    prime_writer_from_export,
    record_error_product,
    release_process_lock,
    request_with_retries,
    use_export_context,
)

LOGGER = logging.getLogger(__name__)

SITE_DOMAIN = "6wool.ru"
SCRIPT_NAME = Path(__file__).stem
CONFIG_PATH = Path("config/settings.json")
EXPORT_PATH = Path("data/sites/6wool.ru/exports/httpx_latest.json")
PARTIAL_PATH = Path("data/sites/6wool.ru/temp/httpx_partial.jsonl")
LOCK_FILE = Path(f"/tmp/export_{SITE_DOMAIN.replace('.', '_')}.lock")

URL_MAP_FILES: Sequence[Path] = (
    Path("data/sites/6wool.ru/Wool Catalog Sept 26 2025.json"),
    Path("data/sites/6wool.ru/Wool Accessories Catalog Sept 26 2025.json"),
    Path("data/sites/6wool.ru/Literature Catalog Sept 26 2025.json"),
    Path("data/sites/6wool.ru/Инструменты 2025-09-26.json"),
    Path("data/sites/6wool.ru/Пуговицы 2025-09-26.json"),
)


def _normalize_product_url(url: str) -> Optional[str]:
    candidate = url.strip()
    if not candidate:
        return None
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"}:
        return None
    # drop query/fragment to avoid duplicate entries (?offer, pagination)
    normalized = parsed._replace(query="", fragment="")
    path = normalized.path or "/"
    if not path.endswith("/"):
        path = f"{path}/"
    return normalized._replace(path=path).geturl()


def _is_product_path(path: str) -> bool:
    segments = [segment for segment in path.split("/") if segment]
    return len(segments) >= 3 and "catalog" in segments


def _is_candidate_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.netloc.lower().endswith(SITE_DOMAIN):
        return _is_product_path(parsed.path or "")
    return False


def load_catalog_urls(limit: Optional[int] = None) -> List[str]:
    """Load product URLs from all cached maps (deduplicated & filtered)."""

    collected: List[str] = []

    fallback_urls = load_url_map_with_fallback(
        SITE_DOMAIN,
        allowed_domains={SITE_DOMAIN},
        include_predicate=_is_candidate_url,
        normalize=_normalize_product_url,
    )
    collected.extend(fallback_urls)

    for path in URL_MAP_FILES:
        if not path.exists():
            LOGGER.warning("URL map missing: %s", path)
            continue
        try:
            urls = load_url_map(
                path,
                allowed_domains={SITE_DOMAIN},
                include_predicate=_is_candidate_url,
                normalize=_normalize_product_url,
            )
        except Exception as exc:  # pragma: no cover - defensive guard
            LOGGER.warning("Failed to load %s: %s", path, exc)
            continue
        collected.extend(urls)

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique_urls: List[str] = []
    for url in collected:
        if url in seen:
            continue
        seen.add(url)
        unique_urls.append(url)

    if limit is not None and limit > 0:
        return unique_urls[:limit]
    return unique_urls


def _extract_product_id(soup: BeautifulSoup, url: str) -> Optional[str]:
    attr_nodes = soup.select("[data-product-id], input[name='PRODUCT_ID'], input[name='product_id']")
    for node in attr_nodes:
        value = node.get("data-product-id") or node.get("value")
        if value and str(value).strip():
            return str(value).strip()

    parsed = urlparse(url)
    segments = [segment for segment in parsed.path.split("/") if segment]
    if segments:
        last = segments[-1]
        if last.isdigit():
            return last
    return None


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_product_html(
    html: str,
    url: str,
    parser: VariationParser,
    selectors: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Parse 6wool product HTML into export payload."""

    soup = BeautifulSoup(html, "lxml")

    canonical = soup.find("link", rel="canonical")
    canonical_url = canonical.get("href") if canonical else url

    name_node = soup.select_one("h1")
    title = soup.select_one("title")
    meta_desc = soup.find("meta", attrs={"name": "description"})

    parser._current_url = canonical_url or url
    cms_type, selectors_bundle = parser.detect_cms_and_get_selectors(
        url=canonical_url or url,
        html=html,
    )
    # Ensure sixwool selectors are available even if detection fallback triggered
    cms_selectors = selectors or selectors_bundle

    price = parser.extract_price_static(html, cms_selectors)
    base_stock = parser.extract_stock_static(html, cms_selectors)
    variations = parser._parse_sixwool_variations(html, cms_selectors)

    total_stock = 0.0
    in_stock = False
    if variations:
        for variation in variations:
            stock_value = _to_float(variation.get("stock"))
            if stock_value is None and variation.get("in_stock"):
                stock_value = 1.0
            if stock_value is not None:
                total_stock += max(stock_value, 0.0)
            if variation.get("in_stock"):
                in_stock = True
            if variation.get("price") is None and price is not None:
                variation["price"] = price
            if variation.get("currency") is None:
                variation["currency"] = parser._resolve_currency_override(url)
    else:
        stock_candidate = _to_float(base_stock)
        if stock_candidate is not None:
            total_stock = stock_candidate
            in_stock = stock_candidate > 0
        else:
            total_stock = binary_stock(True)
            in_stock = True

    if not variations and price is None:
        price = parser.extract_price_static(html)

    currency = parser._resolve_currency_override(url) or "RUB"

    product: Dict[str, Any] = {
        "url": canonical_url or url,
        "original_url": url,
        "name": name_node.get_text(strip=True) if name_node else None,
        "price": price,
        "base_price": price,
        "currency": currency,
        "stock": round(total_stock, 2) if total_stock else total_stock,
        "stock_quantity": round(total_stock, 2) if total_stock else total_stock,
        "in_stock": in_stock,
        "variations": variations,
        "product_id": _extract_product_id(soup, canonical_url or url),
        "seo_h1": name_node.get_text(strip=True) if name_node else None,
        "seo_title": title.get_text(strip=True) if title else None,
        "seo_meta_description": meta_desc.get("content") if meta_desc else None,
        "site_domain": SITE_DOMAIN,
    }
    return product


async def fetch_product(
    client: httpx.AsyncClient,
    parser: VariationParser,
    url: str,
    *,
    request_headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Fetch and parse a single 6wool product URL."""

    response = await request_with_retries(
        client,
        "GET",
        url,
        follow_redirects=True,
        headers=request_headers,
    )
    return parse_product_html(response.text, url, parser)


async def _run_async(
    concurrency: int,
    limit: Optional[int],
    dry_run: bool,
    *,
    resume: bool,
    resume_window_hours: Optional[int],
    skip_existing: bool,
    antibot_runtime: Optional[Any],
) -> None:
    urls = load_catalog_urls(limit)
    if not urls:
        LOGGER.warning("No candidate URLs discovered for %s", SITE_DOMAIN)
        return

    writer, existing_partial = prepare_incremental_writer(
        PARTIAL_PATH,
        resume=resume,
        resume_window_hours=resume_window_hours,
    )

    if existing_partial:
        LOGGER.info("Resume: loaded %s partial products", len(existing_partial))

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

    parser = VariationParser(
        antibot_manager=getattr(antibot_runtime, "manager", None),
        cms_type="sixwool",
    )

    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) sixwool-fast-export/1.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru,en;q=0.8",
    }

    client_config = HTTPClientConfig(
        concurrency=max(concurrency, 1),
        timeout=30.0,
        headers=headers,
    )
    fetcher = AsyncFetcher(client_config)

    async def handler(client: httpx.AsyncClient, url: str) -> Optional[Dict[str, Any]]:
        try:
            product = await fetch_product(
                client,
                parser,
                url,
                request_headers=headers,
            )
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
        await asyncio.sleep(0.05)
        return product

    processed_products: List[Dict[str, Any]] = []
    total_attempts = len(urls_to_fetch)
    progress_callback = make_cli_progress_callback(
        site=SITE_DOMAIN,
        script=SCRIPT_NAME,
        total=total_attempts,
    )

    try:
        if urls_to_fetch:
            processed_products = await fetcher.run(
                urls_to_fetch,
                handler,
                progress_callback=progress_callback,
                progress_total=total_attempts,
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
    fallback_concurrency = antibot_concurrency
    if fallback_concurrency is None or fallback_concurrency <= 0:
        fallback_concurrency = max(1, concurrency // 4) or 1

    antibot_runtime = create_antibot_runtime(
        enabled=use_antibot,
        config_path=CONFIG_PATH,
        concurrency=fallback_concurrency,
        timeout=antibot_timeout,
    )

    try:
        with use_export_context(antibot=antibot_runtime):
            asyncio.run(
                _run_async(
                    concurrency,
                    limit,
                    dry_run,
                    resume=resume,
                    resume_window_hours=resume_window_hours,
                    skip_existing=skip_existing,
                    antibot_runtime=antibot_runtime,
                )
            )
    finally:
        finalize_antibot_runtime(antibot_runtime)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="6wool.ru fast exporter")
    parser.add_argument("--concurrency", type=int, default=3, help="Max concurrent requests (default: 3)")
    parser.add_argument("--limit", type=int, default=0, help="Optional limit of product URLs")
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
        help="Skip URLs already present in existing export",
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
        try:
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
        except KeyboardInterrupt:  # pragma: no cover - CLI convenience
            LOGGER.warning("Interrupted by user")
    finally:
        release_process_lock(LOCK_FILE, logger=LOGGER)


if __name__ == "__main__":
    main()
