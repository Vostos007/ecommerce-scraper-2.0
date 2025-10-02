#!/usr/bin/env python3
"""Batch runner for MPYarn scraping with Firecrawl safeguard checks."""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from collections import OrderedDict
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from utils.firecrawl_summary import update_summary
from utils.export_writers import write_product_exports

URL_MAP_PATH = Path("data/sites/mpyarn.ru/mpyarn.ru.URL_map.json")

CACHE_PATH = Path("data/sites/mpyarn.ru/cache/mpyarn_urls.txt")
SITE_SCRIPT = Path("scripts/sites/mpyarn_ru.py")
EXPORT_PATH = Path("data/sites/mpyarn.ru/exports/httpx_latest.json")
DEFAULT_BATCH_SIZE = 100
DEFAULT_PAUSE_SECONDS = 10.0
EXPECTED_PRODUCT_COUNT = 856
FIRECRAWL_LIMIT_TOKENS = (
    "Firecrawl request limit reached",
    "Firecrawl request failed",
    "Firecrawl request limit",
)


def _normalize_product_url(raw_url: str) -> Optional[str]:
    candidate = raw_url.strip()
    if not candidate:
        return None
    # Strip query/fragment to keep canonical parent URL
    for token in ("#", "?"):
        if token in candidate:
            candidate = candidate.split(token, 1)[0]
    if "/magazin/product/" not in candidate:
        return None
    candidate = candidate.rstrip("/") + "/"
    return candidate


