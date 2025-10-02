#!/usr/bin/env python3
"""Fast exporter for knitshop.ru using static HTML parsing."""

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

SITE_DOMAIN = "knitshop.ru"
SCRIPT_NAME = Path(__file__).stem
URL_MAP_PATH = Path("data/sites/knitshop.ru/Knitshop Data Sept 27 2025.json")
FILTERED_MAP_PATH = Path("data/sites/knitshop.ru/Knitshop Data filtered.json")
ACTIVE_URL_PATH = Path("data/sites/knitshop.ru/knitshop_active_urls.json")
EXPORT_PATH = Path("data/sites/knitshop.ru/exports/httpx_latest.json")
PARTIAL_PATH = Path("data/sites/knitshop.ru/temp/httpx_partial.jsonl")
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
        if parsed.netloc.lower() == SITE_DOMAIN:
            host = SITE_DOMAIN
        else:
            return None

    path = parsed.path or "/"
    if not path.startswith("/"):
        path = f"/{path}"
    # Нормализация множественных слэшей
    path = re.sub(r"/+", "/", path)
    if not path.endswith("/"):
        path = f"{path}/"

    segments = [segment for segment in path.split("/") if segment]
    if len(segments) < 2:
        return None
    if segments[0] != "catalog":
        return None
    if not segments[-1].isdigit():
        return None

    normalized = f"https://www.{SITE_DOMAIN}{path}"
    return normalized


def _load_product_urls(limit: Optional[int] = None) -> List[str]:
    def _read_json(path: Path) -> Optional[List[str]]:
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            LOGGER.warning("Failed to decode URL file %s", path)
            return None
        urls: List[str] = []
        if isinstance(payload, dict) and isinstance(payload.get("links"), list):
            for item in payload["links"]:
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


def _extract_product(html: str, url: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")

    name_node = soup.select_one("h1")
    title_node = soup.select_one("title")
    meta_desc = soup.find("meta", attrs={"name": "description"})

    price_node = soup.select_one("div[id^='bx_'][id$='_price']")
    price: Optional[float] = None
    if price_node:
        price_match = re.search(r"([0-9]+[0-9\s.,]*)", price_node.get_text())
        if price_match:
            price = _safe_float(price_match.group(1))

    stock_node = soup.select_one("div[id^='bx_'][id$='_store_quantity']")
    stock: Optional[float] = None
    if stock_node:
        stock_match = re.search(r"\(([^)]+)\)", stock_node.get_text())
        if stock_match:
            stock = _safe_float(stock_match.group(1))
        elif "нет" in stock_node.get_text(strip=True).lower():
            stock = 0.0

    if stock is None:
        max_node = soup.select_one("span[id^='bx_'][id$='_quant_up']")
        if max_node and max_node.has_attr("data-max"):
            stock = _safe_float(max_node.get("data-max"))

    in_stock = bool(stock is None or (isinstance(stock, (int, float)) and stock > 0))
    if stock is None:
        stock = 1.0 if in_stock else 0.0

    product: Dict[str, Any] = {
        "url": url,
        "original_url": url,
        "name": name_node.get_text(strip=True) if name_node else None,
        "price": price,
        "base_price": price,
        "currency": "RUB",
        "stock": stock,
        "stock_quantity": stock,
        "in_stock": in_stock,
        "variations": [],
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
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) knitshop-fast-export/1.0",
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

    antibot_runtime: Optional[Any] = None
    if use_antibot:
        antibot_runtime = create_antibot_runtime(
            enabled=True,
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
            LOGGER.warning("Failed to fetch %s URLs", len(failures))
            for failure in failures[:10]:
                LOGGER.warning(
                    "  %s -> %s %s",
                    failure.get("url"),
                    failure.get("status"),
                    failure.get("error"),
                )
            failure_log = PARTIAL_PATH.parent / "knitshop_failures.json"
            failure_log.parent.mkdir(parents=True, exist_ok=True)
            failure_log.write_text(
                json.dumps(failures, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    finally:
        writer.close()
        finalize_antibot_runtime(antibot_runtime)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Knitshop.ru fast exporter")
    parser.add_argument("--concurrency", type=int, default=6, help="Max concurrent requests")
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
