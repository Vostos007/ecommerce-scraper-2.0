import asyncio
import json
import logging
import re
import threading
from collections import defaultdict, deque
from dataclasses import dataclass, field
from statistics import mean
from typing import Any, Awaitable, Dict, List, Optional, TYPE_CHECKING
import psutil

from network.fast_scraper import FastHeadlessScraper

from core.async_playwright_manager import AsyncPlaywrightManager
from utils.cms_detection import CMSDetection, CMSConfig, CMSDetectionResult

if TYPE_CHECKING:  # pragma: no cover
    from core.antibot_manager import AntibotManager
    from playwright.async_api import BrowserContext


logger = logging.getLogger(__name__)


@dataclass
class ScrapingConfig:
    whitelist: List[str] = field(default_factory=list)
    blacklist: List[str] = field(default_factory=list)
    js_indicators: List[str] = field(
        default_factory=lambda: [
            r"react[-_]",
            r"angular",
            r"vue",
            r"svelte",
            r"<script[^>]+type=\"module\"",
        ]
    )
    complexity_indicators: Dict[str, List[str]] = field(
        default_factory=lambda: {
            "spa_patterns": [r"#/", r"#!/", r"router", r"state"],
            "ajax_patterns": [r"fetch\(", r"XMLHttpRequest", r"$.ajax", r"axios"],
            "framework_patterns": [r"ng-", r"data-react", r"data-vue"],
        }
    )
    force_playwright: List[str] = field(default_factory=list)
    force_aiohttp: List[str] = field(default_factory=list)


@dataclass
class MethodMetrics:
    success: int = 0
    failure: int = 0
    latencies: deque = field(default_factory=lambda: deque(maxlen=50))

    def record(self, succeeded: bool, latency: float) -> None:
        if succeeded:
            self.success += 1
        else:
            self.failure += 1
        self.latencies.append(latency)

    @property
    def success_rate(self) -> float:
        total = self.success + self.failure
        return self.success / total if total else 0.0

    @property
    def average_latency(self) -> float:
        return mean(self.latencies) if self.latencies else 0.0


