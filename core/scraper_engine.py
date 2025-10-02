import asyncio
import contextlib
import importlib
import inspect
import json
import logging
import time
from datetime import UTC, datetime
from urllib.parse import urljoin, urlparse
from types import SimpleNamespace
import psutil  # For resource monitoring
from typing import Dict, List, Optional, Any, Tuple
from pathlib import Path

# Импорт типов из нашего модуля types
from .types import (
    URL,
    ProductID,
    ConfigDict,
    ScrapeResult,
    ProductData,
    ScrapingMethod,
    ProcessingMode,
    PerformanceMetrics,
    ProgressCallback,
    ProgressEvent,
    PHASE_COMPLETE,
    PHASE_DISCOVERY,
    PHASE_SCRAPING,
)

from .antibot_manager import AntibotManager
from .sitemap_analyzer import SitemapAnalyzer
from .batch_processor import BatchProcessor
from database.manager import DatabaseManager
from utils.logger import setup_logger
from .hybrid_engine import HybridScrapingEngine
from .async_playwright_manager import AsyncPlaywrightManager
from network.httpx_scraper import ModernHttpxScraper
from network.firecrawl_client import FirecrawlClient
from database.history_writer import (
    append_history_records,
    export_site_history_to_csv,
    export_site_history_to_json,
)
from utils.export_writers import write_product_exports
from utils.helpers import human_delay as _human_delay_helper

def human_delay(duration: float = 0.0) -> None:
    """Convenience wrapper to allow patching in tests."""

    _human_delay_helper(duration)


