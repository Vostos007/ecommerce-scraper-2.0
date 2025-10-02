"""Product URL discovery helpers for per-site cache warm-up."""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable, List, Optional, Sequence, Set
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlsplit, urlunsplit

from bs4 import BeautifulSoup

try:  # optional dependency, available in this project
    from curl_cffi import requests as curl_requests
except Exception:  # pragma: no cover - curl_cffi may be missing in CI
    curl_requests = None  # type: ignore

import requests

from utils.helpers import is_product_url, looks_like_guard_html

if TYPE_CHECKING:  # pragma: no cover - type check only
    from network.firecrawl_client import FirecrawlClient


logger = logging.getLogger("url_cache_builder")


@dataclass
class DiscoveryRuntime:
    """Runtime dependencies that can be overridden for testing."""

    fetch_resource: Callable[[str], Optional[bytes]]
    fetch_with_playwright: Callable[[str, float], Optional[str]]
    sleep: Callable[[float], None]
    logger: logging.Logger


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.1",
}


FetchFunc = Callable[[str], Optional[bytes]]


def _resolve_preseed_urls(scraping_config: dict, runtime: DiscoveryRuntime) -> List[str]:
    """Load pre-seeded URLs from inline values or JSON snapshots."""

    urls: List[str] = []
    seen: Set[str] = set()

    def _append(raw: Optional[str]) -> None:
        if not isinstance(raw, str):
            return
        candidate = _normalize_discovery_url(raw)
        if not candidate or candidate in seen:
            return
        seen.add(candidate)
        urls.append(candidate)

    direct = scraping_config.get("preseed_urls")
    if isinstance(direct, (list, tuple, set)):
        for entry in direct:
            _append(entry if isinstance(entry, str) else None)

    file_entry = scraping_config.get("preseed_urls_file")
    if file_entry:
        path = Path(file_entry)
        try:
            payload = path.read_text(encoding="utf-8")
            data = json.loads(payload)
        except FileNotFoundError:
            runtime.logger.debug("Preseed file not found: %s", path)
        except json.JSONDecodeError as exc:
            runtime.logger.debug("Preseed JSON decode error (%s): %s", path, exc)
        except OSError as exc:
            runtime.logger.debug("Preseed read error (%s): %s", path, exc)
        else:
            if isinstance(data, dict):
                candidates = data.get("links")
                if isinstance(candidates, list):
                    for item in candidates:
                        if isinstance(item, dict):
                            _append(item.get("url"))
                        else:
                            _append(item if isinstance(item, str) else None)
                else:
                    for value in data.values():
                        if isinstance(value, (list, tuple)):
                            for entry in value:
                                if isinstance(entry, dict):
                                    _append(entry.get("url"))
                                else:
                                    _append(entry if isinstance(entry, str) else None)
                        elif isinstance(value, str):
                            _append(value)
            elif isinstance(data, list):
                for entry in data:
                    if isinstance(entry, dict):
                        _append(entry.get("url"))
                    else:
                        _append(entry if isinstance(entry, str) else None)

    return urls


def _normalize_discovery_url(raw: str) -> str:
    candidate = raw.strip()
    if not candidate:
        return ""

    parsed = urlsplit(candidate)
    path = parsed.path or ""
    if path and not path.endswith("/"):
        last_segment = path.rsplit("/", 1)[-1]
        if "." not in last_segment:
            path = f"{path}/"
    normalized = urlunsplit(parsed._replace(path=path))
    return normalized.strip()


@dataclass
class DiscoveryConfig:
    base_url: str
    cached_urls_file: Path
    product_patterns: Sequence[str]
    category_urls: Sequence[str]
    pagination: dict
    sitemap_sources: Sequence[str]
    max_urls: int
    request_delay: float
    min_product_segments: int
    force_category_discovery: bool
    playwright_enabled: bool
    playwright_wait: float
    filter_sitemap_products: bool
    require_numeric_last_segment: bool
    preseed_replace_existing: bool


