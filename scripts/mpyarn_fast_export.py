#!/usr/bin/env python3
"""Fast MPYarn catalog exporter using Shop2 AJAX endpoints."""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import logging
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import httpx
from bs4 import BeautifulSoup

from scripts import run_mpyarn_batches
from scripts.fast_export_base import (
    IncrementalWriter,
    NotFoundError,
    add_antibot_arguments,
    acquire_process_lock,
    create_antibot_runtime,
    export_products,
    finalize_antibot_runtime,
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

# ---------------------------------------------------------------------------
# Configuration ----------------------------------------------------------------
EXPORT_PATH = Path("data/sites/mpyarn.ru/exports/httpx_latest.json")
SITE_DOMAIN = "mpyarn.ru"
PARTIAL_PATH = Path("data/sites/mpyarn.ru/temp/httpx_partial.jsonl")
LOCK_FILE = Path(f"/tmp/export_{SITE_DOMAIN.replace('.', '_')}.lock")
CONFIG_PATH = Path("config/settings.json")


LOGGER = logging.getLogger(__name__)


@dataclass
class Variant:
    variant_id: str
    value: str
    price: Optional[float]
    in_stock: bool
    stock_level: Optional[float]
    currency: Optional[str]
    min_quantity: Optional[int]
    step_quantity: Optional[int]
    article: Optional[str]
    meta: Dict[str, Any]

    def to_dict(self, sort_order: int) -> Dict[str, Any]:
        attributes: Dict[str, Any] = {
            "name": self.value,
            "min_quantity": self.min_quantity,
            "step_quantity": self.step_quantity,
        }
        if self.article:
            attributes["article"] = self.article
        if self.meta:
            attributes.update(self.meta)

        stock_value = self.stock_level
        return {
            "type": "variant",
            "value": self.value,
            "option_id": self.variant_id,
            "price": self.price,
            "stock": stock_value,
            "stock_quantity": stock_value,
            "in_stock": self.in_stock,
            "display_name": self.value,
            "sort_order": sort_order,
            "category": "variant",
            "confidence_score": 0.9,
            "sku": self.article,
            "variant_id": self.variant_id,
            "currency": self.currency,
            "attributes": attributes,
        }


# ---------------------------------------------------------------------------
# Helpers ---------------------------------------------------------------------


def _collect_kind_ids(node: Any) -> Iterable[str]:
    if isinstance(node, dict):
        for value in node.values():
            yield from _collect_kind_ids(value)
    elif isinstance(node, (list, tuple, set)):
        for value in node:
            yield from _collect_kind_ids(value)
    elif isinstance(node, str):
        yield node


def _parse_shop2_config(html_text: str) -> Dict[str, Any]:
    marker = "shop2.init("
    start = html_text.find(marker)
    if start == -1:
        raise ValueError("shop2.init payload not found")
    depth = 0
    in_string = False
    escape = False
    for index in range(start + len(marker), len(html_text)):
        char = html_text[index]
        if char == "\"" and not escape:
            in_string = not in_string
        escape = char == "\\" and not escape and in_string
        if in_string:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                payload = html_text[start + len(marker) : index + 1]
                return json.loads(payload)
    raise ValueError("Failed to parse shop2.init payload")


def _extract_text(element: Optional[BeautifulSoup]) -> Optional[str]:
    if element is None:
        return None
    return element.get_text(strip=True) or None


def _safe_float(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        normalized = value.replace(" ", "").replace(",", ".")
        return float(normalized)
    except (ValueError, AttributeError):
        return None


async def _fetch_product_html(client: httpx.AsyncClient, url: str) -> str:
    response = await request_with_retries(
        client,
        "GET",
        url,
        follow_redirects=True,
    )
    return response.text


async def _fetch_variant_html(
    client: httpx.AsyncClient,
    *,
    product_url: str,
    product_id: str,
    kind_id: str,
    api_hash: str,
    ver_id: Any,
) -> str:
    payload = {
        "product_id": product_id,
        "kind_id": kind_id,
    }
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Referer": product_url,
    }
    response = await request_with_retries(
        client,
        "POST",
        f"https://{SITE_DOMAIN}/-/shop2-api/?cmd=getProductListItem&hash={api_hash}&ver_id={ver_id}",
        data=payload,
        headers=headers,
        follow_redirects=True,
    )
    payload_json = response.json()
    body = payload_json.get("data", {}).get("body")
    if not isinstance(body, str):
        raise ValueError("Unexpected variant payload structure")
    return body


def _parse_variant(body_html: str, kind_id: str) -> Variant:
    soup = BeautifulSoup(body_html, "html.parser")

    price_el = soup.select_one(".price-current strong")
    currency_el = soup.select_one(".price-current span")
    button_el = soup.select_one(".shop-product-btn.buy")
    article_el = soup.select_one(".product-article")
    amount_el = soup.select_one("input[name='amount']")
    meta_el = soup.find("input", {"name": "meta"})

    article_text = None
    if article_el:
        article_text = article_el.get_text(separator=" ", strip=True)
        if ":" in article_text:
            article_text = article_text.split(":", 1)[-1].strip()

    min_qty = None
    step_qty = None
    if amount_el:
        min_qty = amount_el.get("data-min")
        step_qty = amount_el.get("data-multiplicity")
        min_qty = int(min_qty) if min_qty and min_qty.isdigit() else None
        step_qty = int(step_qty) if step_qty and step_qty.isdigit() else None

    meta: Dict[str, Any] = {}
    if meta_el and meta_el.get("value"):
        try:
            meta = json.loads(html.unescape(meta_el["value"]))
        except json.JSONDecodeError:
            meta = {}

    value = meta.get("name") or article_text or "Variant"

    stock_level = 1.0 if button_el else 0.0

    return Variant(
        variant_id=str(kind_id),
        value=value,
        price=_safe_float(_extract_text(price_el)),
        in_stock=bool(button_el and "disabled" not in button_el.attrs),
        stock_level=stock_level,
        currency=_extract_text(currency_el),
        min_quantity=min_qty,
        step_quantity=step_qty,
        article=article_text,
        meta={k: v for k, v in meta.items() if k not in {"name"}},
    )


def _parse_product_meta(html_text: str, url: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html_text, "html.parser")
    name = _extract_text(soup.select_one("h1")) or url.rstrip("/").split("/")[-1]
    price = _safe_float(_extract_text(soup.select_one(".price-current strong")))
    currency = _extract_text(soup.select_one(".price-current span"))
    in_stock = bool(soup.select_one(".shop-product-btn.buy"))
    seo_title = _extract_text(soup.find("title"))
    seo_desc = None
    meta_desc = soup.find("meta", attrs={"name": "description"})
    if meta_desc and meta_desc.get("content"):
        seo_desc = meta_desc["content"].strip()

    return {
        "url": url,
        "name": name,
        "price": price,
        "base_price": price,
        "stock": None,
        "in_stock": in_stock,
        "stock_quantity": None,
        "currency": currency,
        "seo_h1": name,
        "seo_title": seo_title,
        "seo_meta_description": seo_desc,
    }


async def _scrape_product(
    client: httpx.AsyncClient,
    url: str,
    *,
    semaphore: asyncio.Semaphore,
) -> Dict[str, Any]:
    async with semaphore:
        try:
            product_html = await _fetch_product_html(client, url)
        except NotFoundError:
            LOGGER.info("Skipping %s (404 not found)", url)
            raise

    product_data = _parse_product_meta(product_html, url)

    base_stock = 1.0 if product_data.get("in_stock") else 0.0

    try:
        shop_config = _parse_shop2_config(product_html)
    except ValueError as exc:
        product_data["error"] = str(exc)
        product_data["variations"] = []
        product_data["stock"] = base_stock
        product_data["stock_quantity"] = base_stock
        return product_data

    product_refs = shop_config.get("productRefs", {})
    if not isinstance(product_refs, dict) or not product_refs:
        product_data["variations"] = []
        product_data["stock"] = base_stock
        product_data["stock_quantity"] = base_stock
        return product_data

    product_id = next(iter(product_refs.keys()))
    hash_value = shop_config.get("apiHash", {}).get("getProductListItem")
    ver_id = shop_config.get("verId")

    kind_ids = OrderedDict()
    for ref_block in product_refs.get(product_id, {}).values():
        for kind_id in _collect_kind_ids(ref_block):
            kind_ids[str(kind_id)] = None

    variants: List[Variant] = []
    if hash_value and ver_id and kind_ids:
        for index, kind_id in enumerate(kind_ids.keys()):
            variant_html = await _fetch_variant_html(
                client,
                product_url=url,
                product_id=product_id,
                kind_id=kind_id,
                api_hash=hash_value,
                ver_id=ver_id,
            )
            variants.append(_parse_variant(variant_html, kind_id))

    product_data["variations"] = [variant.to_dict(idx) for idx, variant in enumerate(variants)]

    if variants:
        prices = [variant.price for variant in variants if variant.price is not None]
        product_data["price"] = prices[0] if prices else product_data.get("price")
        product_data["base_price"] = product_data["price"]
        product_data["in_stock"] = any(variant.in_stock for variant in variants)
        total_stock = sum(variant.stock_level or 0 for variant in variants)
    else:
        total_stock = 1.0 if product_data.get("in_stock") else 0.0

    product_data["stock"] = total_stock
    product_data["stock_quantity"] = total_stock

    product_data["scraped_at"] = datetime.now(timezone.utc).isoformat()
    product_data["error"] = None
    product_data["site_domain"] = SITE_DOMAIN
    return product_data


async def scrape_catalog(
    urls: List[str],
    *,
    concurrency: int,
    writer: IncrementalWriter,
) -> int:
    if not urls:
        return 0

    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    timeout = httpx.Timeout(30.0, read=30.0)
    semaphore = asyncio.Semaphore(concurrency)
    appended = 0

    async with httpx.AsyncClient(base_url=f"https://{SITE_DOMAIN}", limits=limits, timeout=timeout) as client:
        async def wrapped(url: str) -> tuple[str, Optional[Dict[str, Any]], Optional[Exception]]:
            try:
                product = await _scrape_product(client, url, semaphore=semaphore)
            except Exception as exc:  # pragma: no cover - propagate to handler
                return url, None, exc
            return url, product, None

        tasks = [asyncio.create_task(wrapped(url)) for url in urls]
        for task in asyncio.as_completed(tasks):
            url, product, error = await task
            if error is not None:
                if isinstance(error, NotFoundError):
                    status_code = error.status_code
                else:
                    status_code = getattr(getattr(error, "response", None), "status_code", None)
                record_error_product(
                    writer,
                    domain=SITE_DOMAIN,
                    url=url,
                    status_code=status_code,
                    message=str(error),
                    logger=LOGGER,
                )
                continue
            if not product:
                continue
            product_url = product.get("url")
            if isinstance(product_url, str) and product_url in writer.processed_urls:
                LOGGER.debug("Skipping already processed product %s", product_url)
                continue
            writer.append(product)
            appended += 1

    return appended


# ---------------------------------------------------------------------------
# CLI ------------------------------------------------------------------------


def _load_urls() -> List[str]:
    return run_mpyarn_batches.load_product_urls()


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
    urls = _load_urls()
    if limit is not None and limit > 0:
        urls = urls[:limit]

    LOGGER.info("Total candidate URLs: %s", len(urls))

    writer, existing_products = prepare_incremental_writer(
        PARTIAL_PATH,
        resume=resume,
        resume_window_hours=resume_window_hours,
    )

    try:
        if existing_products:
            LOGGER.info("Resume: loaded %s products from partial", len(existing_products))

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
        skipped = len(urls) - len(urls_to_fetch)
        LOGGER.info(
            "To fetch: %s URLs (skipped: %s already processed)",
            len(urls_to_fetch),
            skipped,
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
                new_count = asyncio.run(
                    scrape_catalog(
                        urls_to_fetch,
                        concurrency=concurrency,
                        writer=writer,
                    )
                )
        finally:
            finalize_antibot_runtime(antibot_runtime)

        LOGGER.info("Scraped %s new products", new_count)

        products = writer.finalize()
        if skip_existing and existing_export_products:
            products = merge_products(existing_export_products, products)
        LOGGER.info("Combined product count: %s", len(products))

        if dry_run:
            LOGGER.info("Dry run: skipping export write, removing partial")
            writer.cleanup()
            return

        total_attempts = len(urls_to_fetch)
        success_ratio = new_count / total_attempts if total_attempts > 0 else None

        artifacts = export_products(
            SITE_DOMAIN,
            EXPORT_PATH,
            products,
            success_rate=success_ratio,
        )
        LOGGER.info("JSON export: %s", artifacts.json_path)
        if artifacts.csv_paths:
            LOGGER.info(
                "CSV exports: %s",
                ", ".join(path.name for path in artifacts.csv_paths.values()),
            )
        if artifacts.excel_path:
            LOGGER.info("Excel export: %s", artifacts.excel_path)
        writer.cleanup()
    finally:
        writer.close()


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fast MPYarn exporter via Shop2 API")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=64,
        help="Maximum concurrent requests (default: 64)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Limit number of products for debugging",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and parse products but skip writing exports",
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
        help="Ignore partial progress and start from scratch",
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
            concurrency=args.concurrency,
            limit=limit,
            dry_run=args.dry_run,
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
