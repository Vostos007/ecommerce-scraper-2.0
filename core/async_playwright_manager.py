import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)


class AsyncPlaywrightManager:
    """High-level async Playwright manager with browser/context/page pooling."""

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.config = config or {}
        self.logger = logger or logging.getLogger(__name__)

        pooling_cfg = self.config.get("browser_pooling", {})
        self.max_browsers = pooling_cfg.get("max_browsers", 3)
        self.max_contexts_per_domain = pooling_cfg.get("max_contexts_per_domain", 5)
        self.max_pages_per_context = pooling_cfg.get("max_pages_per_context", 10)
        self.browser_idle_timeout = pooling_cfg.get("browser_idle_timeout_seconds", 600)
        self.context_idle_timeout = pooling_cfg.get("context_idle_timeout_seconds", 300)
        self.page_idle_timeout = pooling_cfg.get("page_idle_timeout_seconds", 60)
        self.cleanup_interval = pooling_cfg.get("cleanup_interval_seconds", 120)

        perf_cfg = self.config.get("performance_settings", {})
        self.navigation_timeout_ms = perf_cfg.get("navigation_timeout_ms", 30_000)
        self.wait_for_selector_timeout_ms = perf_cfg.get(
            "wait_for_selector_timeout_ms", 10_000
        )
        self.default_wait_strategy = perf_cfg.get(
            "default_wait_strategy", "networkidle"
        )
        self.enable_request_interception = perf_cfg.get(
            "enable_request_interception", False
        )
        self.blocked_resources = set(perf_cfg.get("block_resources", []))

        resource_cfg = self.config.get("resource_optimization", {})
        self.max_concurrent_navigations = resource_cfg.get(
            "max_concurrent_navigations", 5
        )

        stealth_cfg = self.config.get("stealth_settings", {})
        self.enable_stealth_mode = stealth_cfg.get("enable_stealth_mode", True)
        self.randomize_viewport = stealth_cfg.get("randomize_viewport", True)
        self.randomize_user_agent_per_context = stealth_cfg.get(
            "randomize_user_agent_per_context", True
        )
        self.simulate_human_behavior = stealth_cfg.get("simulate_human_behavior", True)
        self.random_delay_cfg = stealth_cfg.get(
            "random_delays", {"min_ms": 100, "max_ms": 1000}
        )

        launch_options = (
            self.config.get("playwright_options", {})
            .get("launch_options", {})
        )
        self._default_ignore_https_errors = launch_options.get("ignore_https_errors")

        self.playwright: Optional[Playwright] = None
        self._playwright_lock = asyncio.Lock()
        self.browser_pool: Dict[str, List[Dict[str, Any]]] = {}
        self.context_pool: Dict[str, List[Dict[str, Any]]] = {}
        self.page_pool: Dict[str, List[Dict[str, Any]]] = {}
        self.active_pages: Dict[str, Dict[str, Any]] = {}
        self.metrics: Dict[str, Any] = {
            "browser_reuses": 0,
            "context_reuses": 0,
            "page_reuses": 0,
            "total_navigations": 0,
            "avg_navigation_time": 0.0,
            "resource_usage": [],
        }

        self._cleanup_task: Optional[asyncio.Task] = None
        self._navigation_semaphore = asyncio.Semaphore(self.max_concurrent_navigations)

    async def start(self) -> None:
        if self.playwright:
            return
        async with self._playwright_lock:
            if self.playwright:
                return
            self.logger.debug("Starting async Playwright")
            self.playwright = await async_playwright().start()
            if self.cleanup_interval:
                loop = asyncio.get_running_loop()
                self._cleanup_task = loop.create_task(self._periodic_cleanup())

    async def stop(self) -> None:
        if self._cleanup_task:
            self._cleanup_task.cancel()
            self._cleanup_task = None

        for context_entries in list(self.context_pool.values()):
            for entry in context_entries:
                await self._safe_close_context(entry["context"])
        self.context_pool.clear()

        for browser_entries in list(self.browser_pool.values()):
            for entry in browser_entries:
                await self._safe_close_browser(entry.get("browser"))
        self.browser_pool.clear()

        if self.playwright:
            await self.playwright.stop()
            self.playwright = None

    async def _periodic_cleanup(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.cleanup_interval)
                await self.cleanup_idle_resources()
        except asyncio.CancelledError:
            return

    async def cleanup_idle_resources(self) -> None:
        now = time.time()

        # Clean idle pages
        for context_id, pages in list(self.page_pool.items()):
            active_pages = []
            for page_entry in pages:
                if now - page_entry["last_used"] > self.page_idle_timeout:
                    await self._safe_close_page(page_entry["page"])
                else:
                    active_pages.append(page_entry)
            if active_pages:
                self.page_pool[context_id] = active_pages
            else:
                self.page_pool.pop(context_id, None)

        # Clean idle contexts
        for domain, contexts in list(self.context_pool.items()):
            active_contexts = []
            for entry in contexts:
                if now - entry["last_used"] > self.context_idle_timeout:
                    context_id = self._context_id(entry["context"])
                    self.page_pool.pop(context_id, None)
                    await self._safe_close_context(entry["context"])
                else:
                    active_contexts.append(entry)
            if active_contexts:
                self.context_pool[domain] = active_contexts
            else:
                self.context_pool.pop(domain, None)

        # Clean idle browsers
        for browser_type, browser_entries in list(self.browser_pool.items()):
            active_entries = []
            for entry in browser_entries:
                if now - entry.get("last_used", now) > self.browser_idle_timeout:
                    await self._safe_close_browser(entry.get("browser"))
                else:
                    active_entries.append(entry)
            if active_entries:
                self.browser_pool[browser_type] = active_entries
            else:
                self.browser_pool.pop(browser_type, None)

    async def get_optimized_browser_context(
        self,
        domain: Optional[str] = None,
        proxy: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> BrowserContext:
        await self.start()
        domain_key = domain or "__global__"

        # Attempt to reuse context
        context_entry = None
        contexts = self.context_pool.get(domain_key, [])
        for i, potential in enumerate(contexts):
            if not proxy or potential.get("proxy") == proxy:
                context_entry = contexts.pop(i)
                context_entry["last_used"] = time.time()
                self.metrics["context_reuses"] += 1
                break

        if not context_entry:
            context = await self._create_browser_context(
                proxy=proxy, user_agent=user_agent
            )
            context_entry = {
                "context": context,
                "last_used": time.time(),
                "proxy": proxy,
                "user_agent": user_agent,
            }

        self._register_context(domain_key, context_entry)
        return context_entry["context"]

    async def get_page_from_pool(
        self, context: Optional[BrowserContext] = None
    ) -> Page:
        if not context:
            context = await self.get_optimized_browser_context()

        context_id = self._context_id(context)
        pooled_pages = self.page_pool.get(context_id, [])
        page_entry = None
        if pooled_pages:
            page_entry = pooled_pages.pop(0)
            self.metrics["page_reuses"] += 1

        if not page_entry:
            page = await context.new_page()
            await self._apply_page_optimizations(page)
            page_entry = {"page": page, "last_used": time.time()}

        page = page_entry["page"]
        page_id = self._page_id(page)
        self.active_pages[page_id] = {
            "page": page,
            "context_id": context_id,
            "acquired": time.time(),
        }
        return page

    async def get_browser(
        self,
        browser_type: Optional[str] = None,
        force_new: bool = False,
    ) -> Browser:
        """Return a Playwright browser instance, optionally forcing a fresh launch."""

        await self.start()
        target_type = browser_type or self._browser_type
        if target_type != self._browser_type:
            raise ValueError(
                f"Unsupported browser type '{target_type}'."
            )

        if force_new:
            browser = await self._launch_browser(target_type)
            return browser

        return await self._get_browser()

    async def return_page_to_pool(self, page: Page, context: BrowserContext) -> None:
        context_id = self._context_id(context)
        page_id = self._page_id(page)
        self.active_pages.pop(page_id, None)

        if len(self.page_pool.get(context_id, [])) >= self.max_pages_per_context:
            await self._safe_close_page(page)
            return

        try:
            await page.goto("about:blank", wait_until="domcontentloaded", timeout=5_000)
            entry = {"page": page, "last_used": time.time()}
            self.page_pool.setdefault(context_id, []).append(entry)
        except Exception:
            await self._safe_close_page(page)

    async def navigate_with_optimization(
        self,
        page: Page,
        url: str,
        wait_for: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> bool:
        wait_for_strategy = wait_for or self.default_wait_strategy
        timeout_ms = timeout or self.navigation_timeout_ms

        start_time = time.time()
        async with self._navigation_semaphore:
            try:
                await page.goto(url, wait_until=wait_for_strategy, timeout=timeout_ms)
                success = True
            except PlaywrightTimeoutError:
                success = False
            except Exception:
                self.logger.exception(
                    "Playwright navigation failed", extra={"url": url}
                )
                success = False

        elapsed = time.time() - start_time
        self._record_navigation_metric(elapsed)
        return success

    async def __aenter__(self) -> "AsyncPlaywrightManager":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.stop()

    @asynccontextmanager
    async def page_context(
        self,
        domain: Optional[str] = None,
        proxy: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> AsyncIterator[Page]:
        context = await self.get_optimized_browser_context(domain, proxy, user_agent)
        page = await self.get_page_from_pool(context)
        try:
            yield page
        finally:
            await self.return_page_to_pool(page, context)

    async def _create_browser_context(
        self, proxy: Optional[str], user_agent: Optional[str]
    ) -> BrowserContext:
        browser = await self._get_browser()
        context_options = self._build_context_options(
            user_agent=user_agent, proxy=proxy
        )
        context = await browser.new_context(**context_options)
        await self._apply_context_optimizations(context)
        return context

    async def _get_browser(self) -> Browser:
        await self.start()
        browser_type = self._browser_type
        total_browsers = sum(len(pool) for pool in self.browser_pool.values())

        pool = self.browser_pool.setdefault(browser_type, [])
        per_type_limit = self.config.get("browser_pooling", {}).get(
            "max_browsers_per_type", self.max_browsers
        )

        # Global cap is enforced; optional max_browsers_per_type can further constrain per-type scaling.
        # First branch: create new browser if under both per-type and global caps
        if len(pool) < per_type_limit and total_browsers < self.max_browsers:
            launch_options = self._build_launch_options()
            browser = await getattr(self.playwright, browser_type).launch(
                **launch_options
            )
            entry = {"browser": browser, "last_used": time.time()}
            pool.append(entry)
            return browser

        # Second branch: reuse from pool if available
        if pool:
            lru_entry = min(pool, key=lambda e: e.get("last_used", 0))
            lru_entry["last_used"] = time.time()
            self.metrics["browser_reuses"] += 1
            return lru_entry["browser"]

        # Third branch: if pool empty but global cap reached, pick oldest across all types
        if total_browsers >= self.max_browsers:
            all_entries = [
                entry for pool in self.browser_pool.values() for entry in pool
            ]
            oldest_entry = min(all_entries, key=lambda e: e.get("last_used", 0))
            oldest_entry["last_used"] = time.time()
            self.metrics["browser_reuses"] += 1
            return oldest_entry["browser"]

        # Fallback: create first instance for this type (shouldn't reach here normally)
        launch_options = self._build_launch_options()
        browser = await getattr(self.playwright, browser_type).launch(**launch_options)
        entry = {"browser": browser, "last_used": time.time()}
        pool.append(entry)
        return browser

    async def _launch_browser(self, browser_type: str) -> Browser:
        launch_options = self._build_launch_options()
        browser = await getattr(self.playwright, browser_type).launch(
            **launch_options
        )
        pool = self.browser_pool.setdefault(browser_type, [])
        entry = {"browser": browser, "last_used": time.time()}
        pool.append(entry)
        await self._enforce_browser_limits()
        return browser

    async def _enforce_browser_limits(self) -> None:
        total_browsers = sum(len(pool) for pool in self.browser_pool.values())
        if total_browsers <= self.max_browsers:
            return

        # Close oldest browsers until within limit
        while total_browsers > self.max_browsers:
            oldest_entry = None
            oldest_key = None
            for key, pool in self.browser_pool.items():
                for entry in pool:
                    if not oldest_entry or entry["last_used"] < oldest_entry["last_used"]:
                        oldest_entry = entry
                        oldest_key = key
            if not oldest_entry or oldest_key is None:
                break

            self.browser_pool[oldest_key].remove(oldest_entry)
            await self._safe_close_browser(oldest_entry.get("browser"))
            if not self.browser_pool[oldest_key]:
                self.browser_pool.pop(oldest_key, None)
            total_browsers = sum(len(pool) for pool in self.browser_pool.values())

    async def _apply_page_optimizations(self, page: Page) -> None:
        if self.enable_request_interception or self.blocked_resources:
            await page.route("**/*", self._route_handler)

        if self.simulate_human_behavior:
            await page.evaluate(
                """
                () => {
                    window.__codexHumanized = true;
                }
            """
            )

    async def _apply_context_optimizations(self, context: BrowserContext) -> None:
        if not self.enable_stealth_mode:
            return

        await context.add_init_script(
            """
            () => {
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            }
        """
        )

    async def _route_handler(self, route, request) -> None:
        if request.resource_type in self.blocked_resources:
            await route.abort()
            return
        await route.continue_()

    def _register_context(self, domain_key: str, entry: Dict[str, Any]) -> None:
        contexts = self.context_pool.setdefault(domain_key, [])
        contexts.insert(0, entry)
        if len(contexts) > self.max_contexts_per_domain:
            stale = contexts.pop()
            context_id = self._context_id(stale["context"])
            self.page_pool.pop(context_id, None)
            asyncio.create_task(self._safe_close_context(stale["context"]))

    async def _safe_close_page(self, page: Page) -> None:
        try:
            await page.close()
        except Exception:
            self.logger.debug("Failed to close Playwright page", exc_info=True)

    async def _safe_close_context(self, context: BrowserContext) -> None:
        try:
            await context.close()
        except Exception:
            self.logger.debug("Failed to close Playwright context", exc_info=True)

    async def _safe_close_browser(self, browser: Optional[Browser]) -> None:
        if not browser:
            return
        try:
            await browser.close()
        except Exception:
            self.logger.debug("Failed to close Playwright browser", exc_info=True)

    def _build_launch_options(self) -> Dict[str, Any]:
        options = (
            self.config.get("playwright_options", {}).get("launch_options", {}).copy()
        )
        # remove unsupported launch option so mocks match expected signature
        ignore_https_errors = options.pop("ignore_https_errors", None)
        if ignore_https_errors is not None:
            self._default_ignore_https_errors = ignore_https_errors
        options.setdefault(
            "headless", self.config.get("playwright_options", {}).get("headless", True)
        )
        if self.config.get("playwright_options", {}).get("debug_mode"):
            options["headless"] = False
        return options

    def _build_context_options(
        self, user_agent: Optional[str], proxy: Optional[str]
    ) -> Dict[str, Any]:
        options = (
            self.config.get("playwright_options", {}).get("context_options", {}).copy()
        )
        if self.randomize_user_agent_per_context and user_agent:
            options["user_agent"] = user_agent
        elif user_agent:
            options.setdefault("user_agent", user_agent)

        if proxy:
            options["proxy"] = {"server": proxy}

        if (
            self._default_ignore_https_errors is not None
            and "ignore_https_errors" not in options
        ):
            options["ignore_https_errors"] = self._default_ignore_https_errors

        if self.randomize_viewport and "viewport" not in options:
            options["viewport"] = {"width": 1280, "height": 720}
        return options

    def _record_navigation_metric(self, elapsed: float) -> None:
        metrics = self.metrics
        total = metrics["total_navigations"] + 1
        running_avg = metrics["avg_navigation_time"]
        metrics["avg_navigation_time"] = running_avg + (elapsed - running_avg) / total
        metrics["total_navigations"] = total

    @property
    def _browser_type(self) -> str:
        return self.config.get("playwright_options", {}).get("browser_type", "chromium")

    def _context_id(self, context: BrowserContext) -> str:
        return f"ctx-{id(context)}"

    def _page_id(self, page: Page) -> str:
        return f"pg-{id(page)}"