def refresh_cached_urls(
    scraping_config: dict,
    base_url: str,
    *,
    firecrawl_client: Optional["FirecrawlClient"] = None,
    fetch_resource: Optional[Callable[[str], Optional[bytes]]] = None,
    fetch_with_playwright: Optional[Callable[[str, float], Optional[str]]] = None,
    sleep_func: Optional[Callable[[float], None]] = None,
    logger_obj: Optional[logging.Logger] = None,
) -> Optional[int]:
    """Refresh cached product URLs for a site.

    Args:
        scraping_config: Scraper discovery configuration block.
        base_url: Primary site URL used for discovery.
        fetch_resource: Optional override for low-level HTTP fetching.
        fetch_with_playwright: Optional override for guard bypass fetching.
        sleep_func: Optional delay override used during pagination waits.
        logger_obj: Optional logger override for structured testing.

    Returns:
        Number of cached URLs written, or ``None`` when discovery is skipped.
    """

    runtime = DiscoveryRuntime(
        fetch_resource=fetch_resource or _fetch_resource,
        fetch_with_playwright=fetch_with_playwright or _fetch_with_playwright,
        sleep=sleep_func or time.sleep,
        logger=logger_obj or logger,
    )

    if not isinstance(scraping_config, dict):
        runtime.logger.warning(
            "Skipping cache refresh for %s: scraping_config must be a dict", base_url
        )
        return None

    cached_urls_file = scraping_config.get("cached_urls_file")
    if not cached_urls_file:
        runtime.logger.debug(
            "Cache refresh skipped for %s: no cached_urls_file configured", base_url
        )
        return None

    base_url_normalized = (base_url or "").strip()
    if not base_url_normalized:
        runtime.logger.warning(
            "Cache refresh skipped because base_url is empty: %s", base_url
        )
        return None

    start_time = time.perf_counter()

    raw_max_discovery = scraping_config.get("max_discovery_urls", 10000)
    try:
        max_urls = int(raw_max_discovery)
    except (TypeError, ValueError):
        runtime.logger.warning(
            "Invalid max_discovery_urls in config for %s; using default 10000",
            base_url_normalized,
        )
        max_urls = 10000
    if max_urls < 0:
        runtime.logger.warning(
            "Negative max_discovery_urls in config for %s; treating as 0",
            base_url_normalized,
        )
        max_urls = 0
    if max_urls == 0:
        runtime.logger.debug(
            "Cache refresh disabled for %s: max_discovery_urls=0",
            base_url_normalized,
        )
        return 0

    preseed_urls = _resolve_preseed_urls(scraping_config, runtime)

    config = DiscoveryConfig(
        base_url=base_url_normalized.rstrip("/"),
        cached_urls_file=Path(cached_urls_file),
        product_patterns=tuple(scraping_config.get("product_patterns", [])),
        category_urls=tuple(scraping_config.get("category_urls", [])),
        pagination=scraping_config.get("pagination", {}) or {},
        sitemap_sources=tuple(scraping_config.get("sitemap_sources", [])),
        max_urls=max_urls,
        request_delay=float(scraping_config.get("product_delay_seconds", 0.0) or 0.0),
        min_product_segments=int(scraping_config.get("min_product_path_segments", 0) or 0),
        force_category_discovery=bool(scraping_config.get("force_category_discovery", False)),
        playwright_enabled=bool(scraping_config.get("playwright_fallback", False)),
        playwright_wait=float(scraping_config.get("playwright_wait_seconds", 4.0) or 0.0),
        filter_sitemap_products=bool(scraping_config.get("filter_sitemap_products", False)),
        require_numeric_last_segment=bool(scraping_config.get("require_numeric_last_segment", False)),
        preseed_replace_existing=bool(scraping_config.get("preseed_replace_existing", False)),
    )

    runtime.logger.debug(
        "Starting cached URL refresh for %s with limit %d", config.base_url, config.max_urls
    )

    existing: List[str] = []
    cache_ttl_hours = scraping_config.get("cache_ttl_hours")
    try:
        cache_ttl = float(cache_ttl_hours) if cache_ttl_hours is not None else None
    except (TypeError, ValueError):
        cache_ttl = None

    if config.cached_urls_file.exists():
        try:
            existing_text = config.cached_urls_file.read_text(encoding="utf-8")
            existing = [line.strip() for line in existing_text.splitlines() if line.strip()]
        except Exception as exc:
            runtime.logger.warning(
                "Failed reading existing cache %s: %s", config.cached_urls_file, exc
            )
            existing = []

        if cache_ttl and cache_ttl > 0 and not config.preseed_replace_existing:
            age_seconds = time.time() - config.cached_urls_file.stat().st_mtime
            age_hours = age_seconds / 3600.0
            if age_hours <= cache_ttl:
                runtime.logger.info(
                    "Reusing cached URLs for %s (age %.1fh <= %.1fh TTL)",
                    config.base_url,
                    age_hours,
                    cache_ttl,
                )
                return len(existing)

    seen: Set[str] = set()
    discovered: List[str] = []

    firecrawl_cfg = scraping_config.get("firecrawl_map", {})
    firecrawl_urls: List[str] = []
    if (
        firecrawl_client
        and getattr(firecrawl_client, "enabled", False)
        and isinstance(firecrawl_cfg, dict)
        and firecrawl_cfg.get("enabled")
    ):
        firecrawl_urls = _discover_with_firecrawl_map(
            config,
            firecrawl_cfg,
            seen,
            runtime,
            firecrawl_client,
        )
        discovered.extend(firecrawl_urls)

    if preseed_urls and len(discovered) < config.max_urls:
        preseed_filtered = _filter_products(
            preseed_urls,
            config.product_patterns,
            config.min_product_segments,
            require_numeric_last_segment=config.require_numeric_last_segment,
        )
        if config.preseed_replace_existing:
            existing = []
        for url in preseed_filtered:
            if url in seen:
                continue
            seen.add(url)
            discovered.append(url)
            if len(discovered) >= config.max_urls:
                break

    if config.sitemap_sources and len(discovered) < config.max_urls:
        sitemap_urls = _discover_from_sitemaps(config, seen, runtime)
        discovered.extend(sitemap_urls)

    if config.category_urls and (
        config.force_category_discovery or not discovered
    ) and len(discovered) < config.max_urls:
        category_urls = _discover_from_categories(config, seen, runtime)
        discovered.extend(category_urls)

    if not discovered:
        runtime.logger.debug(
            "URL cache refresh skipped for %s: discovery produced no URLs",
            config.base_url,
        )
        return None

    combined = _merge_and_limit_urls(existing, discovered, config.max_urls)
    runtime.logger.debug(
        "Merged %d existing and %d discovered URLs into %d unique entries",
        len(existing),
        len(discovered),
        len(combined),
    )

    try:
        config.cached_urls_file.parent.mkdir(parents=True, exist_ok=True)
        config.cached_urls_file.write_text("\n".join(combined), encoding="utf-8")
    except OSError as exc:
        runtime.logger.error(
            "Failed to write cache file %s: %s", config.cached_urls_file, exc
        )
        return None

    try:
        product_only_urls = [
            url
            for url in combined
            if url.rstrip("/").split("/")[-1].isdigit()
        ]
        if product_only_urls:
            product_only_path = config.cached_urls_file.with_name(
                f"{config.cached_urls_file.stem}_products{config.cached_urls_file.suffix}"
            )
            product_only_path.write_text("\n".join(product_only_urls), encoding="utf-8")
    except Exception as exc:  # pragma: no cover - best effort helper
        runtime.logger.debug(
            "Failed to generate product-only cache for %s: %s",
            config.base_url,
            exc,
        )

    duration = time.perf_counter() - start_time
    runtime.logger.info(
        "Cached %d product URLs for %s in %.2fs (new=%d existing=%d)",
        len(combined),
        config.base_url,
        duration,
        len(discovered),
        len(existing),
    )
    return len(combined)


