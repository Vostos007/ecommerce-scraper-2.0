#!/usr/bin/env python3
"""Validate CMS mappings and configuration for key ecommerce targets.

Usage examples:
  python scripts/validate_cms_mapping.py
  python scripts/validate_cms_mapping.py --live
  python scripts/validate_cms_mapping.py --site initki.ru --live

The script performs lightweight configuration checks offline and, when
``--live`` is supplied, fetches representative pages to confirm the detection
and variation parsing stacks line up with expectations. Live checks require
network access and may rely on environment variables for API credentials.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

if __package__ is None or __package__ == "":  # pragma: no cover
    sys.path.append(str(Path(__file__).resolve().parents[1]))
from typing import Dict, Iterable, List, Optional

try:
    import requests
except Exception:  # pragma: no cover - requests may be unavailable in some envs
    requests = None  # type: ignore

from utils.cms_detection import CMSDetection

SETTINGS_PATH = Path("config/settings.json")
SITES_PATH = Path("config/sites.json")

CMS_EXPECTATIONS = {
    "mpyarn.ru": "cm3",
    "initki.ru": "cscart",
    "triskeli.ru": "insales",
    "ili-ili.com": "bitrix",
}

API_ENV_HINTS = {
    "cscart": "CSCART_API_KEY",
    "insales": "INSALES_API_KEY",
}


@dataclass
class SiteSummary:
    domain: str
    cms_type: str
    has_api_profile: bool
    validation_errors: List[str]
    warnings: List[str]
    live_confidence: Optional[float] = None


def load_json(path: Path) -> Dict:
    if not path.exists():
        raise FileNotFoundError(f"Missing configuration file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def validate_site_config(site: Dict, settings: Dict, *, warn_missing_env: bool = False) -> SiteSummary:
    domain = site["domain"]
    expected_cms = CMS_EXPECTATIONS.get(domain)
    errors: List[str] = []
    warnings: List[str] = []

    cms_type = site.get("cms_type") or ""
    if expected_cms and cms_type != expected_cms:
        errors.append(f"Expected cms_type {expected_cms} but found {cms_type or 'missing'}")

    overrides = site.get("overrides", {})
    detection = overrides.get("cms_detection", {})
    if detection.get("force") != expected_cms:
        errors.append("cms_detection.force mismatch")

    variation_parser = overrides.get("variation_parser")
    if not variation_parser:
        errors.append("variation_parser override missing")
    else:
        if variation_parser.get("strategy") is None:
            errors.append("variation_parser.strategy missing")

    api_profile = overrides.get("api_integration")
    has_api_profile = bool(api_profile)
    if expected_cms in {"cscart", "insales"} and not has_api_profile:
        errors.append("API integration override missing")

    settings_api_profiles = settings.get("api_integration_profiles", {})
    if has_api_profile:
        api_type = api_profile.get("type")
        if api_type not in settings_api_profiles:
            errors.append(f"No api_integration_profile defined for {api_type}")
        else:
            profile_env = API_ENV_HINTS.get(api_type)
            if warn_missing_env and profile_env and not os.getenv(profile_env):
                warnings.append(f"Environment variable {profile_env} not set (API tests will be skipped)")

    return SiteSummary(
        domain=domain,
        cms_type=cms_type or expected_cms or "unknown",
        has_api_profile=has_api_profile,
        validation_errors=errors,
        warnings=warnings,
    )


def offline_validation(selected_sites: Optional[Iterable[str]] = None, *, warn_missing_env: bool = False) -> List[SiteSummary]:
    settings = load_json(SETTINGS_PATH)
    sites_cfg = load_json(SITES_PATH)
    summaries: List[SiteSummary] = []

    for site in sites_cfg.get("sites", []):
        if selected_sites and site["domain"] not in selected_sites:
            continue
        if site["domain"] not in CMS_EXPECTATIONS:
            continue
        summaries.append(validate_site_config(site, settings, warn_missing_env=warn_missing_env))

    return summaries


def live_validation(summaries: List[SiteSummary], selected_sites: Optional[Iterable[str]] = None) -> None:
    if requests is None:
        print("[warn] requests library unavailable; skipping live validation", file=sys.stderr)
        return

    detector = CMSDetection()
    for summary in summaries:
        if selected_sites and summary.domain not in selected_sites:
            continue
        url = f"https://{summary.domain}"
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            detection = detector.detect_cms_by_patterns(url=url, html=response.text)
            summary.live_confidence = detection.confidence
            if detection.cms_type != CMS_EXPECTATIONS.get(summary.domain):
                summary.validation_errors.append(
                    f"Live detection returned {detection.cms_type} (confidence {detection.confidence:.2f})"
                )
        except Exception as exc:  # pragma: no cover - network variability
            summary.validation_errors.append(f"Live fetch failed: {exc}")


def print_report(summaries: List[SiteSummary]) -> None:
    print("CMS Mapping Validation Report")
    print("=" * 32)
    for summary in summaries:
        status = "OK" if not summary.validation_errors else "ISSUES"
        print(f"- {summary.domain}: {summary.cms_type} [{status}]")
        if summary.has_api_profile:
            print("  API profile configured")
        if summary.live_confidence is not None:
            print(f"  Live detection confidence: {summary.live_confidence:.2f}")
        for error in summary.validation_errors:
            print(f"  ! {error}")
        for warning in summary.warnings:
            print(f"  ~ {warning}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate CMS site mappings")
    parser.add_argument("--live", action="store_true", help="Perform live detection checks")
    parser.add_argument("--site", action="append", help="Restrict validation to specific domains")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    selected = set(args.site or []) or None
    summaries = offline_validation(selected, warn_missing_env=args.live)

    if args.live:
        live_validation(summaries, selected)

    print_report(summaries)

    issues = [summary for summary in summaries if summary.validation_errors]
    if issues:
        sys.exit(1)


if __name__ == "__main__":
    main()
