#!/usr/bin/env python3
"""Build a full ili-ili.com export (JSON + Excel) from history analytics."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import sys
if __package__ is None or __package__ == "":
    sys.path.append(str(Path(__file__).resolve().parents[1]))

from utils.export_writers import write_product_exports

ANALYTICS_PATH = Path("data/sites/ili-ili.com/history/history.analytics.json")
EXPORT_JSON_PATH = Path("data/sites/ili-ili.com/exports/latest.json")
SITE_DOMAIN = "ili-ili.com"


def parse_latest_snapshot(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Extract the most recent price/stock snapshot for a product."""
    price_keys = sorted(
        [key for key in entry if key.startswith("price_")],
        key=lambda key: key.split("_", 1)[1],
        reverse=True,
    )

    latest_price = None
    latest_stock = None
    latest_date = None

    for price_key in price_keys:
        date_part = price_key.split("_", 1)[1]
        stock_key = f"stock_{date_part}"
        price_value = entry.get(price_key)
        stock_value = entry.get(stock_key)
        if price_value is not None:
            latest_price = float(price_value)
            latest_stock = int(stock_value) if isinstance(stock_value, (int, float)) else None
            latest_date = date_part
            break

    if latest_price is None:
        return None

    scraped_at = (
        datetime.strptime(latest_date, "%Y-%m-%d").isoformat() + "Z"
        if latest_date
        else None
    )

    product = {
        "name": entry.get("product_name", ""),
        "url": entry.get("product_url"),
        "price": latest_price,
        "base_price": latest_price,
        "stock": latest_stock if latest_stock is not None else 0,
        "in_stock": latest_stock is None or latest_stock > 0,
        "scraped_at": scraped_at,
        "site_domain": SITE_DOMAIN,
        "variations": [],
    }
    return product if product["url"] else None


def main() -> None:
    if not ANALYTICS_PATH.exists():
        raise SystemExit(f"Analytics file not found: {ANALYTICS_PATH}")

    entries = json.loads(ANALYTICS_PATH.read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        raise SystemExit("Unexpected analytics format")

    products: List[Dict[str, Any]] = []
    for entry in entries:
        product = parse_latest_snapshot(entry)
        if product:
            products.append(product)

    if not products:
        raise SystemExit("No products with price data found")

    write_product_exports(products, EXPORT_JSON_PATH)
    print(f"Exported {len(products)} products to {EXPORT_JSON_PATH}")


if __name__ == "__main__":
    main()