def _merge_and_limit_urls(
    existing: Sequence[str], discovered: Sequence[str], limit: int
) -> List[str]:
    """Merge URL collections, preserving order and applying a hard limit."""

    merged: List[str] = []
    seen: Set[str] = set()
    for source in (existing, discovered):
        for candidate in source:
            if not candidate:
                continue
            if candidate in seen:
                continue
            seen.add(candidate)
            merged.append(candidate)
            if len(merged) >= limit:
                return merged
    return merged


def _filter_products(
    urls: Sequence[str],
    patterns: Sequence[str],
    min_segments: int,
    *,
    require_numeric_last_segment: bool = False,
) -> List[str]:
    filtered: List[str] = []
    for url in urls:
        if not isinstance(url, str):
            continue
        candidate = url.strip()
        if not candidate:
            continue
        if require_numeric_last_segment:
            path = urlsplit(candidate).path.strip("/")
            if not path:
                continue
            last_segment = path.split("/")[-1]
            if not last_segment.isdigit():
                continue
        if _looks_like_product(candidate, patterns, min_segments):
            filtered.append(candidate)
    return filtered


def _discover_from_sitemaps(
    config: DiscoveryConfig, seen: Set[str], runtime: DiscoveryRuntime
) -> List[str]:
    aggregated: List[str] = []
    pending: List[str] = [urljoin(config.base_url, url) for url in config.sitemap_sources]

    while pending and len(aggregated) < config.max_urls:
        sitemap_url = pending.pop(0)
        try:
            payload = runtime.fetch_resource(sitemap_url)
        except Exception as exc:  # noqa: BLE001
            runtime.logger.warning(
                "Exception fetching sitemap %s: %s", sitemap_url, exc
            )
            continue

        if payload and payload[:2] == b"\x1f\x8b":
            try:
                payload = gzip.decompress(payload)
            except OSError as exc:
                runtime.logger.debug(
                    "Failed to decompress sitemap %s: %s", sitemap_url, exc
                )
                continue
        if not payload:
            runtime.logger.debug("Failed to fetch sitemap %s", sitemap_url)
            continue

        try:
            urls, nested = _parse_sitemap(payload)
        except ET.ParseError as exc:
            runtime.logger.debug("Sitemap parse error for %s: %s", sitemap_url, exc)
            continue
        except Exception as exc:  # pragma: no cover - defensive path
            runtime.logger.debug("Unexpected sitemap error for %s: %s", sitemap_url, exc)
            continue

        for child in nested:
            if len(pending) + len(aggregated) >= config.max_urls:
                break
            pending.append(urljoin(sitemap_url, child))

        for url in urls:
            if not url or url in seen:
                continue
            if config.filter_sitemap_products and not _looks_like_product(
                url, config.product_patterns, config.min_product_segments
            ):
                continue
            if (
                not config.filter_sitemap_products
                and config.min_product_segments
                and _path_segments(url) < config.min_product_segments
            ):
                continue
            seen.add(url)
            aggregated.append(url)
            if len(aggregated) >= config.max_urls:
                break

    runtime.logger.debug(
        "Sitemap discovery produced %d URLs (seen=%d)", len(aggregated), len(seen)
    )
    return aggregated


