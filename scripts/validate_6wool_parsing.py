#!/usr/bin/env python3
"""End-to-end validator for the 6wool.ru parsing workflow.

The validator supports two modes:

1. Offline (default) – uses deterministic fixtures in ``tests/fixtures`` to
   ensure the parser logic continues to extract consistent variation data.
2. Live (``--live``) – optionally spins up AntibotManager + Playwright to check
   the current production pages. Live mode depends on network access and the
   availability of anti-bot infrastructure.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path

if __package__ is None or __package__ == "":  # pragma: no cover
    sys.path.append(str(Path(__file__).resolve().parents[1]))
from typing import Any, Dict, List, Optional

from parsers.variation_parser import VariationParser
from utils.cms_detection import CMSDetection

try:
    from core.antibot_manager import AntibotManager
except Exception:  # noqa: BLE001
    AntibotManager = None  # type: ignore

from tests.fixtures.sixwool_live_data import INTEGRATION_CASES, EXPECTED_VARIATIONS


@dataclass
class ValidationResult:
    slug: str
    url: str
    mode: str
    observed_variations: List[Dict[str, Any]]
    expected_variations: Optional[List[Dict[str, Any]]]
    cms_type: Optional[str]
    confidence: float
    duration_seconds: float
    success: bool
    notes: Optional[str] = None


def _load_sixwool_selector_bundle(detector: CMSDetection) -> Dict[str, List[str]]:
    bundle: Dict[str, List[str]] = {}
    for key in ["selectors", "attributes", "swatches", "price_update", "stock_update", "json_data"]:
        bundle[key] = detector.get_variation_selectors(key, "sixwool")
    return bundle


def _compact(variations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    compacted: List[Dict[str, Any]] = []
    for item in variations:
        compacted.append(
            {
                "type": item.get("type"),
                "value": item.get("value"),
                "price": round(float(item.get("price", 0.0)), 2),
                "stock": item.get("stock"),
            }
        )
    compacted.sort(key=lambda entry: entry["value"])
    return compacted


def validate_offline() -> List[ValidationResult]:
    detector = CMSDetection()
    parser = VariationParser()
    parser.cms_type = "sixwool"
    selectors = _load_sixwool_selector_bundle(detector)

    results: List[ValidationResult] = []
    for case in INTEGRATION_CASES:
        parser._current_url = case["url"]
        start = time.perf_counter()
        parser._http_get_json = lambda *args, **kwargs: case["ajax"]  # type: ignore
        variations = parser._parse_sixwool_variations(case["html"], selectors)
        elapsed = time.perf_counter() - start

        expected = _compact(EXPECTED_VARIATIONS[case["slug"]])
        observed = _compact(variations)
        success = observed == expected

        detection = detector.detect_cms_by_patterns(url=case["url"], html=case["html"])

        results.append(
            ValidationResult(
                slug=case["slug"],
                url=case["url"],
                mode="offline",
                observed_variations=observed,
                expected_variations=expected,
                cms_type=detection.cms_type,
                confidence=detection.confidence,
                duration_seconds=elapsed,
                success=success,
                notes=None if success else "Variation mismatch",
            )
        )
    return results


def validate_live(config_path: str) -> List[ValidationResult]:
    if AntibotManager is None:
        raise SystemExit("AntibotManager is unavailable; install Playwright dependencies first.")

    manager = AntibotManager(config_path)
    parser = VariationParser(antibot_manager=manager)
    parser.cms_type = "sixwool"
    detector = CMSDetection()
    selectors = _load_sixwool_selector_bundle(detector)

    results: List[ValidationResult] = []
    browser = manager.get_browser()
    try:
        for case in INTEGRATION_CASES:
            start = time.perf_counter()
            page = manager.get_page()
            try:
                page.goto(case["url"], wait_until="domcontentloaded")
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(3000)
                html = page.content()
            finally:
                context = page.context
                page.close()
                context.close()

            parser._current_url = case["url"]
            variations = parser._parse_sixwool_variations(html, selectors)
            elapsed = time.perf_counter() - start
            observed = _compact(variations)
            expected = _compact(EXPECTED_VARIATIONS[case["slug"]])
            detection = detector.detect_cms_by_patterns(url=case["url"], html=html)

            results.append(
                ValidationResult(
                    slug=case["slug"],
                    url=case["url"],
                    mode="live",
                    observed_variations=observed,
                    expected_variations=expected,
                    cms_type=detection.cms_type,
                    confidence=detection.confidence,
                    duration_seconds=elapsed,
                    success=True,
                    notes="Live mode does not compare against expected fixtures.",
                )
            )
    finally:
        try:
            browser.close()
        except Exception:  # noqa: BLE001
            pass

    return results


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate 6wool.ru variation parsing pipeline")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Run live validation through AntibotManager + Playwright.",
    )
    parser.add_argument(
        "--config",
        default="config/settings.json",
        help="Path to scraper configuration (default: config/settings.json)",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="Optional path to save the validation report as JSON.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)

    if args.live:
        results = validate_live(args.config)
    else:
        results = validate_offline()

    payload = [asdict(result) for result in results]

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Saved validation report to {args.report}")
    else:
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        print()

    failed = [result for result in results if not result.success]
    if failed:
        raise SystemExit(f"Validation failed for: {[failure.slug for failure in failed]}")


if __name__ == "__main__":
    main(sys.argv[1:])