class ScraperEngine:
    """
    Главный движок скрапинга для CompetitorMonitor RU.

    Управляет всеми аспектами скрапинга: HTTP/2 запросы, Playwright,
    анти-бот защита, мониторинг складских остатков и уведомления.
    """

    # Типизированные атрибуты класса
    config_path: str
    config: ConfigDict
    logger: logging.Logger

    # Основные компоненты
    scraper_backend: ScrapingMethod
    use_hybrid: bool
    intelligent_selection: bool
    method_performance: Dict[str, PerformanceMetrics]

    # Движки скрапинга
    playwright_manager: Optional[AsyncPlaywrightManager]
    httpx_scraper: Optional[Any]  # HttpxScraper
    fast_scraper: Optional[Any]  # FastScraper
    hybrid_engine: Optional[HybridScrapingEngine]

    # Системы мониторинга
    stock_monitor: Optional[Any]  # StockMonitor
    webhook_notifier: Optional[Any]  # WebhookNotifier

    # Batch обработка
    batch_enabled: bool
    batch_processor: Optional[BatchProcessor]

    # Базовые компоненты
    antibot: AntibotManager
    analyzer: SitemapAnalyzer
    parser: Any  # ProductParser
    db: DatabaseManager

    # Метрики производительности
    batch_metrics: PerformanceMetrics
    sequential_metrics: PerformanceMetrics
    processing_mode: ProcessingMode

    def __init__(self, config_path: str = "config/settings.json") -> None:
        """Initialize ScraperEngine with configuration and components."""
        self.config_path = config_path
        self.config = self._load_config()
        self.logger = self._setup_logger()
        self.firecrawl_client: Optional[FirecrawlClient] = None

        # Initialize core configuration
        self._init_core_config()

        # Initialize scraping engines
        self._init_scraping_engines()

        # Initialize monitoring systems
        self._init_monitoring_systems()

        # Initialize batch processing
        self._init_batch_processing()

        # Initialize basic components (antibot, db, etc.)
        self._init_basic_components()

        # Finalize batch processor after basic components
        self._finalize_batch_processor()

        # Initialize performance metrics
        self._init_performance_metrics()

        # Runtime state placeholders
        self._current_base_url: Optional[URL] = None
        self._notification_email: Optional[str] = None
        self._last_scrape_metadata: Dict[str, Any] = {}
        self._last_used_method: ScrapingMethod = self.scraper_backend
        self._timeout_override: Optional[int] = None
        self._cached_urls_override: Optional[List[URL]] = None
        self._skip_cache_refresh: bool = False
        self._progress_callback: Optional[ProgressCallback] = None
        self._current_scrape_total: int = 0

    def _load_config(self) -> ConfigDict:
        """Load configuration from JSON file."""
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            self.logger.error(f"Configuration file not found: {self.config_path}")
            return {}
        except json.JSONDecodeError as e:
            self.logger.error(f"Error parsing configuration file: {e}")
            return {}

    def _emit_progress(
        self,
        phase: str,
        current: int,
        total: int,
        message: Optional[str] = None,
    ) -> None:
        if not self._progress_callback:
            return
        try:
            event = ProgressEvent(
                phase=phase,
                current=max(0, current),
                total=max(total, 0),
                message=message,
            )
            self._progress_callback(event)
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("Progress callback raised an exception: %s", exc)

    def _setup_logger(self) -> logging.Logger:
        """Setup logger for the scraper engine."""
        return setup_logger("scraper_engine", level=logging.INFO)

    def _init_core_config(self) -> None:
        """Initialize core configuration settings."""
        scraping_config = self.config.get("scraping", {})
        self.scraper_backend = ScrapingMethod(scraping_config.get("backend", "httpx"))
        self.use_hybrid = scraping_config.get("use_hybrid", False)

        # Advanced performance options
        performance_config = scraping_config.get("performance", {})
        self.intelligent_selection = performance_config.get(
            "intelligent_selection", False
        )

        # Initialize method performance tracking
        self.method_performance = {}

    def _init_scraping_engines(self) -> None:
        """Initialize scraping engines based on configuration."""
        # Initialize Playwright manager
        self.playwright_manager = AsyncPlaywrightManager(
            config=self.config.get("playwright", {}), logger=self.logger
        )

        # Initialize HTTPX scraper if needed
        self.httpx_scraper = None  # Will be initialized when needed

        # Initialize fast scraper and hybrid engine
        self._init_fast_scraper()
        self._init_hybrid_engine()

    def _init_fast_scraper(self) -> None:
        """Initialize fast scraper for HTTP-only scraping."""
        try:
            from network.fast_scraper import FastScraper

            self.fast_scraper = FastScraper()
        except ImportError:
            self.logger.warning("FastScraper not available")
            self.fast_scraper = None

    def _init_hybrid_engine(self) -> None:
        """Initialize hybrid scraping engine."""
        if self.use_hybrid:
            self.hybrid_engine = HybridScrapingEngine(
                config=self.config, logger=self.logger
            )
        else:
            self.hybrid_engine = None

    def _init_monitoring_systems(self) -> None:
        """Initialize monitoring systems (stock, webhooks)."""
        self._init_stock_monitor()
        self._init_webhook_notifier()

    def _init_stock_monitor(self) -> None:
        """Initialize stock monitoring system."""
        stock_config = self.config.get("stock_monitoring", {})
        if stock_config.get("enabled", False):
            try:
                from monitoring.stock_monitor import StockMonitor

                self.stock_monitor = StockMonitor(
                    config=stock_config,
                    db_manager=None,  # Will be set later
                    logger=self.logger,
                )
            except ImportError:
                self.logger.warning("StockMonitor not available")
                self.stock_monitor = None
            except Exception as exc:
                self.logger.warning(
                    "Stock monitoring disabled due to initialization error: %s", exc
                )
                self.stock_monitor = None
        else:
            self.stock_monitor = None

    def _init_webhook_notifier(self) -> None:
        """Initialize webhook notification system."""

        webhook_config_raw = self.config.get("webhook_notifications")
        validated_config = self._validate_webhook_config(webhook_config_raw)

        if validated_config is None:
            self.webhook_notifier = None
            return

        try:
            from notifications.webhook_notifier import WebhookNotifier

            wrapped_config = {"notifications": validated_config}
            self.webhook_notifier = WebhookNotifier(
                config=wrapped_config,
                logger=self.logger,
            )
        except ImportError:
            self.logger.warning("WebhookNotifier not available")
            self.webhook_notifier = None
        except Exception as exc:
            self.logger.warning(
                "Webhook notifications disabled due to initialization error: %s",
                exc,
            )
            self.webhook_notifier = None

    def _validate_webhook_config(self, config: Any) -> Optional[ConfigDict]:
        if not isinstance(config, dict):
            if config is not None:
                self.logger.warning("Webhook configuration must be a mapping; got %s", type(config).__name__)
            return None

        if not config.get("enabled", False):
            return None

        sanitized: ConfigDict = dict(config)

        endpoints = sanitized.get("endpoints", [])
        if not isinstance(endpoints, list):
            self.logger.warning("Webhook endpoints should be a list; skipping invalid configuration")
            endpoints = []

        valid_endpoints: List[ConfigDict] = []
        for endpoint in endpoints:
            if not isinstance(endpoint, dict):
                self.logger.warning("Skipping webhook endpoint with invalid structure: %r", endpoint)
                continue
            url = endpoint.get("url")
            if not url:
                self.logger.warning("Skipping webhook endpoint missing 'url': %r", endpoint)
                continue
            channel = endpoint.get("channel", "webhook")
            endpoint["channel"] = channel
            valid_endpoints.append(endpoint)

        if not valid_endpoints:
            self.logger.warning("Webhook notifications enabled but no valid endpoints were configured")
            return None

        sanitized["endpoints"] = valid_endpoints

        templates = sanitized.get("templates", {})
        if templates is None:
            templates = {}
        if not isinstance(templates, dict):
            self.logger.warning("Webhook templates should be a mapping; ignoring invalid templates configuration")
            templates = {}
        sanitized["templates"] = templates

        return sanitized

    def _init_batch_processing(self) -> None:
        """Initialize batch processing configuration."""
        batch_config = self.config.get("batch_processing", {})
        self.batch_enabled = batch_config.get("enabled", False)

        if self.batch_enabled:
            self._validate_batch_config(batch_config)
            self._check_system_resources()

        self.batch_processor = None  # Will be initialized after basic components

    def _validate_batch_config(self, batch_config: ConfigDict) -> None:
        """Validate batch processing configuration."""
        required_keys = ["batch_size", "max_concurrent"]
        for key in required_keys:
            if key not in batch_config:
                self.logger.warning(f"Missing batch config key: {key}")

        batch_size = batch_config.get("batch_size", 10)
        if batch_size > 100:
            self.logger.warning(f"Large batch size detected: {batch_size}")

    def _check_system_resources(self) -> None:
        """Check system resources for batch processing."""
        memory = psutil.virtual_memory()
        if memory.percent > 80:
            self.logger.warning(f"High memory usage: {memory.percent}%")

    def _init_basic_components(self) -> None:
        """Initialize basic components (antibot, parser, db)."""
        try:
            self.antibot = AntibotManager(self.config_path)
        except Exception as exc:
            self.logger.warning(
                "AntibotManager initialization failed; using no-op manager: %s", exc
            )
            async def _async_noop(*_args, **_kwargs):
                return None

            self.antibot = SimpleNamespace(
                fetch_sitemap=lambda *_: "",
                get_headers=lambda *_: {},
                get_proxy=lambda *_: None,
                make_request_with_retry=_async_noop,
                async_human_delay=_async_noop,
            )

        try:
            analyzer_module = importlib.import_module("core.sitemap_analyzer")
            analyzer_cls = getattr(analyzer_module, "SitemapAnalyzer")
            self.analyzer = analyzer_cls(
                self.antibot, self.config.get("base_url", "")
            )
        except Exception as exc:
            self.logger.warning(
                "SitemapAnalyzer initialization failed; using lightweight stub: %s",
                exc,
            )
            self.analyzer = SimpleNamespace(
                set_base_url=lambda *_: None,
                get_product_urls_from_sitemap=lambda *_1, **_2: [],
            )

        firecrawl_config = self.config.get("firecrawl", {})
        if isinstance(firecrawl_config, dict):
            try:
                self.firecrawl_client = FirecrawlClient(firecrawl_config)
            except Exception as exc:  # noqa: BLE001
                self.logger.warning(
                    "Firecrawl client initialization failed; continuing without fallback: %s",
                    exc,
                )
                self.firecrawl_client = None
        else:
            self.firecrawl_client = None

        try:
            from parsers.product_parser import ProductParser

            self.parser = ProductParser(
                antibot_manager=self.antibot,
                sitemap_analyzer=self.analyzer,
                html="",
                firecrawl_client=self.firecrawl_client,
            )
        except Exception as exc:
            self.logger.warning(
                "ProductParser initialization failed; built-in fallback will be used: %s",
                exc,
            )
            self.parser = SimpleNamespace(
                parse_product=lambda *_: None,
                parse_product_page=lambda *_: None,
                is_product_url=lambda *_: False,
                is_category_url=lambda *_: False,
                extract_product_links=lambda *_: [],
            )

        noop_db = SimpleNamespace(
            init_db=lambda: None,
            insert_product=lambda *args, **kwargs: None,
            insert_variations=lambda *args, **kwargs: None,
            record_batch_result=lambda *args, **kwargs: None,
            close=lambda: None,
        )

        db_config = self.config.get("database", {})

        self.db = noop_db

        should_enable_db = db_config.get("enabled")
        if should_enable_db is not False:
            try:
                config_payload = db_config if db_config else None
                database_module = importlib.import_module("database.manager")
                database_manager_cls = getattr(database_module, "DatabaseManager")
                self.db = database_manager_cls(config=config_payload)
            except Exception as exc:
                self.logger.warning(
                    "DatabaseManager failed to initialize; using in-memory stub: %s",
                    exc,
                )
                self.db = noop_db

    def _finalize_batch_processor(self) -> None:
        """Finalize batch processor with basic components."""
        if self.batch_enabled:
            self.batch_processor = BatchProcessor(
                config=self.config.get("batch_processing", {}),
                db_manager=self.db,
                logger=self.logger,
            )

    def _init_performance_metrics(self) -> None:
        """Initialize performance metrics tracking."""
        self.batch_metrics = {
            "total_requests": 0,
            "successful_requests": 0,
            "failed_requests": 0,
            "avg_response_time": 0.0,
        }

        self.sequential_metrics = {
            "total_requests": 0,
            "successful_requests": 0,
            "failed_requests": 0,
            "avg_response_time": 0.0,
        }

        self.processing_mode = ProcessingMode.SEQUENTIAL

    async def _maybe_await(self, value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    async def _maybe_call_method(
        self, obj: Any, method_name: str, *args: Any, **kwargs: Any
    ) -> Any:
        if not obj:
            return None
        method = getattr(obj, method_name, None)
        if not callable(method):
            return None
        return await self._maybe_await(method(*args, **kwargs))

    def run_scrape(
        self,
        base_url: URL,
        email: str,
        max_products: Optional[int] = None,
        output_format: str = "json",
        enable_stock_monitor: Optional[bool] = None,
        timeout_override: Optional[int] = None,
        cached_urls_override: Optional[List[URL]] = None,
        skip_cache_refresh: bool = False,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> ScrapeResult:
        """Synchronous wrapper that executes the async scraping workflow."""

        self._validate_base_url(base_url)

        (
            resolved_max_products,
            resolved_stock_monitor,
            resolved_timeout,
            resolved_cached_urls,
            resolved_skip_cache_refresh,
        ) = self._resolve_scrape_parameters(
            max_products,
            enable_stock_monitor,
            timeout_override,
            cached_urls_override,
            skip_cache_refresh,
        )

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.run_scrape_async(
                    base_url=base_url,
                    email=email,
                    max_products=resolved_max_products,
                    output_format=output_format,
                    enable_stock_monitor=resolved_stock_monitor,
                    timeout_override=resolved_timeout,
                    cached_urls_override=resolved_cached_urls,
                    skip_cache_refresh=resolved_skip_cache_refresh,
                    progress_callback=progress_callback,
                )
            )

        raise RuntimeError(
            "run_scrape() cannot be called from an active event loop. Use run_scrape_async()."
        )

    async def run_scrape_async(
        self,
        base_url: URL,
        email: str,
        max_products: Optional[int] = None,
        output_format: str = "json",
        enable_stock_monitor: Optional[bool] = None,
        timeout_override: Optional[int] = None,
        cached_urls_override: Optional[List[URL]] = None,
        skip_cache_refresh: bool = False,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> ScrapeResult:
        """Public asynchronous entry point for the scraping workflow."""

        (
            resolved_max_products,
            resolved_stock_monitor,
            resolved_timeout,
            resolved_cached_urls,
            resolved_skip_cache_refresh,
        ) = self._resolve_scrape_parameters(
            max_products,
            enable_stock_monitor,
            timeout_override,
            cached_urls_override,
            skip_cache_refresh,
        )

        self._validate_base_url(base_url)

        self._notification_email = email
        self._current_base_url = base_url
        self._timeout_override = resolved_timeout
        self._cached_urls_override = resolved_cached_urls
        self._skip_cache_refresh = resolved_skip_cache_refresh
        self._apply_runtime_overrides()

        previous_callback = getattr(self, "_progress_callback", None)
        self._progress_callback = progress_callback
        try:
            return await self._run_scrape_async(
                base_url=base_url,
                max_products=resolved_max_products,
                output_format=output_format,
                enable_stock_monitor=resolved_stock_monitor,
            )
        finally:
            self._progress_callback = previous_callback

    def _validate_base_url(self, base_url: URL) -> None:
        parsed = urlparse(str(base_url))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"Invalid base URL provided: {base_url}")

    def _resolve_scrape_parameters(
        self,
        max_products: Optional[int],
        enable_stock_monitor: Optional[bool],
        timeout_override: Optional[int] = None,
        cached_urls_override: Optional[List[URL]] = None,
        skip_cache_refresh: bool = False,
    ) -> Tuple[int, bool, Optional[int], Optional[List[URL]], bool]:
        """Resolve runtime parameters using explicit values or configuration defaults."""

        resolved_max_products = max_products or self.config.get(
            "max_products_per_run", 50
        )
        stock_config = self.config.get("stock_monitoring", {})
        resolved_stock_monitor = (
            enable_stock_monitor
            if enable_stock_monitor is not None
            else bool(stock_config.get("enabled", False))
        )

        resolved_timeout: Optional[int] = None
        if timeout_override is not None:
            try:
                candidate = int(timeout_override)
                if candidate > 0:
                    resolved_timeout = candidate
            except (TypeError, ValueError):
                resolved_timeout = None

        resolved_cached_urls: Optional[List[URL]] = None
        if cached_urls_override:
            filtered_urls = [url for url in cached_urls_override if url]
            if filtered_urls:
                resolved_cached_urls = list(dict.fromkeys(filtered_urls))

        resolved_skip_cache_refresh = bool(skip_cache_refresh)

        return (
            resolved_max_products,
            resolved_stock_monitor,
            resolved_timeout,
            resolved_cached_urls,
            resolved_skip_cache_refresh,
        )

    def _apply_runtime_overrides(self) -> None:
        """Apply runtime overrides to in-memory configuration and helpers."""
        timeout_seconds = (
            self._timeout_override
            if self._timeout_override is not None and self._timeout_override > 0
            else None
        )

        if timeout_seconds is not None:
            self._update_timeout_config(timeout_seconds)
            self._apply_timeout_to_antibot(timeout_seconds)
            self._apply_timeout_to_playwright(timeout_seconds)
            self.logger.debug("Applied timeout override: %ss", timeout_seconds)

    def _update_timeout_config(self, timeout_seconds: int) -> None:
        httpx_cfg = self.config.setdefault("httpx_scraper", {})
        timeout_cfg = httpx_cfg.setdefault("timeout", {})
        for key in ("connect", "read", "write", "pool"):
            timeout_cfg[key] = float(timeout_seconds)

        playwright_cfg = self.config.setdefault("playwright", {})
        perf_cfg = playwright_cfg.setdefault("performance_settings", {})
        timeout_ms = max(int(timeout_seconds * 1000), 0)
        perf_cfg["navigation_timeout_ms"] = timeout_ms
        perf_cfg["wait_for_selector_timeout_ms"] = timeout_ms

    def _apply_timeout_to_antibot(self, timeout_seconds: int) -> None:
        if hasattr(self.antibot, "config") and isinstance(
            getattr(self.antibot, "config", None), dict
        ):
            self.antibot.config["timeout"] = timeout_seconds
        elif hasattr(self.antibot, "timeout"):
            setattr(self.antibot, "timeout", timeout_seconds)

    def _apply_timeout_to_playwright(self, timeout_seconds: int) -> None:
        if not self.playwright_manager:
            return
        timeout_ms = max(int(timeout_seconds * 1000), 0)
        self.playwright_manager.navigation_timeout_ms = timeout_ms
        self.playwright_manager.wait_for_selector_timeout_ms = timeout_ms

    async def _maybe_refresh_url_cache(self, base_url: URL) -> None:
        try:
            from utils.url_cache_builder import refresh_cached_urls
        except ImportError:
            self.logger.debug("URL cache builder not available; skipping refresh")
            return

        scraping_config = self.config.get("scraping", {})
        if not scraping_config:
            return

        try:
            await asyncio.to_thread(
                refresh_cached_urls,
                scraping_config,
                base_url,
                firecrawl_client=self.firecrawl_client,
            )
        except Exception as exc:
            self.logger.debug("URL cache refresh failed: %s", exc)

    def _apply_timeout_override_httpx(self, scraper: ModernHttpxScraper) -> None:
        if not hasattr(scraper, "httpx_config"):
            return
        if self._timeout_override is None or self._timeout_override <= 0:
            return

        httpx_cfg = getattr(scraper, "httpx_config", None)
        if not isinstance(httpx_cfg, dict):
            return

        timeout_cfg = httpx_cfg.setdefault("timeout", {})
        override = float(self._timeout_override)
        for key in ("connect", "read", "write", "pool"):
            timeout_cfg[key] = override

        scraper.httpx_config = httpx_cfg

    async def _run_scrape_async(
        self,
        base_url: URL,
        max_products: int = 50,
        output_format: str = "json",
        enable_stock_monitor: bool = False,
    ) -> ScrapeResult:
        """
        Execute main scraping workflow.

        Args:
            base_url: Base URL to start scraping from
            max_products: Maximum number of products to scrape
            output_format: Output format (json, csv, etc.)
            enable_stock_monitor: Enable stock monitoring after scraping

        Returns:
            ScrapeResult with scraping statistics and results
        """
        start_time = time.time()
        result: ScrapeResult = {
            "success": False,
            "method_used": ScrapingMethod.AUTO,
            "response_time": 0.0,
            "status_code": None,
            "error_message": None,
            "products_found": 0,
            "variations_found": 0,
            "timestamp": start_time,
        }

        self._last_scrape_metadata = {}

        try:
            # Initialize scraping session
            await self._initialize_scraping_session(base_url)

            # Discover and validate URLs
            product_urls = await self._discover_and_validate_urls(
                base_url,
                max_products,
                cached_urls_override=self._cached_urls_override,
                skip_cache_refresh=self._skip_cache_refresh,
            )

            if not product_urls:
                return await self._handle_url_discovery_failure(result)

            # Execute scraping
            scraped_data = await self._execute_scraping(product_urls)

            if len(scraped_data) > max_products:
                scraped_data = scraped_data[:max_products]

            # Create final result
            result = await self._create_final_result(result, scraped_data, start_time)

            # Execute post-scraping tasks
            await self._execute_post_scraping_tasks(enable_stock_monitor, scraped_data)

            return result

        except Exception as e:
            self.logger.error(f"Scraping failed: {e}")
            result["error_message"] = str(e)
            return result
        finally:
            await self._cleanup_scraping_session()

    async def _initialize_scraping_session(self, base_url: URL) -> None:
        """Initialize scraping session and components."""
        self.logger.info(f"Initializing scraping session for: {base_url}")
        self._current_base_url = base_url

        if hasattr(self.analyzer, "set_base_url"):
            try:
                self.analyzer.set_base_url(base_url)
            except Exception as exc:
                self.logger.debug("Failed to set analyzer base URL: %s", exc)

        if hasattr(self.parser, "sitemap_analyzer"):
            try:
                self.parser.sitemap_analyzer = self.analyzer
            except Exception:
                pass

        # Initialize antibot system
        if hasattr(self.antibot, "initialize"):
            await self._maybe_call_method(self.antibot, "initialize")

        # Initialize fast scraper if needed
        if self.fast_scraper and hasattr(self.fast_scraper, "initialize"):
            await self._maybe_call_method(self.fast_scraper, "initialize")

        # Initialize hybrid engine if needed
        if self.hybrid_engine and hasattr(self.hybrid_engine, "initialize"):
            await self._maybe_call_method(self.hybrid_engine, "initialize")

    async def _discover_and_validate_urls(
        self,
        base_url: URL,
        max_products: int,
        cached_urls_override: Optional[List[URL]] = None,
        skip_cache_refresh: bool = False,
    ) -> List[URL]:
        """Discover and validate product URLs."""
        self.logger.info(f"Discovering product URLs from: {base_url}")

        discovery_total_hint = max(max_products or 0, 1)
        self._emit_progress(
            PHASE_DISCOVERY,
            0,
            discovery_total_hint,
            "Starting URL discovery",
        )

        if cached_urls_override:
            filtered_urls = [url for url in cached_urls_override if url]
            if filtered_urls:
                deduped = list(dict.fromkeys(filtered_urls))
                limited = deduped[:max_products] if max_products else deduped
                self.logger.debug(
                    "Using cached URL override with %d entries (limited to %d)",
                    len(deduped),
                    len(limited),
                )
                self._emit_progress(
                    PHASE_DISCOVERY,
                    len(limited),
                    max(len(limited), 1),
                    f"Using cached URLs ({len(limited)})",
                )
                return limited

        if skip_cache_refresh:
            self.logger.debug("Skipping URL cache refresh per override")
        else:
            await self._maybe_refresh_url_cache(base_url)

        expanded_target = max(max_products * 3, max_products)
        discovered: List[URL] = []

        try:
            sitemap_urls: List[URL] = []
            sitemap_hint: Optional[str] = None
            sitemap_page = None
            if hasattr(self.analyzer, "find_sitemap_url"):
                try:
                    sitemap_hint = self.analyzer.find_sitemap_url(self.antibot)
                except TypeError:
                    sitemap_hint = self.analyzer.find_sitemap_url()
                except Exception as exc:
                    self.logger.debug("Sitemap URL lookup failed: %s", exc)

                if sitemap_hint and hasattr(self.antibot, "get_page"):
                    try:
                        sitemap_page = self.antibot.get_page()
                    except Exception as exc:
                        self.logger.debug("Failed to acquire page for sitemap parse: %s", exc)

            if sitemap_hint and hasattr(self.analyzer, "parse_sitemap"):
                try:
                    sitemap_urls = self.analyzer.parse_sitemap(sitemap_hint)
                except TypeError:
                    if sitemap_page is None and hasattr(self.antibot, "get_page"):
                        try:
                            sitemap_page = self.antibot.get_page()
                        except Exception as exc:
                            self.logger.debug(
                                "Failed to acquire page for sitemap parse: %s", exc
                            )
                    try:
                        sitemap_urls = self.analyzer.parse_sitemap(
                            sitemap_hint, sitemap_page
                        )
                    except Exception as nested_exc:
                        self.logger.debug(
                            "Sitemap parsing via hint failed: %s", nested_exc
                        )
                except Exception as exc:
                    self.logger.debug("Sitemap parsing via hint failed: %s", exc)

            if not sitemap_urls and hasattr(
                self.analyzer, "get_product_urls_from_sitemap"
            ):
                try:
                    sitemap_urls = self.analyzer.get_product_urls_from_sitemap(
                        base_url=base_url, max_products=expanded_target
                    )
                    if sitemap_hint:
                        self.logger.debug(
                            "Analyzer suggested sitemap URL: %s", sitemap_hint
                        )
                except Exception as exc:
                    self.logger.debug("Sitemap discovery failed: %s", exc)

            if sitemap_urls:
                self.logger.info(f"Found {len(sitemap_urls)} URLs from sitemap")
                discovered.extend(sitemap_urls[:expanded_target])
            else:
                http_urls = await self._httpx_discover_product_urls(
                    base_url, expanded_target
                )
                if http_urls:
                    discovered.extend(http_urls[:expanded_target])

            if not discovered:
                discovered.extend(
                    await self._get_product_urls(base_url, expanded_target)
                )

        except Exception as e:
            self.logger.error(f"URL discovery failed: {e}")

        if not discovered:
            self.logger.warning(
                "Falling back to scraping the provided URL directly; no product URLs discovered"
            )
            discovered = [base_url]

        final_urls = discovered[:expanded_target]
        self._emit_progress(
            PHASE_DISCOVERY,
            len(final_urls),
            max(len(final_urls), 1),
            f"Discovered {len(final_urls)} URL(s)",
        )
        return final_urls

    async def _handle_url_discovery_failure(self, result: ScrapeResult) -> ScrapeResult:
        """Handle the case when no URLs are discovered."""
        result["error_message"] = "No product URLs discovered"
        self.logger.warning(
            "No product URLs discovered. Check site structure or selectors."
        )

        # Try to provide helpful information
        if self.analyzer.last_error:
            result["error_message"] += f" Analyzer error: {self.analyzer.last_error}"

        return result

    async def _execute_scraping(self, urls: List[URL]) -> List[ProductData]:
        """Execute the main scraping process."""
        scraped_data: List[ProductData] = []

        # Choose scraping method
        method = self._select_optimal_method()
        actual_method = method

        self._current_scrape_total = len(urls)
        total_urls = max(self._current_scrape_total, 1)
        self._emit_progress(
            PHASE_SCRAPING,
            0,
            total_urls,
            "Starting product scraping",
        )

        try:
            if method == ScrapingMethod.HTTPX:
                scraped_data = await self._try_httpx_scraping(urls)
                if scraped_data:
                    actual_method = ScrapingMethod.HTTPX
                else:
                    fallback = await self._try_playwright_scraping(urls)
                    if fallback:
                        scraped_data = fallback
                        actual_method = ScrapingMethod.PLAYWRIGHT
            elif method == ScrapingMethod.PLAYWRIGHT:
                scraped_data = await self._try_playwright_scraping(urls)
                if scraped_data:
                    actual_method = ScrapingMethod.PLAYWRIGHT
                else:
                    fallback = await self._try_httpx_scraping(urls)
                    if fallback:
                        scraped_data = fallback
                        actual_method = ScrapingMethod.HTTPX
            else:  # HYBRID or AUTO
                scraped_data = await self._try_httpx_scraping(urls)
                if scraped_data:
                    actual_method = ScrapingMethod.HTTPX
                else:
                    scraped_data = await self._try_playwright_scraping(urls)
                    if scraped_data:
                        actual_method = ScrapingMethod.PLAYWRIGHT

        except Exception as e:
            self.logger.error(f"Scraping execution failed: {e}")

        self._last_used_method = actual_method

        if not scraped_data and hasattr(self.parser, "parse_product"):
            fallback_products: List[ProductData] = []
            loop = asyncio.get_running_loop()
            for url in urls:
                try:
                    try:
                        parsed = await loop.run_in_executor(
                            None,
                            lambda url=url: self.antibot.run_playwright_task(
                                lambda: self.parser.parse_product(url)
                            ),
                        )
                    except TypeError:
                        parsed = await loop.run_in_executor(
                            None,
                            lambda url=url: self.antibot.run_playwright_task(
                                lambda: self.parser.parse_product("", url)
                            ),
                        )
                    if parsed:
                        fallback_products.append(parsed)
                except Exception as exc:
                    self.logger.debug("Parser fallback failed for %s: %s", url, exc)
            if fallback_products:
                scraped_data = fallback_products

        return scraped_data

    async def _try_httpx_scraping(self, urls: List[URL]) -> List[ProductData]:
        """Try HTTPX-based scraping first."""
        products: List[ProductData] = []

        try:
            from network.httpx_scraper import ModernHttpxScraper

            base_url = self._current_base_url or (urls[0] if urls else "")
            scraper = ModernHttpxScraper(
                self.config_path, firecrawl_client=self.firecrawl_client
            )
            self._apply_timeout_override_httpx(scraper)

            total_urls = max(len(urls), 1)
            progress_state = {"processed": 0}

            def _httpx_progress(event: str, payload: Dict[str, object]) -> None:
                current_processed = progress_state["processed"]
                message_parts: List[str] = []
                url = payload.get("url") if isinstance(payload, dict) else None

                if event in {"parse_success", "parse_failed", "parse_exception"}:
                    progress_state["processed"] = min(current_processed + 1, total_urls)
                    current_processed = progress_state["processed"]
                    verb = {
                        "parse_success": "parsed",
                        "parse_failed": "parse failed",
                        "parse_exception": "parse error",
                    }.get(event, event)
                    if url:
                        message_parts.append(f"{verb}: {url}")
                    else:
                        message_parts.append(verb)
                    if event == "parse_success":
                        variations = payload.get("variations") if isinstance(payload, dict) else None
                        if isinstance(variations, int):
                            message_parts.append(f"variations={variations}")
                elif event == "parse_start":
                    if url:
                        message_parts.append(f"parse_start: {url}")
                    else:
                        message_parts.append("parse_start")
                elif event in {"request_start", "request_success", "request_timeout", "request_failed", "request_error"}:
                    if url:
                        message_parts.append(f"{event}: {url}")
                    else:
                        message_parts.append(event)
                    attempt = payload.get("attempt") if isinstance(payload, dict) else None
                    total_attempts = payload.get("total_attempts") if isinstance(payload, dict) else None
                    if attempt and total_attempts:
                        message_parts.append(f"attempt {attempt}/{total_attempts}")
                    if event == "request_success":
                        rt = payload.get("response_time") if isinstance(payload, dict) else None
                        if isinstance(rt, (int, float)):
                            message_parts.append(f"{rt:.2f}s")
                elif event == "batch_complete":
                    success = payload.get("success") if isinstance(payload, dict) else None
                    failures = payload.get("failures") if isinstance(payload, dict) else None
                    message_parts.append(f"batch complete: success={success} failures={failures}")
                else:
                    message_parts.append(event)

                message = " | ".join(message_parts)
                self._emit_progress(
                    PHASE_SCRAPING,
                    progress_state.get("processed", current_processed),
                    max(self._current_scrape_total or total_urls, 1),
                    message,
                )

            async with scraper as active_scraper:
                httpx_result = await active_scraper.scrape_products(
                    base_url=base_url,
                    product_urls=urls,
                    email=self._notification_email or "",
                    progress_hook=_httpx_progress,
                )

            products = httpx_result.get("products", [])
            self._last_scrape_metadata = httpx_result

            if products:
                self.method_performance[ScrapingMethod.HTTPX.value] = {
                    "avg_response_time": httpx_result.get("avg_response_time", 0.0),
                    "success_rate": httpx_result.get("success_rate", 0.0),
                }
                self._emit_progress(
                    PHASE_SCRAPING,
                    len(products),
                    max(self._current_scrape_total or len(products), 1),
                    "HTTPX scraping completed",
                )
                return products

        except ImportError:
            self.logger.warning("ModernHttpxScraper not available")
        except Exception as e:
            self.logger.warning(f"HTTPX scraping failed: {e}")

        if self.fast_scraper:
            try:
                fast_result = await self._maybe_await(
                    self.fast_scraper.scrape_products(
                        base_url=self._current_base_url or (urls[0] if urls else ""),
                        product_urls=urls,
                        email=self._notification_email or "",
                    )
                )
                self._last_scrape_metadata = fast_result
                products = fast_result.get("products", [])
                if products:
                    self._emit_progress(
                        PHASE_SCRAPING,
                        len(products),
                        max(self._current_scrape_total or len(products), 1),
                        "Fast HTTP scraping completed",
                    )
                return products
            except Exception as e:
                self.logger.warning(f"Legacy fast scraper failed: {e}")

        return products

    async def _try_playwright_scraping(self, urls: List[URL]) -> List[ProductData]:
        """Fallback to Playwright scraping."""
        if not self.playwright_manager:
            return []

        try:
            return await self._scrape_with_playwright(urls)
        except Exception as e:
            self.logger.warning(f"Playwright scraping failed: {e}")
            return []

    async def _create_final_result(
        self, result: ScrapeResult, scraped_data: List[ProductData], start_time: float
    ) -> ScrapeResult:
        """Create final scraping result."""
        result["success"] = len(scraped_data) > 0
        result["products_found"] = len(scraped_data)
        result["response_time"] = time.time() - start_time
        result["method_used"] = self._last_used_method

        # Count variations
        total_variations = 0
        for product in scraped_data:
            if "variations" in product:
                total_variations += len(product.get("variations", []))

        result["variations_found"] = total_variations
        result["variations"] = total_variations
        result["scraped_products"] = result["products_found"]

        metadata = self._last_scrape_metadata or {}

        if not scraped_data and metadata.get("products"):
            scraped_data = metadata.get("products", [])
            result["products_found"] = len(scraped_data)
            result["scraped_products"] = len(scraped_data)
            total_variations = 0
            for product in scraped_data:
                total_variations += len(product.get("variations", []))
            result["variations_found"] = total_variations
            result["variations"] = total_variations

        result["products"] = scraped_data
        if scraped_data and hasattr(self.db, "insert_product"):
            persisted_ids: List[Any] = []
            for product in scraped_data:
                try:
                    product_id = self.db.insert_product(product)
                    variations = (
                        product.get("variations", [])
                        if isinstance(product, dict)
                        else []
                    )
                    if variations and hasattr(self.db, "insert_variations"):
                        self.db.insert_variations(product_id, variations)
                    persisted_ids.append(product_id)
                except Exception as exc:
                    self.logger.debug("Failed to persist product: %s", exc)
            if persisted_ids:
                result["persisted_ids"] = persisted_ids

        result["total_urls_found"] = metadata.get(
            "total_urls",
            metadata.get("total_urls_found", len(scraped_data)),
        )

        if "failures" in metadata:
            result["failures"] = metadata["failures"]

        if metadata.get("avg_response_time") is not None:
            result["avg_response_time"] = metadata["avg_response_time"]

        if metadata.get("success_rate") is not None:
            result["success_rate"] = metadata["success_rate"]

        export_path = metadata.get("export_path")
        export_path_excel = metadata.get("export_path_excel")
        if export_path:
            result["export_path"] = export_path
            try:
                json_file, excel_file = write_product_exports(
                    scraped_data, Path(export_path)
                )
                result["export_path"] = str(json_file)
                if excel_file:
                    result["export_path_excel"] = str(excel_file)
            except Exception as exc:
                self.logger.debug(
                    "Failed to rewrite export artefacts at %s: %s", export_path, exc
                )
                if export_path_excel:
                    result["export_path_excel"] = export_path_excel
        elif export_path_excel:
            result["export_path_excel"] = export_path_excel

        self.logger.info(
            f"Scraping completed: {result['products_found']} products, "
            f"{result['variations_found']} variations in "
            f"{result['response_time']:.2f}s"
        )

        self._emit_progress(
            PHASE_COMPLETE,
            result["products_found"],
            max(result["products_found"], 1),
            "Scraping completed",
        )

        try:
            append_history_records(scraped_data, datetime.now(UTC))
            if self._current_base_url:
                site_domain = (
                    urljoin(self._current_base_url, "/")
                    .split("//", 1)[-1]
                    .split("/", 1)[0]
                )
                export_site_history_to_csv(site_domain)
                export_site_history_to_json(site_domain)
        except Exception as exc:
            self.logger.debug("Failed to append history records: %s", exc)

        return result

    async def _execute_post_scraping_tasks(
        self, enable_stock_monitor: bool, scraped_data: List[ProductData]
    ) -> None:
        """Execute post-scraping tasks like monitoring."""
        if enable_stock_monitor and self.stock_monitor and scraped_data:
            try:
                await self._run_stock_monitoring_task(scraped_data)
            except Exception as e:
                self.logger.error(f"Stock monitoring failed: {e}")

    async def _run_stock_monitoring_task(self, scraped_data: List[ProductData]) -> None:
        """Run stock monitoring on scraped data."""
        self.logger.info("Starting stock monitoring analysis...")

        try:
            # Process each product for stock monitoring
            for product in scraped_data:
                if self.stock_monitor:
                    await self._maybe_call_method(
                        self.stock_monitor, "analyze_product", product
                    )

            # Generate stock reports if configured
            if hasattr(self.stock_monitor, "generate_report"):
                await self._maybe_call_method(self.stock_monitor, "generate_report")

        except Exception as e:
            self.logger.error(f"Stock monitoring task failed: {e}")

    async def _cleanup_scraping_session(self) -> None:
        """Cleanup scraping session and resources."""
        try:
            # Cleanup playwright resources
            if self.playwright_manager and hasattr(self.playwright_manager, "cleanup"):
                await self._maybe_call_method(self.playwright_manager, "cleanup")

            # Cleanup fast scraper
            if self.fast_scraper and hasattr(self.fast_scraper, "cleanup"):
                await self._maybe_call_method(self.fast_scraper, "cleanup")

            # Cleanup hybrid engine
            if self.hybrid_engine and hasattr(self.hybrid_engine, "cleanup"):
                await self._maybe_call_method(self.hybrid_engine, "cleanup")

        except Exception as e:
            self.logger.error(f"Cleanup failed: {e}")

    async def run_stock_monitoring(
        self, product_ids: Optional[List[ProductID]] = None
    ) -> Dict[str, Any]:
        """
        Run stock monitoring for specified products or all products.

        Args:
            product_ids: Optional list of product IDs to monitor

        Returns:
            Dictionary with monitoring results
        """
        if not self.stock_monitor:
            return {"error": "Stock monitor not initialized"}

        try:
            results = {}

            if product_ids:
                # Monitor specific products
                for product_id in product_ids:
                    result = await self._maybe_call_method(
                        self.stock_monitor, "check_product_stock", product_id
                    )
                    results[str(product_id)] = result
            else:
                # Monitor all products
                results = await self._maybe_call_method(
                    self.stock_monitor, "monitor_all_products"
                ) or {}

            return {"success": True, "results": results, "timestamp": time.time()}

        except Exception as e:
            self.logger.error(f"Stock monitoring failed: {e}")
            return {"success": False, "error": str(e), "timestamp": time.time()}

    def _select_optimal_method(self) -> ScrapingMethod:
        """Select optimal scraping method based on configuration and performance."""
        # Check for method override
        override = self._get_method_override()
        if override:
            return override

        # Use intelligent selection if enabled
        if self.intelligent_selection and self.method_performance:
            # Select method with best performance
            best_method = min(
                self.method_performance.items(),
                key=lambda x: x[1].get("avg_response_time", float("inf")),
            )[0]
            return ScrapingMethod(best_method)

        return self.scraper_backend

    def _get_method_override(self) -> Optional[ScrapingMethod]:
        """Get method override from configuration or environment."""
        scraping_config = self.config.get("scraping", {})
        method_override = scraping_config.get("method_override")

        if method_override and method_override in [m.value for m in ScrapingMethod]:
            return ScrapingMethod(method_override)

        return None

    async def _scrape_with_httpx(self, urls: List[URL]) -> List[ProductData]:
        """Scrape URLs using HTTPX."""
        if not self.fast_scraper:
            return []

        try:
            return await self._maybe_await(self.fast_scraper.scrape_multiple(urls)) or []
        except Exception as e:
            self.logger.error(f"HTTPX scraping failed: {e}")
            return []

    async def _scrape_with_playwright(self, urls: List[URL]) -> List[ProductData]:
        """Scrape URLs using Playwright."""
        if not self.playwright_manager:
            return []

        scraped_products: List[ProductData] = []
        total_urls = max(len(urls), 1)
        processed = 0

        try:
            # Initialize browser
            browser = await self._maybe_await(
                self.playwright_manager.get_browser()
            )
            if not browser:
                return []

            # Process each URL
            for url in urls:
                page = None
                try:
                    page = await self._maybe_await(browser.new_page()) if hasattr(browser, "new_page") else None

                    if not page:
                        continue

                    # Apply antibot headers
                    headers = await self._maybe_await(self.antibot.get_headers())
                    if page and hasattr(page, "set_extra_http_headers"):
                        await self._maybe_await(page.set_extra_http_headers(headers))

                    if self._timeout_override and self._timeout_override > 0:
                        timeout_ms = int(self._timeout_override * 1000)
                        page.set_default_navigation_timeout(timeout_ms)
                        page.set_default_timeout(timeout_ms)

                    # Navigate to page
                    response = await self._maybe_await(
                        page.goto(url, wait_until="networkidle")
                    )

                    if response and response.status == 200:
                        # Get page content
                        content = await self._maybe_await(page.content())

                        # Parse product data
                        product_data = self.parser.parse_product(content, url)

                        if product_data:
                            scraped_products.append(product_data)

                except Exception as e:
                    self.logger.error(f"Failed to scrape {url}: {e}")
                finally:
                    processed += 1
                    self._emit_progress(
                        PHASE_SCRAPING,
                        processed,
                        total_urls,
                        f"Processed {processed}/{total_urls} via Playwright",
                    )
                    if page is not None:
                        with contextlib.suppress(Exception):
                            await page.close()

            return scraped_products

        except Exception as e:
            self.logger.error(f"Playwright scraping failed: {e}")
            return []

    async def _get_product_urls(self, base_url: URL, max_products: int) -> List[URL]:
        """Get product URLs through HTTP discovery."""
        try:
            if self.config.get("scraping", {}).get("discovery_method") == "http":
                return await self._discover_products_via_http(base_url, max_products)
            else:
                # Use sitemap discovery
                if hasattr(self.analyzer, "get_product_urls_from_sitemap"):
                    return self.analyzer.get_product_urls_from_sitemap(
                        base_url=base_url, max_products=max_products
                    )
                return []
        except Exception as e:
            self.logger.error(f"URL discovery failed: {e}")
            return []

    async def _discover_products_via_http(
        self, base_url: URL, max_products: int
    ) -> List[URL]:
        """Discover products via HTTP crawling."""
        # Validate requirements
        if not self._validate_http_discovery_requirements():
            return []

        # Initialize discovery state
        discovered_urls, queue, visited = self._init_http_discovery_state(base_url)

        # Process URLs until we have enough products or queue is empty
        while len(discovered_urls) < max_products and queue:
            await self._process_next_url_in_queue(
                queue, visited, discovered_urls, max_products
            )

        return discovered_urls[:max_products]

    async def _httpx_discover_product_urls(
        self, base_url: URL, max_products: int
    ) -> List[URL]:
        try:
            scraper = ModernHttpxScraper(
                self.config_path, firecrawl_client=self.firecrawl_client
            )
            self._apply_timeout_override_httpx(scraper)
            async with scraper as active_scraper:
                return await active_scraper._discover_product_urls(base_url, max_products)
        except Exception as exc:
            self.logger.debug("HTTPX discovery helper failed: %s", exc)
            return []

    def _validate_http_discovery_requirements(self) -> bool:
        """Validate requirements for HTTP discovery."""
        if not self.fast_scraper:
            self.logger.error("Fast scraper not available for HTTP discovery")
            return False

        if not hasattr(self.parser, "extract_product_links"):
            self.logger.error("Parser doesn't support link extraction")
            return False

        return True

    def _init_http_discovery_state(
        self, base_url: URL
    ) -> Tuple[List[URL], List[URL], set]:
        """Initialize HTTP discovery state."""
        discovered_urls: List[URL] = []
        queue: List[URL] = [base_url]
        visited: set = set()

        # Add category URLs if configured
        category_urls = self.config.get("scraping", {}).get("category_urls", [])
        queue.extend(category_urls)

        return discovered_urls, queue, visited

    async def _process_next_url_in_queue(
        self,
        queue: List[URL],
        visited: set,
        discovered_urls: List[URL],
        max_products: int,
    ) -> None:
        """Process next URL in the discovery queue."""
        if not queue:
            return

        current_url = queue.pop(0)

        if current_url in visited:
            return

        visited.add(current_url)

        # Fetch page content
        content = await self._fetch_page_content(current_url)
        if not content:
            return

        # Extract and process links
        await self._extract_and_process_links(
            content, current_url, queue, discovered_urls, max_products
        )

    async def _fetch_page_content(self, url: URL) -> Optional[str]:
        """Fetch page content for link extraction."""
        try:
            if self.fast_scraper and hasattr(self.fast_scraper, "fetch_content"):
                return await self._maybe_await(self.fast_scraper.fetch_content(url))
            return None
        except Exception as e:
            self.logger.warning(f"Failed to fetch content from {url}: {e}")
            return None

    async def _extract_and_process_links(
        self,
        content: str,
        current_url: URL,
        queue: List[URL],
        discovered_urls: List[URL],
        max_products: int,
    ) -> None:
        """Extract and process links from page content."""
        if len(discovered_urls) >= max_products:
            return

        try:
            # Extract links using parser
            links = self.parser.extract_product_links(content, current_url)

            for link in links:
                full_url = urljoin(current_url, link)

                if self._is_product_url(full_url):
                    self._add_product_url(full_url, discovered_urls)
                elif self._should_queue_category_url(full_url, queue):
                    queue.append(full_url)

        except Exception as e:
            self.logger.warning(f"Link extraction failed for {current_url}: {e}")

    def _is_product_url(self, url: URL) -> bool:
        """Check if URL is a product URL."""
        return (
            self.parser.is_product_url(url)
            if hasattr(self.parser, "is_product_url")
            else False
        )

    def _add_product_url(self, url: URL, discovered_urls: List[URL]) -> None:
        """Add product URL to discovered list."""
        if url not in discovered_urls:
            discovered_urls.append(url)
            self.logger.debug(f"Added product URL: {url}")

    def _should_queue_category_url(self, url: URL, queue: List[URL]) -> bool:
        """Check if category URL should be queued for further processing."""
        # Avoid infinite loops
        if len(queue) > 100:
            return False

        # Check if it's a category URL
        if hasattr(self.parser, "is_category_url"):
            return self.parser.is_category_url(url) and url not in queue

        return False

    def _print_enhanced_output_with_monitoring(
        self, grouped_products: Dict[str, Any]
    ) -> None:
        """Print enhanced output with monitoring information."""
        # This method would contain the implementation for enhanced output
        # For now, keeping it as a placeholder to maintain the original structure
        pass

    def _print_hierarchical_display_from_grouped(
        self, grouped_products: Dict[str, Any]
    ) -> None:
        """Print hierarchical display from grouped products."""
        # This method would contain the implementation for hierarchical display
        # For now, keeping it as a placeholder to maintain the original structure
        pass