def _discover_from_categories(
    config: DiscoveryConfig, seen: Set[str], runtime: DiscoveryRuntime
) -> List[str]:
    aggregated: List[str] = []
    pagination = config.pagination
    page_param = pagination.get("param")
    raw_start_page = pagination.get("start", 1)
    raw_max_pages = pagination.get("max_pages", 0)

    try:
        start_page = int(raw_start_page)
    except (TypeError, ValueError):
        runtime.logger.debug(
            "Invalid pagination start for %s; defaulting to 1", config.base_url
        )
        start_page = 1
    if start_page < 1:
        runtime.logger.debug(
            "Adjusted pagination start to 1 for %s (was %s)",
            config.base_url,
            raw_start_page,
        )
        start_page = 1

    try:
        max_pages = int(raw_max_pages)
    except (TypeError, ValueError):
        runtime.logger.debug(
            "Invalid pagination max_pages for %s; defaulting to 0", config.base_url
        )
        max_pages = 0
    if max_pages < 0:
        runtime.logger.debug(
            "Adjusted pagination max_pages to 0 for %s (was %s)",
            config.base_url,
            raw_max_pages,
        )
        max_pages = 0

    include_base = bool(pagination.get("include_base", True))
    stop_on_empty = bool(pagination.get("stop_on_empty", True))
    delay_seconds = float(pagination.get("delay_seconds", config.request_delay))

    for category_url in config.category_urls:
        page_counter = 0
        empty_runs = 0
        safety_guard = 0
        while True:
            if page_counter == 0 and include_base:
                page_url = category_url
            else:
                if not page_param:
                    break
                offset = 1 if include_base else 0
                page_number = max(1, start_page + page_counter - offset)
                page_url = _attach_page(category_url, page_param, page_number)

            if max_pages and page_counter >= max_pages:
                break

            html = _fetch_category_html(page_url, config, runtime)
            page_counter += 1
            if not html:
                empty_runs += 1
                if stop_on_empty and empty_runs >= 1:
                    break
                continue

            product_links = _extract_product_links(
                html, page_url, config.product_patterns, config.min_product_segments
            )
            if not product_links:
                empty_runs += 1
                if stop_on_empty and empty_runs >= 1:
                    break
                continue

            empty_runs = 0
            for link in product_links:
                if link in seen:
                    continue
                seen.add(link)
                aggregated.append(link)
            if len(aggregated) >= config.max_urls:
                break

            if len(aggregated) >= config.max_urls:
                break

            if delay_seconds > 0:
                runtime.sleep(delay_seconds)

            safety_guard += 1
            if safety_guard > 10_000:
                runtime.logger.warning(
                    "Stopping pagination for %s due to safety guard threshold",
                    category_url,
                )
                break

        if len(aggregated) >= config.max_urls:
            break

    return aggregated


