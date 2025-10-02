#!/usr/bin/env python3
"""Utility for validating export datasets and comparing with baseline snapshots."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd


PRICE_COLUMNS = [
    "variation_price",
    "price",
    "base_price",
]

STOCK_COLUMNS = [
    "variation_stock",
    "stock",
    "total",
]


@dataclass
class ExportMetrics:
    rows: int
    unique_products: int
    variation_rows: int
    price_missing: int
    price_zero: int
    stock_missing: int
    stock_zero: int

    @property
    def price_missing_pct(self) -> float:
        return (self.price_missing / self.rows * 100) if self.rows else 0.0

    @property
    def stock_missing_pct(self) -> float:
        return (self.stock_missing / self.rows * 100) if self.rows else 0.0

    @property
    def price_zero_pct(self) -> float:
        return (self.price_zero / self.rows * 100) if self.rows else 0.0

    @property
    def stock_zero_pct(self) -> float:
        return (self.stock_zero / self.rows * 100) if self.rows else 0.0

    def to_dict(self) -> Dict[str, float]:
        return {
            "rows": self.rows,
            "unique_products": self.unique_products,
            "variation_rows": self.variation_rows,
            "price_missing": self.price_missing,
            "price_missing_pct": round(self.price_missing_pct, 2),
            "price_zero": self.price_zero,
            "price_zero_pct": round(self.price_zero_pct, 2),
            "stock_missing": self.stock_missing,
            "stock_missing_pct": round(self.stock_missing_pct, 2),
            "stock_zero": self.stock_zero,
            "stock_zero_pct": round(self.stock_zero_pct, 2),
        }


def resolve_series(frame: pd.DataFrame, columns: List[str]) -> pd.Series:
    available = [col for col in columns if col in frame.columns]
    if not available:
        return pd.Series([pd.NA] * len(frame))
    resolved = frame[available].bfill(axis=1)
    return resolved.iloc[:, 0]


def compute_metrics(frame: pd.DataFrame) -> ExportMetrics:
    rows = len(frame)
    unique_products = frame["url"].nunique() if "url" in frame.columns else rows
    variation_rows = (
        frame["row_type"].str.lower().eq("variation").sum()
        if "row_type" in frame.columns
        else rows
    )

    price_series = resolve_series(frame, PRICE_COLUMNS)
    stock_series = resolve_series(frame, STOCK_COLUMNS)

    price_missing = int(price_series.isna().sum())
    stock_missing = int(stock_series.isna().sum())

    price_filled = price_series.fillna(0)
    stock_filled = stock_series.fillna(0)

    price_zero = int((price_filled <= 0).sum())
    stock_zero = int((stock_filled <= 0).sum())

    return ExportMetrics(
        rows=rows,
        unique_products=int(unique_products),
        variation_rows=int(variation_rows),
        price_missing=price_missing,
        price_zero=price_zero,
        stock_missing=stock_missing,
        stock_zero=stock_zero,
    )


def load_export(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Export file not found: {path}")
    return pd.read_excel(path)


def delta(current: ExportMetrics, baseline: ExportMetrics) -> Dict[str, float]:
    diff: Dict[str, float] = {}
    current_dict = current.to_dict()
    baseline_dict = baseline.to_dict()
    for key, current_value in current_dict.items():
        diff[key] = round(current_value - baseline_dict.get(key, 0), 2)
    return diff


def render_section(title: str, metrics: ExportMetrics) -> str:
    data = metrics.to_dict()
    lines = [f"### {title}", "", "| Metric | Value |", "| --- | --- |"]
    for key, value in data.items():
        lines.append(f"| {key} | {value} |")
    lines.append("")
    return "\n".join(lines)


def render_delta(diff: Dict[str, float]) -> str:
    lines = ["### Delta vs baseline", "", "| Metric | Δ |", "| --- | --- |"]
    for key, value in diff.items():
        lines.append(f"| {key} | {value} |")
    lines.append("")
    return "\n".join(lines)


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze export dataset quality.")
    parser.add_argument("--site", required=True, help="Site domain, e.g. ili-ili.com")
    parser.add_argument("--export", help="Override path to export .xlsx file")
    parser.add_argument(
        "--baseline",
        help="Path to baseline .xlsx snapshot (optional)",
    )
    parser.add_argument("--report", required=True, help="Path to markdown report")
    parser.add_argument(
        "--json",
        help="Optional path to write metrics as JSON",
    )

    args = parser.parse_args()

    site = args.site
    export_path = (
        Path(args.export)
        if args.export
        else Path(f"data/sites/{site}/exports/{site}_latest.xlsx")
    )
    baseline_path = (
        Path(args.baseline)
        if args.baseline
        else Path(f"reports/baseline/{site}_latest.xlsx")
    )
    report_path = Path(args.report)
    json_path = Path(args.json) if args.json else None

    current_df = load_export(export_path)
    current_metrics = compute_metrics(current_df)

    baseline_metrics: Optional[ExportMetrics] = None
    baseline_df: Optional[pd.DataFrame] = None
    if baseline_path.exists():
        baseline_df = load_export(baseline_path)
        baseline_metrics = compute_metrics(baseline_df)

    sections = [f"## Export quality report — {site}", ""]
    sections.append(render_section("Current snapshot", current_metrics))

    if baseline_metrics is not None:
        sections.append(render_section("Baseline snapshot", baseline_metrics))
        sections.append(render_delta(delta(current_metrics, baseline_metrics)))
    else:
        sections.append(
            "_Baseline snapshot not found — created report for current export only._\n"
        )

    ensure_parent(report_path)
    report_path.write_text("\n".join(sections), encoding="utf-8")

    if json_path is not None:
        ensure_parent(json_path)
        payload = {"current": current_metrics.to_dict()}
        if baseline_metrics is not None:
            payload["baseline"] = baseline_metrics.to_dict()
            payload["delta"] = delta(current_metrics, baseline_metrics)
        json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
