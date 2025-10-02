"""Helpers for computing Firecrawl export metrics and maintaining summaries."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, MutableMapping, Optional

import fcntl

SUMMARY_PATH = Path("reports/firecrawl_baseline_summary.json")


@dataclass
class FirecrawlMetrics:
    status: str
    export_file: str
    products: int
    products_with_price: int
    products_with_stock_field: int
    products_in_stock_true: int
    products_total_stock: float
    products_with_variations: int
    total_variations: int
    variations_total_stock: float
    variations_in_stock_true: int
    success_rate: Optional[float] = None
    updated_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "status": self.status,
            "export_file": self.export_file,
            "products": self.products,
            "products_with_price": self.products_with_price,
            "products_with_stock_field": self.products_with_stock_field,
            "products_in_stock_true": self.products_in_stock_true,
            "products_total_stock": round(self.products_total_stock, 2),
            "products_with_variations": self.products_with_variations,
            "total_variations": self.total_variations,
            "variations_total_stock": round(self.variations_total_stock, 2),
            "variations_in_stock_true": self.variations_in_stock_true,
        }
        if self.success_rate is not None:
            payload["success_rate"] = round(self.success_rate, 2)
        payload["updated_at"] = self.updated_at or datetime.now(timezone.utc).isoformat()
        return payload


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().replace(" ", "")
        if not cleaned:
            return None
        try:
            cleaned = cleaned.replace(",", ".")
            return float(cleaned)
        except ValueError:
            return None
    return None


def _load_json(path: Path) -> Any:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def compute_metrics(
    products: List[Dict[str, Any]],
    *,
    export_file: str,
    status: str = "ok",
    success_rate: Optional[float] = None,
    updated_at: Optional[str] = None,
) -> FirecrawlMetrics:
    products_count = 0
    products_with_price = 0
    products_with_stock_field = 0
    products_in_stock_true = 0
    products_total_stock = 0.0
    products_with_variations = 0
    total_variations = 0
    variations_total_stock = 0.0
    variations_in_stock_true = 0

    for product in products:
        if not isinstance(product, dict):
            continue
        products_count += 1
        if product.get("price") is not None:
            products_with_price += 1
        if product.get("in_stock") is True:
            products_in_stock_true += 1

        stock_fields = [
            product.get("stock_quantity"),
            product.get("stock"),
            product.get("inventory"),
            product.get("available"),
            product.get("quantity"),
        ]
        stock_value = None
        for candidate in stock_fields:
            stock_value = _to_float(candidate)
            if stock_value is not None:
                break
        if stock_value is not None:
            products_with_stock_field += 1
            products_total_stock += stock_value

        variations = product.get("variations")
        if isinstance(variations, list) and variations:
            products_with_variations += 1
            total_variations += len(variations)
            for variation in variations:
                if not isinstance(variation, dict):
                    continue
                var_stock = _to_float(variation.get("stock"))
                if var_stock is not None:
                    variations_total_stock += var_stock
                if variation.get("in_stock") is True:
                    variations_in_stock_true += 1
        else:
            variations = []

    success_rate_value = None
    if success_rate is not None:
        clamped = max(0.0, min(success_rate, 1.0))
        success_rate_value = clamped * 100.0

    timestamp = updated_at or datetime.now(timezone.utc).isoformat()

    return FirecrawlMetrics(
        status=status,
        export_file=export_file,
        products=products_count,
        products_with_price=products_with_price,
        products_with_stock_field=products_with_stock_field,
        products_in_stock_true=products_in_stock_true,
        products_total_stock=products_total_stock,
        products_with_variations=products_with_variations,
        total_variations=total_variations,
        variations_total_stock=variations_total_stock,
        variations_in_stock_true=variations_in_stock_true,
        success_rate=success_rate_value,
        updated_at=timestamp,
    )


def _acquire_lock(path: Path):
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("w")
    fcntl.flock(handle, fcntl.LOCK_EX)
    return handle


def _write_json_atomic(path: Path, payload: MutableMapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fd, temp_path = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_path)


def update_summary(
    domain: str,
    products: List[Dict[str, Any]],
    *,
    export_file: str,
    status: str = "ok",
    success_rate: Optional[float] = None,
    summary_path: Path = SUMMARY_PATH,
) -> None:
    metrics = compute_metrics(
        products,
        export_file=export_file,
        status=status,
        success_rate=success_rate,
    )
    summary: MutableMapping[str, Any]
    lock_handle = _acquire_lock(summary_path)
    try:
        summary_data = _load_json(summary_path)
        if isinstance(summary_data, dict):
            summary = summary_data
        else:
            summary = {}
        summary[domain] = metrics.to_dict()
        _write_json_atomic(summary_path, summary)
    finally:
        fcntl.flock(lock_handle, fcntl.LOCK_UN)
        lock_handle.close()


__all__ = ["compute_metrics", "update_summary", "FirecrawlMetrics"]