def _discover_with_firecrawl_map(
    config: DiscoveryConfig,
    map_config: dict,
    seen: Set[str],
    runtime: DiscoveryRuntime,
    firecrawl_client: "FirecrawlClient",
) -> List[str]:
    start_url = str(map_config.get("start_url") or config.base_url)
    search = map_config.get("search")
    sitemap_mode = map_config.get("sitemap")
    include_subdomains = map_config.get("include_subdomains")
    include_paths = map_config.get("include_paths")
    exclude_paths = map_config.get("exclude_paths")

    raw_map_segments = map_config.get("min_segments")
    try:
        map_min_segments = (
            int(raw_map_segments)
            if raw_map_segments is not None
            else config.min_product_segments
        )
    except (TypeError, ValueError):
        map_min_segments = config.min_product_segments

    if map_min_segments < 0:
        map_min_segments = 0
    if raw_map_segments is None and map_min_segments > 0:
        map_min_segments = max(1, map_min_segments - 1)

    raw_limit = map_config.get("limit")
    try:
        primary_limit = int(raw_limit) if raw_limit is not None else config.max_urls
    except (TypeError, ValueError):
        runtime.logger.debug(
            "Invalid Firecrawl map limit for %s; using discovery limit %d",
            config.base_url,
            config.max_urls,
        )
        primary_limit = config.max_urls

    if primary_limit <= 0 or primary_limit > config.max_urls:
        primary_limit = config.max_urls

    fallback_limits_raw = map_config.get("fallback_limits")
    fallback_limits: List[int] = []
    if isinstance(fallback_limits_raw, list):
        for value in fallback_limits_raw:
            try:
                num = int(value)
            except (TypeError, ValueError):
                continue
            if num > 0:
                fallback_limits.append(num)

    if not fallback_limits:
        if primary_limit > 40:
            fallback_limits.extend({max(primary_limit // 2, 20), 10})
        else:
            fallback_limits.append(10)

    attempt_limits = [primary_limit] + [limit for limit in fallback_limits if limit > 0]

    urls: List[str] = []
    selected_limit = primary_limit
    for attempt_limit in attempt_limits:
        effective_limit = max(1, min(attempt_limit, config.max_urls))

        runtime.logger.debug(
            "Requesting Firecrawl map for %s (search=%s, limit=%d)",
            start_url,
            search,
            effective_limit,
        )

        try:
            urls = firecrawl_client.map_urls(
                start_url,
                search=search,
                limit=effective_limit,
                sitemap_mode=sitemap_mode,
                include_subdomains=include_subdomains,
                include_paths=include_paths if isinstance(include_paths, list) else None,
                exclude_paths=exclude_paths if isinstance(exclude_paths, list) else None,
            )
        except Exception as exc:  # noqa: BLE001 - defensive guard
            runtime.logger.warning(
                "Firecrawl map discovery failed for %s: %s", config.base_url, exc
            )
            urls = []

        if urls:
            selected_limit = effective_limit
            break

    if not urls:
        return []

    filtered: List[str] = []
    for url in urls:
        if len(filtered) >= selected_limit or len(filtered) + len(seen) >= config.max_urls:
            break
        if not url or url in seen:
            continue
        if not _looks_like_product(url, config.product_patterns, map_min_segments):
            continue
        seen.add(url)
        filtered.append(url)

    runtime.logger.debug(
        "Firecrawl map discovery added %d URLs (raw=%d) for %s",
        len(filtered),
        len(urls),
        config.base_url,
    )
    return filtered


def _fetch_category_html(
    url: str, config: DiscoveryConfig, runtime: DiscoveryRuntime
) -> Optional[str]:
    try:
        payload = runtime.fetch_resource(url)
    except Exception as exc:  # noqa: BLE001
        runtime.logger.warning("Exception fetching category page %s: %s", url, exc)
        return None
    if not payload:
        runtime.logger.debug("Failed to fetch category page %s", url)
        return None

    try:
        html = payload.decode("utf-8", errors="ignore")
    except Exception:
        html = payload.decode("latin-1", errors="ignore")

    if config.playwright_enabled and looks_like_guard_html(html):
        runtime.logger.info("Guard detected for %s, attempting Playwright fallback", url)
        fallback = runtime.fetch_with_playwright(url, config.playwright_wait)
        if fallback:
            return fallback
        runtime.logger.warning(
            "Playwright fallback failed for %s; using guarded HTML", url
        )

    return html


def _fetch_resource(url: str) -> Optional[bytes]:
    fetchers: List[FetchFunc] = [lambda target: _http_request(target)]
    if curl_requests is not None:
        fetchers.append(lambda target: _curl_request(target))

    for fetch in fetchers:
        try:
            payload = fetch(url)
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.debug("Fetcher %s failed for %s: %s", fetch.__name__, url, exc)  # type: ignore[attr-defined]
            continue
        if payload:
            return payload
    return None


def _http_request(url: str) -> Optional[bytes]:
    response = requests.get(url, headers=DEFAULT_HEADERS, timeout=25)
    if response.status_code >= 400:
        logger.debug("HTTP request to %s returned status %s", url, response.status_code)
        return None
    content = response.content
    if response.headers.get("content-encoding") == "gzip" or url.endswith(".gz"):
        try:
            content = gzip.decompress(content)
        except OSError:
            pass
    return content


def _curl_request(url: str) -> Optional[bytes]:
    response = curl_requests.get(url, impersonate="chrome120", timeout=25)  # type: ignore
    if response.status_code >= 400:
        logger.debug("curl_cffi request to %s returned status %s", url, response.status_code)
        return None
    content = response.content
    if response.headers.get("content-encoding") == "gzip" or url.endswith(".gz"):
        try:
            content = gzip.decompress(content)
        except OSError:
            pass
    return content


def _parse_sitemap(payload: bytes) -> tuple[List[str], List[str]]:
    root = ET.fromstring(payload)

    def strip(tag: str) -> str:
        return tag.split("}", 1)[1] if "}" in tag else tag

    tag = strip(root.tag)
    if tag == "sitemapindex":
        nested: List[str] = []
        for loc in root.findall(".//{*}loc"):
            if loc.text:
                nested.append(loc.text.strip())
        return [], nested

    if tag == "urlset":
        urls: List[str] = []
        for url_elem in root.findall(".//{*}url"):
            loc = url_elem.find("{*}loc")
            if loc is not None and loc.text:
                urls.append(loc.text.strip())
        return urls, []

    return [], []


def _extract_product_links(
    html_text: str,
    page_url: str,
    patterns: Sequence[str],
    min_segments: int,
) -> List[str]:
    try:
        soup = BeautifulSoup(html_text, "html.parser")
    except Exception as exc:  # pragma: no cover
        logger.debug("BeautifulSoup failed for %s: %s", page_url, exc)
        return []

    links: List[str] = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href")
        if not href:
            continue
        absolute = urljoin(page_url, href.split("#", 1)[0])
        if not _looks_like_product(absolute, patterns, min_segments):
            continue
        links.append(absolute)
    return links


def _looks_like_product(url: str, patterns: Sequence[str], min_segments: int) -> bool:
    if is_product_url(url):
        if min_segments and _path_segments(url) < min_segments:
            return False
        return True

    lowered = url.lower()
    for pattern in patterns:
        if pattern.lower() in lowered:
            if min_segments and _path_segments(url) < min_segments:
                return False
            return True
    return False


def _path_segments(url: str) -> int:
    path = urlsplit(url).path.strip("/")
    if not path:
        return 0
    return path.count("/") + 1


def _attach_page(url: str, param: str, page: int) -> str:
    parsed = urlparse(url)
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    filtered = [(key, value) for key, value in query_pairs if key != param]
    filtered.append((param, str(page)))
    new_query = urlencode(filtered, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def _fetch_with_playwright(url: str, wait_seconds: float) -> Optional[str]:
    try:
        from playwright.async_api import async_playwright
    except ModuleNotFoundError:
        logger.debug("Playwright not installed; cannot bypass guard for %s", url)
        return None

    async def _run() -> Optional[str]:
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ]
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=launch_args)
            context = await browser.new_context(
                viewport={"width": 1366, "height": 768},
                locale="ru-RU",
                user_agent=DEFAULT_HEADERS["User-Agent"],
                extra_http_headers={"Accept-Language": DEFAULT_HEADERS["Accept-Language"]},
            )
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                if wait_seconds > 0:
                    await page.wait_for_timeout(wait_seconds * 1000)
                return await page.content()
            finally:
                await browser.close()

    try:
        return asyncio.run(_run())
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_run())
        finally:
            loop.close()
