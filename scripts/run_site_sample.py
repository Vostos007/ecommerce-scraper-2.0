#!/usr/bin/env python3
"""Run a sampled scrape for a single site using sitemap URLs."""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from typing import List

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from run_sites import load_sites_config, run_site, SiteRunError


def load_sitemap_urls(path: Path) -> List[str]:
    if not path.exists():
        raise FileNotFoundError(f"Sitemap file not found: {path}")

    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    links = payload.get("links", [])
    urls = [entry.get("url") for entry in links if isinstance(entry, dict)]
    return [url for url in urls if url]


def resolve_site(config: dict, site_key: str) -> dict:
    sites = config.get("sites", [])
    for entry in sites:
        domain = (entry.get("domain") or "").lower()
        name = (entry.get("name") or "").lower()
        if site_key == domain or site_key == name:
            return entry
    raise SiteRunError(f"Site '{site_key}' not found in configuration")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run sampled scrape for a site")
    parser.add_argument("site", help="Site identifier (domain or name from sites.json)")
    parser.add_argument(
        "--count",
        type=int,
        default=50,
        help="Number of URLs to sample (default: 50)",
    )
    parser.add_argument(
        "--sitemap",
        type=Path,
        help="Path to sitemap JSON (defaults to data/sites/<site>/<site>.sitemap.json)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Random seed for reproducible sampling",
    )
    parser.add_argument(
        "--engine-config",
        type=Path,
        default=Path("config/settings.json"),
        help="Path to engine config JSON",
    )
    parser.add_argument(
        "--sites-config",
        type=Path,
        default=Path("config/sites.json"),
        help="Path to sites.json configuration",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only print planned execution without running scrape",
    )

    args = parser.parse_args()

    try:
        sites_config = load_sites_config(args.sites_config)
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to load sites config: {exc}", file=sys.stderr)
        return 1

    defaults = sites_config.get("defaults", {})
    site_entry = resolve_site(sites_config, args.site.lower())

    sitemap_path = (
        args.sitemap
        if args.sitemap
        else Path("data/sites")
        / site_entry.get("domain", args.site)
        / f"{site_entry.get('domain', args.site)}.sitemap.json"
    )

    try:
        urls = load_sitemap_urls(sitemap_path)
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to load sitemap: {exc}", file=sys.stderr)
        return 1

    if not urls:
        print("Sitemap contains no URLs", file=sys.stderr)
        return 1

    sample_count = min(max(1, args.count), len(urls))
    if args.seed is not None:
        random.seed(args.seed)
    sampled_urls = random.sample(urls, sample_count)

    summary = run_site(
        site=site_entry,
        defaults=defaults,
        engine_config=args.engine_config,
        max_products_override=sample_count,
        cached_urls_override=sampled_urls,
        skip_cache_refresh=True,
        dry_run=args.dry_run,
    )

    summary["sampled_count"] = sample_count
    summary["sitemap"] = str(sitemap_path)

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
