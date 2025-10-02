#!/usr/bin/env python3
"""Utility script for capturing live 6wool.ru product structure.

This reconnaissance helper uses the project's AntibotManager + Playwright stack
in order to bypass DDoS-Guard, hydrate the product page, and persist both the
rendered HTML and associated JSON payloads that drive variation data. The
resulting assets are written into ``tests/fixtures/sixwool_live`` so they can be
referenced by automated tests and future parser adjustments.

The workflow is intentionally conservative:

* Launch a Playwright page through AntibotManager to inherit all stealth knobs.
* Visit curated product URLs representative of different variation patterns.
* Await network-idle and selector-level hydration to ensure data is present.
* Capture the final HTML, relevant network responses, and structural metadata.
* Emit a per-product JSON summary describing discovered selectors/endpoints.

The script may be run repeatedly; assets are timestamped to avoid overwriting
prior captures. All interactions are serialized to keep the DDoS-Guard surface
area minimal.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path

if __package__ is None or __package__ == "":  # pragma: no cover - allow direct execution
    sys.path.append(str(Path(__file__).resolve().parents[1]))
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse, urljoin

from core.antibot_manager import AntibotManager
from utils.cms_detection import CMSDetection

DEFAULT_PRODUCT_URLS: List[str] = [
    "https://6wool.ru/catalog/pryazha/jawoll-magic-degrade/",
    "https://6wool.ru/catalog/pryazha/estremo/",
    "https://6wool.ru/catalog/pryazha/woolness/",
]

DEFAULT_WAIT_SECONDS: float = 4.0
OUTPUT_ROOT = Path("tests/fixtures/sixwool_live")


@dataclass
class CaptureSummary:
    url: str
    slug: str
    timestamp: float
    selectors_observed: Dict[str, List[str]]
    ajax_endpoints: List[str]
    network_payloads: List[Dict[str, str]]
    ddos_guard_triggered: bool
    cms_detection: Dict[str, Any]


def _slugify(url: str) -> str:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    slug = "-".join(parts) if parts else parsed.netloc.replace(".", "-")
    return slug or "product"


def _load_sixwool_selector_waits() -> Dict[str, Iterable[str]]:
    settings_path = Path("config/settings.json")
    if not settings_path.exists():
        return {
            "wait_for_selectors": [
                ".product-view",
                ".product-detail",
            ],
            "network_idle_states": ["networkidle"],
        }

    try:
        config = json.loads(settings_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {
            "wait_for_selectors": [
                ".product-view",
                ".product-detail",
            ],
            "network_idle_states": ["networkidle"],
        }

    cms_selectors = config.get("cms_selectors", {})
    sixwool_entry = cms_selectors.get("sixwool", {}) if isinstance(cms_selectors, dict) else {}
    wait_for = sixwool_entry.get("wait_for_selectors") or [
        ".product-detail",
        "#bx-component-scope",
        "[data-sixwool-variation]",
    ]
    load_states = sixwool_entry.get("playwright_wait_states") or ["networkidle", "load"]
    return {
        "wait_for_selectors": [selector for selector in wait_for if isinstance(selector, str)],
        "network_idle_states": [state for state in load_states if isinstance(state, str)],
    }


def _ensure_output_dirs() -> None:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    (OUTPUT_ROOT / "network").mkdir(parents=True, exist_ok=True)
    (OUTPUT_ROOT / "summaries").mkdir(parents=True, exist_ok=True)


def _record_network_payloads(page, slug: str) -> List[Dict[str, str]]:
    saved_records: List[Dict[str, str]] = []
    network_dir = OUTPUT_ROOT / "network"

    def handle_response(response) -> None:
        try:
            url = response.url or ""
            if "6wool.ru" not in url:
                return
            headers = response.headers
            content_type = headers.get("content-type", "").lower()
            if "json" not in content_type:
                return
            body = response.text()
            if not body.strip():
                return
            timestamp_ms = int(time.time() * 1000)
            filename = f"{slug}_{timestamp_ms}.json"
            dest = network_dir / filename
            dest.write_text(body, encoding="utf-8")
            saved_records.append({"url": url, "path": str(dest)})
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"[warn] Failed to persist response: {exc}\n")

    page.on("response", handle_response)
    return saved_records


def _extract_selectors(page, candidate_selectors: Iterable[str]) -> Dict[str, List[str]]:
    detected: Dict[str, List[str]] = {
        "present": [],
        "missing": [],
    }
    for selector in candidate_selectors:
        try:
            if page.query_selector(selector):
                detected["present"].append(selector)
            else:
                detected["missing"].append(selector)
        except Exception:
            detected["missing"].append(selector)
    return detected


def analyse_single_url(
    manager: AntibotManager,
    url: str,
    wait_seconds: float,
    selector_conf: Dict[str, Iterable[str]],
    cms_detector: CMSDetection,
) -> CaptureSummary:
    slug = _slugify(url)
    page = manager.get_page()
    captured_payloads = _record_network_payloads(page, slug)

    ddos_guard_triggered = False
    try:
        manager.logger.info("Navigating to %s for 6wool structure capture", url)
        page.goto(url, wait_until="domcontentloaded")

        for state in selector_conf.get("network_idle_states", []):
            try:
                page.wait_for_load_state(state, timeout=30000)
            except Exception:
                continue

        for selector in selector_conf.get("wait_for_selectors", []):
            try:
                page.wait_for_selector(selector, timeout=35000)
            except Exception:
                continue

        page.wait_for_timeout(int(wait_seconds * 1000))

        html = page.content()
        if "DDoS-GUARD" in html or "ddos-guard" in html.lower():
            ddos_guard_triggered = True

        html_path = OUTPUT_ROOT / f"{slug}.html"
        html_path.write_text(html, encoding="utf-8")

        selectors_observed = _extract_selectors(
            page,
            selector_conf.get("wait_for_selectors", []),
        )

        detection = cms_detector.detect_cms_by_patterns(url=url, html=html)
        ajax_urls = sorted({record["url"] for record in captured_payloads if record.get("url")})

        summary = CaptureSummary(
            url=url,
            slug=slug,
            timestamp=time.time(),
            selectors_observed=selectors_observed,
            ajax_endpoints=ajax_urls,
            network_payloads=captured_payloads,
            ddos_guard_triggered=ddos_guard_triggered,
            cms_detection={
                "cms_type": detection.cms_type,
                "confidence": detection.confidence,
                "detection_methods": detection.detection_methods,
            },
        )

        summary_path = OUTPUT_ROOT / "summaries" / f"{slug}.json"
        summary_path.write_text(json.dumps(asdict(summary), ensure_ascii=False, indent=2), encoding="utf-8")

        metadata_path = OUTPUT_ROOT / "summaries" / f"{slug}_meta.json"
        metadata_path.write_text(
            json.dumps(
                {
                    "url": url,
                    "slug": slug,
                    "html_path": str(html_path),
                    "network_payloads": captured_payloads,
                    "network_payload_urls": ajax_urls,
                    "network_payload_paths": [record["path"] for record in captured_payloads],
                    "selectors_present": summary.selectors_observed["present"],
                    "selectors_missing": summary.selectors_observed["missing"],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        return summary
    finally:
        try:
            context = page.context
            page.close()
            context.close()
        except Exception:
            pass


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Capture hydrated 6wool.ru product pages for parser development")
    parser.add_argument("urls", nargs="*", help="Product URLs to analyze. Defaults to curated set if omitted.")
    parser.add_argument(
        "--wait",
        dest="wait_seconds",
        type=float,
        default=DEFAULT_WAIT_SECONDS,
        help="Seconds to wait after hydration before capturing (default: 4.0)",
    )
    parser.add_argument(
        "--config",
        dest="config_path",
        default="config/settings.json",
        help="Path to scraper configuration (default: config/settings.json)",
    )
    parser.add_argument(
        "--no-defaults",
        action="store_true",
        help="Disable default target URLs and only use ones provided on the command line.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)

    urls = args.urls or []
    if not args.no_defaults:
        urls = list(dict.fromkeys(DEFAULT_PRODUCT_URLS + urls))
    if not urls:
        raise SystemExit("No product URLs provided for analysis")

    _ensure_output_dirs()

    manager = AntibotManager(args.config_path)
    manager.get_browser()

    selectors_conf = _load_sixwool_selector_waits()
    cms_detector = CMSDetection()

    summaries: List[CaptureSummary] = []
    try:
        for url in urls:
            summaries.append(analyse_single_url(manager, url, args.wait_seconds, selectors_conf, cms_detector))
            time.sleep(1.5)
    finally:
        try:
            browser = getattr(manager, "browser", None)
            if browser:
                browser.close()
        except Exception:
            pass

    report_path = OUTPUT_ROOT / f"run_{int(time.time())}.json"
    report_payload = [asdict(summary) for summary in summaries]
    report_path.write_text(json.dumps(report_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Captured {len(summaries)} pages. Report: {report_path}")


if __name__ == "__main__":
    main(sys.argv[1:])