class HybridScrapingEngine:
    """Hybrid scraping engine with intelligent method selection and optimized Playwright integration."""

    def __init__(
        self,
        config_path: str = "config/settings.json",
        antibot_manager: Optional["AntibotManager"] = None,
    ):
        self.config_path = config_path
        self.settings = self._load_settings()
        self.config = self.settings  # Backward compatibility
        self.scraping_config = self._build_scraping_config(self.settings)
        self.intelligent_selection = self.settings.get(
            "intelligent_method_selection", {}
        ).get("enabled", True)

        # Precompile regex patterns for performance
        self.js_indicators_compiled = [
            re.compile(re.escape(p), re.IGNORECASE)
            for p in self.scraping_config.js_indicators
        ]
        self.complexity_indicators_compiled = {
            key: [re.compile(re.escape(pattern), re.IGNORECASE) for pattern in patterns]
            for key, patterns in self.scraping_config.complexity_indicators.items()
        }

        self.playwright_manager = AsyncPlaywrightManager(self.settings, logger=logger)
        cms_config = self._build_cms_config(self.settings)
        self.cms_detector = CMSDetection(cms_config)
        self.antibot_manager = antibot_manager

        self.stats: Dict[str, Any] = {
            "aiohttp_success": 0,
            "playwright_success": 0,
            "aiohttp_failures": 0,
            "playwright_failures": 0,
            "total_time": 0.0,
        }
        self.override_methods: Dict[str, str] = {}
        self.method_performance: Dict[str, Dict[str, MethodMetrics]] = defaultdict(
            lambda: {"aiohttp": MethodMetrics(), "playwright": MethodMetrics()}
        )
        self.domain_preferences: Dict[str, str] = {}
        self.recent_failures: Dict[str, Dict[str, deque]] = defaultdict(
            lambda: {"aiohttp": deque(maxlen=10), "playwright": deque(maxlen=10)}
        )

        # Dedicated background event loop for synchronous wrappers
        self._loop = asyncio.new_event_loop()
        self._loop_ready = threading.Event()
        self._loop_thread = threading.Thread(
            target=self._run_event_loop,
            name="HybridScrapingEngineLoop",
            daemon=True,
        )
        self._loop_thread.start()

    def _run_event_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop_ready.set()
        self._loop.run_forever()

    def _run_coroutine_sync(self, coro: Awaitable[Any]) -> Any:
        if not self._loop_ready.is_set():
            self._loop_ready.wait()
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()

    def shutdown(self) -> None:
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._loop_thread.is_alive():
            self._loop_thread.join(timeout=5)
        if not self._loop.is_closed():
            self._loop.close()

    def __del__(self):  # pragma: no cover - best effort cleanup
        try:
            if self._loop.is_closed():
                return
            self.shutdown()
        except Exception:  # noqa: BLE001
            pass

    def _load_settings(self) -> Dict[str, Any]:
        try:
            with open(self.config_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Failed to load settings.json: {exc}")
            return {}

    def _build_scraping_config(self, settings: Dict[str, Any]) -> ScrapingConfig:
        js_detection = settings.get("js_detection", {})
        return ScrapingConfig(
            whitelist=js_detection.get("force_aiohttp_domains", []),
            blacklist=js_detection.get("force_playwright_domains", []),
            js_indicators=js_detection.get(
                "js_indicators", ScrapingConfig().js_indicators
            ),
            complexity_indicators=js_detection.get(
                "complexity_indicators", ScrapingConfig().complexity_indicators
            ),
            force_playwright=js_detection.get("force_playwright_domains", []),
            force_aiohttp=js_detection.get("force_aiohttp_domains", []),
        )

    def _build_cms_config(self, settings: Dict[str, Any]) -> CMSConfig:
        cms_settings = settings.get("cms_detection", {})
        detection_methods = cms_settings.get("detection_methods")
        return CMSConfig(
            enable_version_detection=cms_settings.get("enable_version_detection", True),
            enable_plugin_detection=cms_settings.get("enable_plugin_detection", True),
            enable_file_probing=cms_settings.get("enable_file_probing", True),
            confidence_threshold=cms_settings.get("confidence_threshold", 0.6),
            max_detection_time=cms_settings.get("max_detection_time", 30.0),
            custom_cms_patterns=cms_settings.get("custom_cms_patterns"),
            detection_methods=detection_methods,
            method_weights=cms_settings.get("method_weights"),
        )

    async def detect_scraping_method(self, url: str) -> str:
        return await self.detect_optimal_scraping_method(url)

    async def detect_optimal_scraping_method(
        self, url: str, html_preview: Optional[str] = None
    ) -> str:
        domain = self._extract_domain(url)

        # Manual override for full URL or domain
        if url in self.override_methods:
            return self.override_methods[url]
        if domain in self.domain_preferences:
            return self.domain_preferences[domain]

        if domain in self.scraping_config.force_aiohttp:
            return "aiohttp"
        if domain in self.scraping_config.force_playwright:
            return "playwright"

        # Check fallback rules for recent failures
        intelligent_selection = self.settings.get("intelligent_method_selection", {})
        fallback_rules = intelligent_selection.get("fallback_rules", {})
        max_aiohttp_failures = fallback_rules.get("max_aiohttp_failures", 3)
        max_playwright_failures = fallback_rules.get("max_playwright_failures", 2)

        recent_aiohttp_failures = len(
            [f for f in self.recent_failures[domain]["aiohttp"] if f]
        )
        recent_playwright_failures = len(
            [f for f in self.recent_failures[domain]["playwright"] if f]
        )

        if recent_aiohttp_failures >= max_aiohttp_failures:
            logger.warning(
                f"AIOHTTP method disabled for {domain} due to {recent_aiohttp_failures} recent failures"
            )
            return "playwright"
        if recent_playwright_failures >= max_playwright_failures:
            logger.warning(
                f"Playwright method disabled for {domain} due to {recent_playwright_failures} recent failures"
            )
            return "aiohttp"

        preview_html = html_preview or await self._fetch_preview_html(url)
        cms_result = self._detect_cms(url, preview_html)

        # Get weights from settings
        factors = intelligent_selection.get("factors", {})
        cms_weight = factors.get("cms_type_weight", 0.4)
        performance_weight = factors.get("performance_history_weight", 0.3)
        resource_weight = factors.get("resource_usage_weight", 0.2)
        complexity_weight = factors.get("site_complexity_weight", 0.1)

        method_scores = {"aiohttp": 0.0, "playwright": 0.0}

        # CMS-based heuristics
        if cms_result and cms_result.cms_type:
            js_heavy_cms = {"shopify", "magento", "vue_storefront", "squarespace"}
            if cms_result.cms_type.lower() in js_heavy_cms:
                method_scores["playwright"] += cms_weight * cms_result.confidence
            else:
                method_scores["aiohttp"] += cms_weight * cms_result.confidence

        # JS indicator scanning (site complexity)
        if preview_html:
            if self._contains_js_indicators(preview_html):
                method_scores["playwright"] += complexity_weight
            else:
                method_scores["aiohttp"] += complexity_weight * 0.4

            if self._contains_complexity_signals(preview_html):
                method_scores["playwright"] += complexity_weight * 0.6

        # Historical performance
        metrics = self.method_performance[domain]
        method_scores["aiohttp"] += metrics["aiohttp"].success_rate * performance_weight
        method_scores["playwright"] += (
            metrics["playwright"].success_rate * performance_weight
        )

        # Latency consideration (prefer faster method if comparable success)
        if metrics["aiohttp"].average_latency and metrics["playwright"].average_latency:
            latency_diff = (
                metrics["playwright"].average_latency
                - metrics["aiohttp"].average_latency
            )
            if latency_diff > 0:  # aiohttp is faster
                method_scores["aiohttp"] += performance_weight * 0.2
            else:  # playwright is faster
                method_scores["playwright"] += performance_weight * 0.2

        # Resource usage component
        try:
            cpu_usage = psutil.cpu_percent(interval=0.1)
            memory_usage = psutil.virtual_memory().percent

            # Prefer aiohttp when resources are high (lighter method)
            resource_penalty = (cpu_usage + memory_usage) / 200.0  # Normalize to 0-1
            method_scores["aiohttp"] += resource_weight * (1 - resource_penalty)
            method_scores["playwright"] += resource_weight * resource_penalty
        except Exception as exc:
            logger.debug(f"Failed to get resource usage: {exc}")

        selected = max(method_scores, key=lambda k: method_scores[k])
        self.domain_preferences[domain] = selected
        return selected

    async def scrape_single(
        self, url: str, method: Optional[str] = None, **kwargs
    ) -> Dict[str, Any]:
        domain = self._extract_domain(url)
        selected_method = method or await self.detect_optimal_scraping_method(url)
        logger.info(f"Selected method '{selected_method}' for {url}")

        start_ts = asyncio.get_event_loop().time()

        try:
            if selected_method == "playwright":
                result = await self.scrape_with_playwright_optimized(url, **kwargs)
            else:
                result = await self.scrape_with_aiohttp(url, **kwargs)

            latency = asyncio.get_event_loop().time() - start_ts
            self._record_method_metrics(domain, selected_method, True, latency)
            return result

        except Exception as primary_exc:  # noqa: BLE001
            latency = asyncio.get_event_loop().time() - start_ts
            self._record_method_metrics(domain, selected_method, False, latency)
            fallback_method = (
                "playwright" if selected_method == "aiohttp" else "aiohttp"
            )
            logger.warning(
                f"Primary method '{selected_method}' failed for {url}, attempting fallback '{fallback_method}': {primary_exc}"
            )
            try:
                fallback_start = asyncio.get_event_loop().time()
                if fallback_method == "playwright":
                    result = await self.scrape_with_playwright_optimized(url, **kwargs)
                else:
                    result = await self.scrape_with_aiohttp(url, **kwargs)
                fallback_latency = asyncio.get_event_loop().time() - fallback_start
                self._record_method_metrics(
                    domain, fallback_method, True, fallback_latency
                )
                return result
            except Exception as fallback_exc:  # noqa: BLE001
                self._record_method_metrics(domain, fallback_method, False, latency)
                logger.error(
                    f"Fallback method '{fallback_method}' also failed for {url}: {fallback_exc}"
                )
                raise

    async def scrape_with_playwright_optimized(
        self,
        url: str,
        context: Optional["BrowserContext"] = None,
        wait_for: Optional[str] = None,
        proxy: Optional[str] = None,
    ) -> Dict[str, Any]:
        domain = self._extract_domain(url)
        manager = (
            self.antibot_manager.playwright_manager
            if self.antibot_manager and self.antibot_manager.playwright_manager
            else self.playwright_manager
        )

        if self.antibot_manager and self.antibot_manager.playwright_manager:
            context = context or await self.antibot_manager.create_stealth_context(
                domain, proxy=proxy
            )
            page = await self.antibot_manager.playwright_manager.get_page_from_pool(
                context
            )
        else:
            context = context or await manager.get_optimized_browser_context(
                domain=domain, proxy=proxy
            )
            page = await manager.get_page_from_pool(context)

        try:
            navigation_success = False
            if self.antibot_manager and self.antibot_manager.playwright_manager:
                navigation_success = await self.antibot_manager.navigate_with_antibot(
                    page, url, wait_for=wait_for
                )
            else:
                navigation_success = await manager.navigate_with_optimization(
                    page, url, wait_for=wait_for
                )

            if not navigation_success:
                raise RuntimeError(f"Navigation failed for {url}")

            html = await page.content()
            return {"html": html, "url": url, "method": "playwright"}
        finally:
            if self.antibot_manager and self.antibot_manager.playwright_manager:
                await self.antibot_manager.playwright_manager.return_page_to_pool(
                    page, context
                )
            else:
                await manager.return_page_to_pool(page, context)

    async def scrape_with_aiohttp(self, url: str, **kwargs) -> Dict[str, Any]:
        async with FastHeadlessScraper() as scraper:
            fetch_result = await scraper.fetch_url(
                url, use_proxy=kwargs.get("use_proxy", True), use_curl=False
            )
            if not fetch_result:
                raise RuntimeError(f"Failed to fetch {url} with aiohttp")
            html, _, _ = fetch_result
            return {"html": html, "url": url, "method": "aiohttp"}

    async def batch_scrape(
        self,
        urls: List[str],
        method: Optional[str] = None,
        batch_size: int = 10,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        return await self.batch_scrape_optimized(
            urls, method=method, batch_size=batch_size, **kwargs
        )

    async def batch_scrape_optimized(
        self,
        urls: List[str],
        method: Optional[str] = None,
        batch_size: int = 10,
        **kwargs,
    ) -> List[Any]:
        if not urls:
            return []

        decisions = []
        if method is not None:
            decisions = [method] * len(urls)
        else:
            decisions = await asyncio.gather(
                *(self.detect_optimal_scraping_method(url) for url in urls)
            )

        grouped: Dict[str, List[str]] = {"aiohttp": [], "playwright": []}
        for url, decision in zip(urls, decisions):
            grouped[decision].append(url)

        results: List[Any] = []

        # Process aiohttp in batches grouped by domain
        if grouped["aiohttp"]:
            domain_groups: Dict[str, List[str]] = defaultdict(list)
            for url in grouped["aiohttp"]:
                domain_groups[self._extract_domain(url)].append(url)

            for domain_urls in domain_groups.values():
                async with FastHeadlessScraper() as scraper:
                    batch_result = await scraper.fetch_urls_batch(
                        domain_urls, batch_size=batch_size
                    )
                    for url in domain_urls:
                        value = batch_result.get(url)
                        if value:
                            html, _, _ = value
                            results.append(
                                {"html": html, "url": url, "method": "aiohttp"}
                            )
                            self._record_method_metrics(
                                self._extract_domain(url), "aiohttp", True, 0.0
                            )
                        else:
                            results.append(Exception(f"Failed to fetch {url}"))
                            self._record_method_metrics(
                                self._extract_domain(url), "aiohttp", False, 0.0
                            )
                await asyncio.sleep(0.1)

        # Process playwright URLs with bounded concurrency using semaphore, maintaining domain grouping
        if grouped["playwright"]:
            concurrency_limit = self.playwright_manager.max_concurrent_navigations
            semaphore = asyncio.Semaphore(concurrency_limit)

            playwright_domain_groups: Dict[str, List[str]] = defaultdict(list)
            for url in grouped["playwright"]:
                domain = self._extract_domain(url)
                playwright_domain_groups[domain].append(url)

            for domain, urls in playwright_domain_groups.items():

                async def scrape_with_semaphore(url: str):
                    async with semaphore:
                        try:
                            result = await self.scrape_with_playwright_optimized(
                                url, **kwargs
                            )
                            self._record_method_metrics(
                                self._extract_domain(url), "playwright", True, 0.0
                            )
                            return result
                        except Exception as exc:  # noqa: BLE001
                            self._record_method_metrics(
                                self._extract_domain(url), "playwright", False, 0.0
                            )
                            return exc

                tasks = [scrape_with_semaphore(url) for url in urls]
                domain_results = await asyncio.gather(*tasks)
                results.extend(domain_results)

        return results

    def set_override(self, url: str, method: str) -> None:
        self.override_methods[url] = method

    def get_stats(self) -> Dict[str, Any]:
        return self.stats

    def sync_scrape(
        self, url: str, method: Optional[str] = None, **kwargs
    ) -> Dict[str, Any]:
        return self._run_coroutine_sync(
            self.scrape_single(url, method=method, **kwargs)
        )

    def sync_batch_scrape(
        self, urls: List[str], method: Optional[str] = None, **kwargs
    ) -> List[Any]:
        return self._run_coroutine_sync(
            self.batch_scrape_optimized(urls, method=method, **kwargs)
        )

    def sync_detect_optimal_method(self, url: str) -> str:
        return self._run_coroutine_sync(self.detect_optimal_scraping_method(url))

    async def _fetch_preview_html(self, url: str) -> Optional[str]:
        try:
            async with FastHeadlessScraper() as scraper:
                fetch_result = await scraper.fetch_url(
                    url, use_proxy=True, use_curl=False
                )
                if fetch_result:
                    html, _, _ = fetch_result
                    return html
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"Failed to fetch preview HTML for {url}: {exc}")
        return None

    def _detect_cms(
        self, url: str, html_preview: Optional[str]
    ) -> Optional[CMSDetectionResult]:
        try:
            return self.cms_detector.detect_cms_by_patterns(url=url, html=html_preview)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"CMS detection failed for {url}: {exc}")
            return None

    def _contains_js_indicators(self, html: str) -> bool:
        lowered = html.lower()
        for compiled in self.js_indicators_compiled:
            if compiled.search(lowered):
                return True
        return False

    def _contains_complexity_signals(self, html: str) -> bool:
        lowered = html.lower()
        for patterns in self.complexity_indicators_compiled.values():
            for compiled in patterns:
                if compiled.search(lowered):
                    return True
        return False

    def _record_method_metrics(
        self, domain: str, method: str, success: bool, latency: float
    ) -> None:
        metrics = self.method_performance[domain][method]
        metrics.record(success, latency)

        # Record recent failure for fallback rules
        self.recent_failures[domain][method].append(not success)

        stat_key = f"{method}_{'success' if success else 'failures'}"
        if stat_key in self.stats:
            self.stats[stat_key] += 1

    def _extract_domain(self, url: str) -> str:
        from urllib.parse import urlparse

        return urlparse(url).netloc.lower()