def _load_urls_from_map(map_path: Path) -> List[str]:
    if not map_path.exists():
        return []

    try:
        payload = json.loads(map_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[warn] unable to read URL map {map_path}: {exc}")
        return []

    urls: List[str] = []
    seen = set()

    entries = payload.get("links") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        print(f"[warn] URL map {map_path} has unexpected format; skipping")
        return []

    for entry in entries:
        if isinstance(entry, dict):
            raw_url = entry.get("url")
        else:
            raw_url = entry
        if not isinstance(raw_url, str):
            continue
        normalized = _normalize_product_url(raw_url)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        urls.append(normalized)

    return urls


def _persist_cache(urls: List[str]) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text("\n".join(urls) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"[warn] unable to persist cache {CACHE_PATH}: {exc}")


def load_product_urls() -> List[str]:
    urls = _load_urls_from_map(URL_MAP_PATH)
    if urls:
        _persist_cache(urls)
        return urls

    if not CACHE_PATH.exists():
        raise SystemExit(
            f"Cache file not found: {CACHE_PATH}; provide URL map or cache before running"
        )

    with CACHE_PATH.open("r", encoding="utf-8") as handle:
        return [
            normalized
            for line in handle
            if (normalized := _normalize_product_url(line))
        ]


def warn_if_mismatch(actual: int, expected: int) -> None:
    if expected <= 0:
        return
    if actual == expected:
        print(f"Product URL count matches expectation: {actual}")
    else:
        delta = actual - expected
        sign = "+" if delta > 0 else ""
        print(
            "[warn] cached product URLs differ from expectation "
            f"({actual} vs {expected}, delta {sign}{delta})"
        )


def _chunk_offsets(total: int, batch_size: int) -> Iterable[int]:
    if batch_size <= 0:
        raise ValueError("Batch size must be positive")
    total_batches = math.ceil(total / batch_size)
    return range(total_batches)


def _build_command(offset: int, batch_size: int) -> List[str]:
    return [
        sys.executable,
        str(SITE_SCRIPT),
        "--skip-cache-refresh",
        "--batch-size",
        str(batch_size),
        "--batch-offset",
        str(offset),
        "--batch-max",
        "1",
        "--max-products",
        "1000",
        "--backend",
        "httpx",
    ]


def _run_subprocess(cmd: List[str]) -> int:
    print(f"[spawn] {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    firecrawl_alert = False
    for line in proc.stdout:
        sys.stdout.write(line)
        if not firecrawl_alert and any(
            token in line for token in FIRECRAWL_LIMIT_TOKENS
        ):
            firecrawl_alert = True
    proc.stdout.close()
    return_code = proc.wait()
    if firecrawl_alert:
        print(
            "[warn] Firecrawl limit warning detected; consider reducing batch size or checking API credits"
        )
    return return_code


def dry_run(offsets: Iterable[int], batch_size: int, urls: List[str]) -> None:
    total = len(urls)
    print("Dry-run plan:")
    for offset in offsets:
        start = offset * batch_size
        end = min(start + batch_size, total)
        chunk = urls[start:end]
        print(
            f"  offset={offset:02d} size={len(chunk)} "
            f"urls={chunk[0] if chunk else '—'} .. {chunk[-1] if chunk else '—'}"
        )


def _load_products() -> List[dict]:
    if not EXPORT_PATH.exists():
        raise SystemExit(f"Export file not found: {EXPORT_PATH}")
    return json.loads(EXPORT_PATH.read_text(encoding="utf-8"))


def _merge_products(
    catalog: Dict[str, dict], batch_products: List[dict]
) -> None:
    for product in batch_products:
        if not isinstance(product, dict):
            continue
        url = product.get("url") or product.get("product_url")
        if not isinstance(url, str):
            continue
        catalog[url] = product


def main() -> None:
    parser = argparse.ArgumentParser(description="Run MPYarn batches sequentially")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument(
        "--start-offset", type=int, default=0, help="Batch offset to start from"
    )
    parser.add_argument(
        "--max-batches", type=int, default=0, help="Limit batches processed (0 = all)"
    )
    parser.add_argument(
        "--pause",
        type=float,
        default=DEFAULT_PAUSE_SECONDS,
        help="Pause between batches in seconds",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Show planned batches without executing"
    )
    parser.add_argument(
        "--skip-summary-update",
        action="store_true",
        help="Skip summary aggregation after successful run",
    )
    args = parser.parse_args()

    urls = load_product_urls()
    total_urls = len(urls)
    warn_if_mismatch(total_urls, EXPECTED_PRODUCT_COUNT)

    batch_size = max(args.batch_size, 1)
    offsets = list(_chunk_offsets(total_urls, batch_size))

    start = max(args.start_offset, 0)
    end = (
        len(offsets)
        if args.max_batches <= 0
        else min(len(offsets), start + max(args.max_batches, 0))
    )
    selected_offsets = offsets[start:end]

    print(
        f"Total product URLs: {total_urls}\n"
        f"Batch size: {batch_size}\n"
        f"Total batches: {len(offsets)}\n"
        f"Running offsets {start}..{end - 1 if end else start}\n"
    )

    if args.dry_run:
        dry_run(selected_offsets, batch_size, urls)
        return

    aggregated: Dict[str, dict] = OrderedDict()

    for offset in selected_offsets:
        cmd = _build_command(offset, batch_size)
        result = _run_subprocess(cmd)
        if result != 0:
            print(f"[error] batch offset {offset} exited with code {result}")
            return
        try:
            batch_products = _load_products()
        except Exception as exc:
            print(f"[warn] unable to load export after batch {offset}: {exc}")
        else:
            _merge_products(aggregated, batch_products)
        if args.pause > 0 and offset != selected_offsets[-1]:
            time.sleep(args.pause)

    print("All requested MPYarn batches completed")

    if args.skip_summary_update:
        return

    products = list(aggregated.values()) if aggregated else []
    if products:
        write_product_exports(products, EXPORT_PATH)
    else:
        try:
            products = _load_products()
        except Exception as exc:
            print(f"[warn] unable to load export for summary update: {exc}")
            return
        if not isinstance(products, list):
            print("[warn] export data not list; skipping summary update")
            return

    update_summary("mpyarn.ru", products, export_file=EXPORT_PATH.name, status="ok")
    print(f"Summary updated for domain mpyarn.ru using {EXPORT_PATH.name}")


if __name__ == "__main__":
    main()
