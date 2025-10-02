from typing import (
    Dict,
    Optional,
    List,
    Any,
    TypedDict,
    Union,
    Tuple,
    TYPE_CHECKING,
    Literal,
    overload,
)
import logging
import inspect
import time
import random
import json
import warnings
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlparse
from core.antibot_manager import AntibotManager
from core.sitemap_analyzer import SitemapAnalyzer
from core.adaptive_selector_learner import AdaptiveSelectorLearner
from core.selector_memory import SelectorMemory
from utils.helpers import (
    clean_price,
    parse_stock,
    extract_with_bs4,
    sanitize_text,
    safe_float_conversion,
    safe_int_conversion,
)
from parsers.variation_parser import VariationParser
from bs4 import BeautifulSoup, Tag
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError
from playwright.async_api import Page as AsyncPage
from utils.error_handling import StructuredLogger, retry_manager, ErrorContext
from utils.cms_detection import CMSDetection, CMSConfig, CMSDetectionResult
from core.async_playwright_manager import AsyncPlaywrightManager
from network.firecrawl_client import FirecrawlClient

if TYPE_CHECKING:  # pragma: no cover
    from playwright.async_api import BrowserContext


# Custom Exceptions
class ParsingError(Exception):
    """Base exception for parsing errors."""

    pass


class ExtractionError(ParsingError):
    """Exception raised when data extraction fails."""

    pass


class ValidationError(ParsingError):
    """Exception raised when data validation fails."""

    pass


# TypedDict Classes
class VariationData(TypedDict):
    type: str
    value: str
    price: Optional[float]
    stock: Optional[int]
    in_stock: Optional[bool]
    variant_id: Optional[str]
    sku: Optional[str]
    url: Optional[str]
    attributes: Dict[str, str]


class ProductData(TypedDict):
    url: str
    name: Optional[str]
    price: Optional[float]
    base_price: Optional[float]
    stock: Optional[int]
    in_stock: bool
    stock_quantity: Optional[int]
    variations: List[VariationData]
    error: Optional[str]


class ParserConfig(TypedDict):
    timeout: int
    retry_attempts: int
    cache_enabled: bool
    debug_logging: bool
    graceful_degradation: bool


@dataclass
class ProductParserConfig:
    """Configuration for ProductParser with validation."""

    timeout: int = 10000
    retry_attempts: int = 3
    cache_enabled: bool = True
    debug_logging: bool = False
    graceful_degradation: bool = True
    return_none_on_missing: bool = False

    def __post_init__(self):
        if self.timeout <= 0:
            raise ValidationError("Timeout must be positive")
        if self.retry_attempts < 0:
            raise ValidationError("Retry attempts must be non-negative")


class ProductParser:
    """
    Enhanced ProductParser with comprehensive type safety, error handling, and performance optimizations.

    Features:
    - Complete type hints for all methods and parameters
    - Structured data types using TypedDict
    - Comprehensive error handling with specific exceptions
    - Graceful degradation from Playwright to BeautifulSoup
    - Performance optimizations with caching and timeouts
    - Lazy loading for variation parser
    - Debug logging and timing information
    - Backward compatibility maintained
    """

    def __init__(
        self,
        antibot_manager: AntibotManager,
        sitemap_analyzer: SitemapAnalyzer,
        page: Optional[Page] = None,
        html: Optional[str] = None,
        config: Optional[ProductParserConfig] = None,
        adaptive_learner: Optional[AdaptiveSelectorLearner] = None,
        playwright_manager: Optional[AsyncPlaywrightManager] = None,
        selector_memory: Optional[SelectorMemory] = None,
        firecrawl_client: Optional[FirecrawlClient] = None,
    ) -> None:
        """
        Initialize ProductParser with enhanced validation and configuration.

        Args:
            antibot_manager: AntibotManager instance for human-like behavior
            sitemap_analyzer: SitemapAnalyzer instance for site analysis
            page: Optional Playwright page instance
            html: Optional HTML content for parsing
            config: Optional configuration object
            adaptive_learner: Optional AdaptiveSelectorLearner for intelligent selector learning

        Raises:
            ValidationError: If configuration is invalid
            ValueError: If neither page nor html is provided
        """
        self.antibot_manager = antibot_manager
        self.sitemap_analyzer = sitemap_analyzer
        self.page = page
        self.html = html
        self.config = config or ProductParserConfig()
        self.playwright_manager = playwright_manager
        self._pooled_context: Optional["BrowserContext"] = None
        self.parsing_metrics: Dict[str, Any] = {
            "page_reuses": 0,
            "navigation_times": [],
            "extraction_times": [],
            "memory_usage": [],
        }

        # Initialize loggers before loading selectors as the loader emits log messages
        self.logger = logging.getLogger(__name__)
        if self.config.debug_logging:
            self.logger.setLevel(logging.DEBUG)
        self.structured_logger = StructuredLogger(name="product_parser")

        # Load selectors with validation (also loads config)
        self.selectors = self._load_selectors()

        # Initialize selector memory for unified storage
        self.selector_memory = selector_memory or SelectorMemory()

        # Initialize adaptive learner with selector memory if not provided
        if adaptive_learner is None:
            try:
                # Use the config loaded in _load_selectors
                adaptive_config = self._config_data.get("adaptive_selectors", {})
                if adaptive_config.get("enabled", True):
                    self.adaptive_learner = AdaptiveSelectorLearner(
                        selector_memory=self.selector_memory, config=self._config_data
                    )
                else:
                    self.adaptive_learner = None
            except Exception as e:
                self.logger.warning(f"Failed to initialize adaptive learner: {e}")
                self.adaptive_learner = None
        else:
            self.adaptive_learner = adaptive_learner

        # Lazy initialization for variation parser
        self._variation_parser: Optional[VariationParser] = None

        # Caching for performance
        self._html_cache: Dict[str, str] = {}
        self._bs4_cache: Dict[str, BeautifulSoup] = {}

        # Current URL for adaptive learning
        self._current_url: Optional[str] = None

        # CMS selectors loaded during _load_selectors()
        # self._cms_selectors is set in _load_selectors()

        # Initialize CMS detector with config
        self.cms_detector = CMSDetection(self.cms_config)

        # Optional Firecrawl fallback client
        self.firecrawl_client = firecrawl_client
        self._firecrawl_stock_cache: Dict[str, Optional[int]] = {}

        # Validation
        self._validate_initialization()

    def _validate_initialization(self) -> None:
        """Validate initialization parameters."""
        if self.page is None and self.html is None and not self.playwright_manager:
            if not hasattr(self.antibot_manager, "get_page"):
                raise ValueError(
                    "Provide a page, html, or a Playwright manager for dynamic fetching"
                )

        if self.page is not None and self.html is not None:
            raise ValueError("Provide either page or html, not both")

    def _extract_domain(self, url: str) -> str:
        """Extract domain from URL for adaptive learning."""
        try:
            parsed = urlparse(url)
            return parsed.netloc.replace("www.", "")
        except Exception:
            return "unknown"

    def _load_selectors(self) -> Dict[str, List[str]]:
        """Load selectors from config with CMS-aware support and enhanced error handling."""
        try:
            config_path = "config/settings.json"
            with open(config_path, "r", encoding="utf-8") as f:
                self._config_data = json.load(f)
                config_data = self._config_data

            # Load OpenCart selectors (preserve existing priority)
            opencart_selectors = config_data.get(
                "opencart_selectors",
                {
                    "name": ["h1.title", ".product-title"],
                    "price": [".price-new", ".product-price"],
                    "stock": [".stock-status"],
                    "variations": [
                        '.options select[name*="option"]',
                        ".form-group select",
                    ],
                },
            )

            # Load CMS selector sets
            cms_selectors = config_data.get("cms_selectors", {})

            # Store CMS selectors for later use
            self._cms_selectors = cms_selectors

            # Load CMS detection config
            cms_config_data = config_data.get("cms_detection", {})
            self.cms_config = CMSConfig(
                enable_version_detection=cms_config_data.get("enabled", True),
                confidence_threshold=cms_config_data.get("confidence_threshold", 0.6),
                max_detection_time=cms_config_data.get("detection_cache_ttl", 30.0),
            )

            # Load fallback chain configuration
            fallback_chain = config_data.get("fallback_chain", {})
            steps = fallback_chain.get("steps", [])
            # Normalize steps to list of strings
            step_names = []
            for s in steps:
                if isinstance(s, dict):
                    name = s.get("name")
                    if name:
                        step_names.append(name)
                elif isinstance(s, str):
                    step_names.append(s)
            self.logger.debug(f"Normalizing fallback steps: {step_names}")
            # Map settings names to internal tokens
            mapping = {
                "primary_selectors": "config_selectors",
                "adaptive_selectors": "adaptive_selectors",
                "cms_selectors": "cms_selectors",
                "generic_selectors": "manual_detection",
                "fallback_selectors": "manual_detection",
            }
            normalized = []
            for name in step_names:
                if name in mapping:
                    normalized.append(mapping[name])
                    self.logger.debug(f"Mapped step '{name}' to '{mapping[name]}'")
                else:
                    self.logger.debug(f"Skipping unrecognized step name '{name}'")
            # Defensive fallback
            if not normalized:
                normalized = [
                    "config_selectors",
                    "adaptive_selectors",
                    "manual_detection",
                ]
            # Add cms_selectors if not present
            if "cms_selectors" not in normalized:
                normalized.append("cms_selectors")
            self._fallback_steps = normalized
            self._cms_confidence_threshold = cms_config_data.get(
                "confidence_threshold", 0.7
            )

            # Validate OpenCart selectors
            for key, selectors in opencart_selectors.items():
                if not isinstance(selectors, list):
                    self.logger.warning(
                        f"Invalid selector format for {key}, using defaults"
                    )
                    default_selectors = self._get_default_selectors(key)
                    opencart_selectors[key] = default_selectors.get(key, [])

            # Validate CMS selectors
            for cms_name, cms_cfg in cms_selectors.items():
                if not isinstance(cms_cfg, dict):
                    self.logger.warning(
                        f"Invalid CMS config for {cms_name}, expected dict"
                    )
                    continue
                selectors = cms_cfg.get("selectors")
                if not isinstance(selectors, dict):
                    self.logger.warning(
                        f"CMS {cms_name} missing or invalid 'selectors' dict"
                    )
                    continue
                fallback_selectors = cms_cfg.get("fallback_selectors")
                if not isinstance(fallback_selectors, dict):
                    self.logger.warning(
                        f"CMS {cms_name} missing or invalid 'fallback_selectors' dict"
                    )
                    continue
                # Then validate each field in selectors and fallback_selectors
                for field, sel_list in selectors.items():
                    if not isinstance(sel_list, list):
                        self.logger.warning(
                            f"Invalid selector list for {cms_name}.selectors.{field}"
                        )
                for field, sel_list in fallback_selectors.items():
                    if not isinstance(sel_list, list):
                        self.logger.warning(
                            f"Invalid selector list for {cms_name}.fallback_selectors.{field}"
                        )

            return opencart_selectors

        except FileNotFoundError:
            self.logger.warning("Settings file not found, using OpenCart defaults")
            self._config_data = {}
            self._cms_selectors = {}
            self._fallback_steps = [
                "config_selectors",
                "adaptive_selectors",
                "manual_detection",
            ]
            self._cms_confidence_threshold = 0.7
            self.cms_config = CMSConfig(
                enable_version_detection=True,
                confidence_threshold=0.6,
                max_detection_time=30.0,
            )
            return self._get_default_selectors()
        except json.JSONDecodeError as e:
            self.logger.error(f"Invalid JSON in config file: {e}")
            self._config_data = {}
            self._cms_selectors = {}
            self._fallback_steps = [
                "config_selectors",
                "adaptive_selectors",
                "manual_detection",
            ]
            self._cms_confidence_threshold = 0.7
            self.cms_config = CMSConfig(
                enable_version_detection=True,
                confidence_threshold=0.6,
                max_detection_time=30.0,
            )
            return self._get_default_selectors()

    def _get_default_selectors(
        self, field: Optional[str] = None
    ) -> Dict[str, List[str]]:
        """Get default selectors for fallback."""
        defaults = {
            "name": ["h1.title", ".product-title"],
            "price": [".price-new", ".price"],
            "stock": [".stock-status"],
            "variations": [".options select[name^='option']"],
        }
        if field:
            return {field: defaults.get(field, [])}
        return defaults

    def _get_cms_selectors(self, cms_type: str, field: str) -> List[str]:
        """Get CMS-specific selectors for a field."""
        if not hasattr(self, "_cms_selectors") or not self._cms_selectors:
            return []

        cms_cfg = self._cms_selectors.get(cms_type, {})
        primary = cms_cfg.get("selectors", {}).get(field, [])
        fallback = cms_cfg.get("fallback_selectors", {}).get(field, [])
        # Merge and deduplicate
        merged = list(dict.fromkeys(primary + fallback))
        return merged

    def _get_adaptive_selectors(
        self, domain: str, field: str, html: Optional[str] = None
    ) -> List[str]:
        """Get adaptive selectors from the learner."""
        if not self.adaptive_learner:
            return []

        return self.adaptive_learner.get_adaptive_selectors(
            domain, field, html, fallback_limit=5
        )

    def _merge_selector_sets(self, *selector_sets: List[List[str]]) -> List[str]:
        """Merge multiple selector sets, removing duplicates while preserving order."""
        merged = []
        seen = set()

        for selector_set in selector_sets:
            for selector in selector_set:
                if selector not in seen:
                    merged.append(selector)
                    seen.add(selector)

        return merged

    def _prioritize_selectors(
        self, field: str, url: Optional[str] = None, html: Optional[str] = None
    ) -> List[str]:
        """
        Create prioritized fallback chain of selectors based on configurable order.

        Reads fallback_chain.steps from settings.json to determine order.
        Supports: config_selectors, cms_selectors, adaptive_selectors, manual_detection
        Only includes cms_selectors if CMS detection confidence >= threshold.
        """
        prioritized_selectors = []

        # Detect CMS with confidence if needed
        cms_type = None
        cms_confidence = 0.0
        if url or html:
            cms_result = self.cms_detector.detect_cms_by_patterns(url=url, html=html)
            cms_type = cms_result.cms_type
            cms_confidence = cms_result.confidence

        # Apply configurable fallback steps
        for step in self._fallback_steps:
            if step == "config_selectors":
                config_selectors = self.selectors.get(field, [])
                prioritized_selectors.extend(config_selectors)
            elif step == "cms_selectors":
                if cms_type and cms_confidence >= self._cms_confidence_threshold:
                    cms_selectors = self._get_cms_selectors(cms_type, field)
                    prioritized_selectors.extend(cms_selectors)
            elif step == "adaptive_selectors":
                if url:
                    domain = self._extract_domain(url)
                    adaptive_selectors = self._get_adaptive_selectors(
                        domain, field, html
                    )
                    prioritized_selectors.extend(adaptive_selectors)
            elif step == "manual_detection":
                if html:
                    detected_selectors = self._detect_selectors_from_html(html, field)
                    prioritized_selectors.extend(detected_selectors)

        # Remove duplicates while preserving priority order
        return self._merge_selector_sets(prioritized_selectors)

    def _detect_cms_type(self, url: str, html: Optional[str] = None) -> Optional[str]:
        """Detect CMS type from URL or HTML content using integrated CMSDetection. Deprecated: use cms_detector.detect_cms_by_patterns directly."""
        warnings.warn(
            "_detect_cms_type is deprecated, use cms_detector.detect_cms_by_patterns directly",
            DeprecationWarning,
            stacklevel=2,
        )
        try:
            result = self.cms_detector.detect_cms_by_patterns(url=url, html=html)
            if result.confidence >= self._cms_confidence_threshold:
                return result.cms_type
            return None
        except Exception as e:
            self.logger.debug(f"CMS detection failed in _detect_cms_type: {e}")
            return None

    def _detect_selectors_from_html(self, html: str, field: str) -> List[str]:
        """Detect selectors from HTML structure for manual fallback."""
        try:
            soup = self._get_bs4_soup(html)
            detected = []

            if field == "name":
                detected = self._detect_name_selectors(soup)
            elif field == "price":
                detected = self._detect_price_selectors(soup)
            elif field == "stock":
                detected = self._detect_stock_selectors(soup)

            return detected[:3]  # Limit to top 3
        except Exception as e:
            self.logger.debug(f"Manual selector detection failed for {field}: {e}")
            return []

    def _track_selector_success(
        self, field: str, selector: str, url: Optional[str] = None
    ) -> None:
        """Track successful selector usage for learning."""
        if self.adaptive_learner and url:
            domain = self._extract_domain(url)
            self.adaptive_learner.update_selector_confidence(
                domain, field, selector, True
            )

    def _track_selector_failure(
        self, field: str, selector: str, url: Optional[str] = None
    ) -> None:
        """Track failed selector usage for learning."""
        if self.adaptive_learner and url:
            domain = self._extract_domain(url)
            self.adaptive_learner.update_selector_confidence(
                domain, field, selector, False
            )

    @property
    def variation_parser(self) -> VariationParser:
        """Lazy initialization of variation parser."""
        if self._variation_parser is None:
            if self.page:
                self._variation_parser = VariationParser(
                    self.antibot_manager, self.page
                )
            else:
                self._variation_parser = VariationParser(self.antibot_manager)
        return self._variation_parser

    def parse_product(self, product_url: str) -> ProductData:
        """
        Parse product data from URL with comprehensive error handling and performance monitoring.

        Args:
            product_url: URL of the product to parse

        Returns:
            ProductData: Structured product data

        Raises:
            ParsingError: If parsing fails critically
        """
        start_time = time.time()
        self.logger.debug(f"Starting product parsing for: {product_url}")

        # Set current URL for adaptive learning
        self._current_url = product_url

        try:
            # Get HTML with caching and validation
            html = self._get_html_source(product_url)

            # Apply human-like behavior if using Playwright
            if self.page:
                self._apply_human_behavior()

            # Extract data with fallbacks and error handling
            name, _ = self._extract_name_with_fallback(
                html, product_url, include_selector=True
            )
            price, _ = self._extract_price_with_fallback(
                html, product_url, include_selector=True
            )
            stock, _ = self._extract_stock_with_fallback(
                html, product_url, include_selector=True
            )

            # Get variations with lazy loading
            variations = self._extract_variations_with_fallback(html)

            # Learning is now handled in individual extraction methods with _track_selector_success
            # The adaptive learner gets real-time feedback on selector performance

            # Build result
            if self.config.return_none_on_missing:
                result: ProductData = {
                    "url": product_url,
                    "name": name,
                    "price": price,
                    "base_price": price,
                    "stock": stock,
                    "in_stock": stock is not None and stock > 0,
                    "stock_quantity": stock,
                    "variations": variations,
                    "error": None,
                }
            else:
                normalized_price = price if price is not None else 0.0
                normalized_stock = stock if stock is not None else 0
                result: ProductData = {
                    "url": product_url,
                    "name": name or "",
                    "price": normalized_price,
                    "base_price": normalized_price,
                    "stock": normalized_stock,
                    "in_stock": normalized_stock > 0,
                    "stock_quantity": normalized_stock,
                    "variations": variations,
                    "error": None,
                }

            parsing_time = time.time() - start_time
            self.logger.debug(f"Product parsing completed in {parsing_time:.2f}s")

            # Structured performance logging
            self.structured_logger.log_performance(
                operation="parse_product",
                duration=parsing_time,
                metadata={"url": product_url, "success": True},
            )

            return result

        except Exception as e:
            parsing_time = time.time() - start_time
            error_msg = f"Error parsing {product_url}: {str(e)}"
            self.logger.error(f"{error_msg} (took {parsing_time:.2f}s)")

            # Structured error logging
            error_context = ErrorContext(url=product_url, execution_time=parsing_time)
            self.structured_logger.log_error(e, context=error_context)

            if self.config.return_none_on_missing:
                return {
                    "url": product_url,
                    "name": None,
                    "price": None,
                    "base_price": None,
                    "stock": None,
                    "in_stock": False,
                    "stock_quantity": None,
                    "variations": [],
                    "error": str(e),
                }
            else:
                return {
                    "url": product_url,
                    "name": "",
                    "price": 0.0,
                    "base_price": 0.0,
                    "stock": 0,
                    "in_stock": False,
                    "stock_quantity": 0,
                    "variations": [],
                    "error": str(e),
                }

    async def parse_product_optimized(
        self, url: str, page: Optional[AsyncPage] = None, reuse_page: bool = True
    ) -> ProductData:
        """Async optimized parsing leveraging pooled Playwright contexts."""
        if not url:
            raise ValueError("URL required for optimized parsing")

        manager = self._resolve_playwright_manager()
        if not manager:
            raise RuntimeError("Playwright manager unavailable for optimized parsing")

        context: Optional["BrowserContext"] = None
        working_page = page
        own_page = page is None
        should_reuse = reuse_page

        if working_page is None:
            working_page, context, manager = await self.get_or_create_optimized_page(
                url, reuse_page=reuse_page
            )
        else:
            context = working_page.context if hasattr(working_page, "context") else None

        start_time = time.time()
        try:
            nav_start = time.time()
            navigation_success = False

            antibot_manager = getattr(self.antibot_manager, "playwright_manager", None)
            if (
                antibot_manager
                and manager == antibot_manager
                and hasattr(self.antibot_manager, "navigate_with_antibot")
            ):
                navigation_success = await self.antibot_manager.navigate_with_antibot(
                    working_page, url
                )
            else:
                navigation_success = await manager.navigate_with_optimization(
                    working_page, url
                )

            nav_time = time.time() - nav_start
            self.parsing_metrics["navigation_times"].append(nav_time)

            if not navigation_success:
                raise RuntimeError(f"Navigation failed for {url}")

            html = await working_page.content()
            extraction_start = time.time()
            parsed = self.parse_product_from_html(html, url)
            self.parsing_metrics["extraction_times"].append(
                time.time() - extraction_start
            )

            return parsed
        finally:
            total_time = time.time() - start_time
            self.parsing_metrics["memory_usage"].append(total_time)

            if own_page and working_page and context and manager:
                await manager.return_page_to_pool(working_page, context)
                if should_reuse:
                    self.parsing_metrics["page_reuses"] += 1

    async def extract_with_optimized_page(
        self, selectors: List[str], page: AsyncPage, extraction_type: str = "text"
    ) -> Optional[str]:
        """Extract value using an optimized async Playwright page."""
        if not selectors:
            return None

        for selector in selectors:
            try:
                await page.wait_for_selector(selector, timeout=self.config.timeout)
                element = await page.query_selector(selector)
                if not element:
                    continue

                if extraction_type == "text":
                    return await element.inner_text()
                if extraction_type == "html":
                    return await element.inner_html()
                if extraction_type == "attribute":
                    return await element.get_attribute("value")
            except Exception:  # noqa: BLE001
                continue

        return None

    async def get_or_create_optimized_page(
        self, url: str, reuse_page: bool = True
    ) -> Tuple[AsyncPage, "BrowserContext", AsyncPlaywrightManager]:
        """Get or reuse a Playwright page from the pool."""
        manager = self._resolve_playwright_manager()
        if not manager:
            raise RuntimeError("Playwright manager is required for page pooling")

        domain = self._extract_domain(url)
        if reuse_page and self._pooled_context is None:
            self._pooled_context = await manager.get_optimized_browser_context(
                domain=domain
            )

        context = (
            self._pooled_context
            if reuse_page and self._pooled_context
            else await manager.get_optimized_browser_context(domain=domain)
        )
        page = await manager.get_page_from_pool(context)
        return page, context, manager

    def _resolve_playwright_manager(self) -> Optional[AsyncPlaywrightManager]:
        if self.playwright_manager:
            return self.playwright_manager
        antibot_manager = getattr(self.antibot_manager, "playwright_manager", None)
        if antibot_manager:
            return antibot_manager
        return None

    def _get_html_source(self, url: Optional[str] = None) -> str:
        """
        Get HTML source with caching, validation, and timeout protection.

        Args:
            url: Optional URL for caching key

        Returns:
            str: HTML content

        Raises:
            ExtractionError: If HTML cannot be retrieved
        """
        cache_key = url or "static_html"

        # Check cache first
        if self.config.cache_enabled and cache_key in self._html_cache:
            self.logger.debug("Using cached HTML")
            return self._html_cache[cache_key]

        try:
            if self.page and url:
                # Playwright with timeout and retry
                def navigate_page():
                    if self.page is None:
                        raise ExtractionError("Page is None")
                    self.page.goto(
                        url, wait_until="domcontentloaded", timeout=self.config.timeout
                    )
                    time.sleep(2)  # Brief wait for dynamic content
                    return self.page.content()

                html = retry_manager.retry(navigate_page, key="page_navigation")
            elif self.html:
                html = self.html
            else:
                if url and self.page is None:
                    try:
                        self.page = self.antibot_manager.get_page()
                        self.logger.debug(
                            "Lazily got Playwright page from antibot manager"
                        )

                        def lazy_navigate():
                            if self.page is None:
                                raise ExtractionError(
                                    "Page is None after lazy initialization"
                                )
                            self.page.goto(
                                url,
                                wait_until="domcontentloaded",
                                timeout=self.config.timeout,
                            )
                            time.sleep(2)  # Brief wait for dynamic content
                            return self.page.content()

                        html = retry_manager.retry(lazy_navigate, key="page_navigation")
                    except Exception as e:
                        raise ExtractionError(f"Failed to lazily get page: {e}")
                else:
                    raise ExtractionError("No HTML source available")

            # Validate HTML
            if not html or len(html.strip()) < 100:
                raise ExtractionError("Invalid or empty HTML content")

            # Cache if enabled
            if self.config.cache_enabled:
                self._html_cache[cache_key] = html

            return html

        except PlaywrightTimeoutError as e:
            raise ExtractionError(f"Timeout loading page: {e}")
        except Exception as e:
            raise ExtractionError(f"Failed to get HTML: {e}")

    def _apply_human_behavior(self) -> None:
        """Apply human-like behavior with error handling."""
        try:
            if self.antibot_manager and self.page:
                self.antibot_manager.human_delay()
                self.antibot_manager.human_scroll(self.page)
                self.antibot_manager.human_mouse_move(self.page)
            else:
                time.sleep(random.uniform(1, 2))
        except Exception as e:
            self.logger.warning(f"Human behavior application failed: {e}")
            time.sleep(1)

    @overload
    def _extract_name_with_fallback(
        self, html: str, url: Optional[str] = None, *, include_selector: Literal[True]
    ) -> Tuple[Optional[str], Optional[str]]:
        ...

    @overload
    def _extract_name_with_fallback(
        self, html: str, url: Optional[str] = None, *, include_selector: Literal[False] = False
    ) -> Optional[str]:
        ...

    def _extract_name_with_fallback(
        self, html: str, url: Optional[str] = None, *, include_selector: bool = False
    ) -> Union[Optional[str], Tuple[Optional[str], Optional[str]]]:
        """Extract product name with CMS-aware prioritized fallback chain."""
        try:
            # Get prioritized selector chain
            prioritized_selectors = self._prioritize_selectors("name", url, html)

            # Try Playwright first with prioritized selectors
            if self.page:
                for selector in prioritized_selectors:
                    try:
                        handle = self.page.locator(selector).first
                        handle.wait_for(state="visible", timeout=self.config.timeout)
                        text = handle.inner_text().strip()
                        if text:
                            # Learning hook: track successful selector
                            self._track_selector_success("name", selector, url)
                            result = sanitize_text(text), selector
                            return result if include_selector else result[0]
                    except Exception:
                        # Learning hook: track failed selector
                        self._track_selector_failure("name", selector, url)
                        continue

            # Fallback to BeautifulSoup with prioritized selectors
            for selector in prioritized_selectors:
                try:
                    result = extract_with_bs4(html, [selector])
                    if result:
                        # Learning hook: track successful selector
                        self._track_selector_success("name", selector, url)
                        formatted = sanitize_text(result), selector
                        return formatted if include_selector else formatted[0]
                except Exception:
                    # Learning hook: track failed selector
                    self._track_selector_failure("name", selector, url)
                    continue

            return (None, None) if include_selector else None

        except Exception as e:
            self.logger.warning(f"Name extraction failed: {e}")
            return (None, None) if include_selector else None

    @overload
    def _extract_price_with_fallback(
        self, html: str, url: Optional[str] = None, *, include_selector: Literal[True]
    ) -> Tuple[Optional[float], Optional[str]]:
        ...

    @overload
    def _extract_price_with_fallback(
        self, html: str, url: Optional[str] = None, *, include_selector: Literal[False] = False
    ) -> Optional[float]:
        ...

    def _extract_price_with_fallback(
        self, html: str, url: Optional[str] = None, *, include_selector: bool = False
    ) -> Union[Optional[float], Tuple[Optional[float], Optional[str]]]:
        """Extract product price with CMS-aware prioritized fallback chain."""
        try:
            # Get prioritized selector chain
            prioritized_selectors = self._prioritize_selectors("price", url, html)

            # Try Playwright first with prioritized selectors
            if self.page:
                for selector in prioritized_selectors:
                    try:
                        handle = self.page.locator(selector).first
                        handle.wait_for(state="visible", timeout=self.config.timeout)
                        text = handle.inner_text().strip()
                        if text:
                            price = clean_price(text)
                            if price is not None:
                                # Learning hook: track successful selector
                                self._track_selector_success("price", selector, url)
                                result = price, selector
                                return result if include_selector else result[0]
                    except Exception:
                        # Learning hook: track failed selector
                        self._track_selector_failure("price", selector, url)
                        continue

            # Fallback to BeautifulSoup with prioritized selectors
            for selector in prioritized_selectors:
                try:
                    result = extract_with_bs4(html, [selector])
                    if result:
                        price = clean_price(result)
                        if price is not None:
                            # Learning hook: track successful selector
                            self._track_selector_success("price", selector, url)
                            formatted = price, selector
                            return formatted if include_selector else formatted[0]
                except Exception:
                    # Learning hook: track failed selector
                    self._track_selector_failure("price", selector, url)
                    continue

            return (None, None) if include_selector else None

        except Exception as e:
            self.logger.warning(f"Price extraction failed: {e}")
            return (None, None) if include_selector else None

    @overload
    def _extract_stock_with_fallback(
        self, html: str, url: Optional[str] = None, *, include_selector: Literal[True]
    ) -> Tuple[Optional[int], Optional[str]]:
        ...

    @overload
    def _extract_stock_with_fallback(
        self, html: str, url: Optional[str] = None, *, include_selector: Literal[False] = False
    ) -> Optional[int]:
        ...

    def _extract_stock_with_fallback(
        self, html: str, url: Optional[str] = None, *, include_selector: bool = False
    ) -> Union[Optional[int], Tuple[Optional[int], Optional[str]]]:
        """Extract product stock with CMS-aware prioritized fallback chain."""
        try:
            # Get prioritized selector chain
            prioritized_selectors = self._prioritize_selectors("stock", url, html)

            # Try Playwright first with prioritized selectors
            if self.page:
                for selector in prioritized_selectors:
                    try:
                        handle = self.page.locator(selector).first
                        handle.wait_for(state="visible", timeout=self.config.timeout)
                        text = handle.inner_text().strip()
                        if text:
                            stock = parse_stock(text)
                            if stock is not None:
                                # Learning hook: track successful selector
                                self._track_selector_success("stock", selector, url)
                                result = stock, selector
                                return result if include_selector else result[0]
                    except Exception:
                        # Learning hook: track failed selector
                        self._track_selector_failure("stock", selector, url)
                        continue

            # Fallback to BeautifulSoup with prioritized selectors
            for selector in prioritized_selectors:
                try:
                    result = extract_with_bs4(html, [selector])
                    if result:
                        stock = parse_stock(result)
                        if stock is not None:
                            # Learning hook: track successful selector
                            self._track_selector_success("stock", selector, url)
                            formatted = stock, selector
                            return formatted if include_selector else formatted[0]
                except Exception:
                    # Learning hook: track failed selector
                    self._track_selector_failure("stock", selector, url)
                    continue

            firecrawl_stock = self._extract_stock_with_firecrawl(url)
            if firecrawl_stock is not None:
                result = firecrawl_stock, "firecrawl"
                return result if include_selector else result[0]

            return (None, None) if include_selector else None

        except Exception as e:
            self.logger.warning(f"Stock extraction failed: {e}")
            return (None, None) if include_selector else None

    def _extract_stock_with_firecrawl(self, url: Optional[str]) -> Optional[int]:
        """Fetch page via Firecrawl and attempt to parse stock when selectors fail."""

        if not url or not self.firecrawl_client:
            return None

        if url in self._firecrawl_stock_cache:
            return self._firecrawl_stock_cache[url]

        markdown = self.firecrawl_client.scrape_markdown(url)
        if not markdown:
            self._firecrawl_stock_cache[url] = None
            return None

        keywords = ("остат", "налич", "stock", "колич", "qty", "нал.)", "в наличии")
        for line in markdown.splitlines():
            lower = line.lower()
            if any(keyword in lower for keyword in keywords) or any(char.isdigit() for char in line):
                stock_value = parse_stock(line)
                if stock_value is not None:
                    self._firecrawl_stock_cache[url] = stock_value
                    return stock_value

        self._firecrawl_stock_cache[url] = None
        return None

    def _extract_variations_with_fallback(self, html: str) -> List[VariationData]:
        """Extract variations with lazy loading and fallback."""
        try:
            current_url = self._current_url
            extractor = self.variation_parser.extract_variations
            signature = inspect.signature(extractor)
            parameters = signature.parameters

            kwargs: Dict[str, Any] = {}
            if "html" in parameters:
                kwargs["html"] = html
            if self.page and "page" in parameters:
                kwargs["page"] = self.page
            if "url" in parameters:
                kwargs["url"] = current_url
            elif current_url is not None:
                # Provide URL context for parsers that rely on internal state
                setattr(self.variation_parser, "_current_url", current_url)

            variations = extractor(**kwargs)

            # Convert to VariationData format
            validated_variations: List[VariationData] = []
            for var in variations:
                if isinstance(var, dict):
                    validated_var: VariationData = {
                        "type": str(var.get("type", "unknown")),
                        "value": str(var.get("value", "")),
                        "price": safe_float_conversion(var.get("price")),
                        "stock": safe_int_conversion(var.get("stock")),
                        "in_stock": (
                            bool(var.get("in_stock"))
                            if var.get("in_stock") is not None
                            else None
                        ),
                        "variant_id": (
                            str(var.get("variant_id")) if var.get("variant_id") else None
                        ),
                        "sku": var.get("sku"),
                        "url": var.get("url"),
                        "attributes": {
                            str(key): str(value)
                            for key, value in (var.get("attributes") or {}).items()
                        },
                    }
                    validated_variations.append(validated_var)

            return validated_variations

        except Exception as e:
            self.logger.warning(f"Variation extraction failed: {e}")
            return []

    def _extract_with_playwright(self, field: str) -> Optional[str]:
        """Extract data using Playwright with timeout and error handling."""
        if not self.page:
            return None

        selectors = self.selectors.get(field, [])
        if not selectors:
            return None

        for selector in selectors:
            try:
                handle = self.page.locator(selector).first
                handle.wait_for(state="visible", timeout=self.config.timeout)
                text = handle.inner_text().strip()
                if text:
                    return text
            except Exception as e:
                self.logger.debug(
                    f"Playwright extraction failed for {field} with selector {selector}: {e}"
                )
                continue

        return None

    def _extract_with_bs4_fallback(self, field: str, html: str) -> Optional[str]:
        """Extract data using BeautifulSoup as fallback."""
        selectors = self.selectors.get(field, [])
        if not selectors:
            return None

        # Use enhanced extract_with_bs4 from helpers
        return extract_with_bs4(html, selectors)

    def parse_product_from_html(self, html: str, url: str) -> ProductData:
        """
        Parse product from provided HTML (for aiohttp integration).

        Args:
            html: HTML content to parse
            url: Product URL for reference

        Returns:
            ProductData: Structured product data
        """
        # Temporarily set html and URL for extraction
        original_html = self.html
        original_url = self._current_url
        self.html = html
        self._current_url = url

        try:
            # Extract data
            name, _ = self._extract_name_with_fallback(
                html, url, include_selector=True
            )
            price, _ = self._extract_price_with_fallback(
                html, url, include_selector=True
            )
            stock, _ = self._extract_stock_with_fallback(
                html, url, include_selector=True
            )
            variations = self._extract_variations_with_fallback(html)

            # Basic SEO fields for parent-level reporting
            try:
                soup = self._get_bs4_soup(html)
                h1_tag = soup.find("h1") if soup else None
                title_tag = soup.find("title") if soup else None
                meta_description_tag = None
                if soup:
                    meta_description_tag = soup.find("meta", attrs={"name": "description"})

                seo_h1 = h1_tag.get_text(strip=True) if h1_tag else None
                seo_title = (
                    title_tag.get_text(strip=True)
                    if title_tag and title_tag.get_text(strip=True)
                    else None
                )
                seo_meta_description = (
                    meta_description_tag.get("content", "").strip()
                    if meta_description_tag and meta_description_tag.get("content")
                    else None
                )
            except Exception:  # pragma: no cover - defensive guard
                seo_h1 = None
                seo_title = None
                seo_meta_description = None

            # Learning is now handled in individual extraction methods with _track_selector_success
            # The adaptive learner gets real-time feedback on selector performance

            if self.config.return_none_on_missing:
                result: ProductData = {
                    "url": url,
                    "name": name,
                    "price": price,
                    "base_price": price,
                    "stock": stock,
                    "in_stock": stock is not None and stock > 0,
                    "stock_quantity": stock,
                    "variations": variations,
                    "error": None,
                    "seo_h1": seo_h1,
                    "seo_title": seo_title,
                    "seo_meta_description": seo_meta_description,
                }
            else:
                normalized_price = price if price is not None else 0.0
                normalized_stock = stock if stock is not None else 0
                result: ProductData = {
                    "url": url,
                    "name": name or "",
                    "price": normalized_price,
                    "base_price": normalized_price,
                    "stock": normalized_stock,
                    "in_stock": normalized_stock > 0,
                    "stock_quantity": normalized_stock,
                    "variations": variations,
                    "error": None,
                    "seo_h1": seo_h1,
                    "seo_title": seo_title,
                    "seo_meta_description": seo_meta_description,
                }

            return result
        finally:
            self.html = original_html
            self._current_url = original_url

    def parse(self, html: str, url: str) -> Dict[str, Any]:
        """Legacy parse interface returning a plain dictionary."""
        product_data = self.parse_product_from_html(html, url)
        return dict(product_data)

    def parse_product_page(self, html: str, url: str) -> ProductData:
        """Backward compatible wrapper for legacy interfaces."""
        return self.parse_product_from_html(html, url)

    def parse_product_optimized_sync(self, url: str) -> ProductData:
        """
        Synchronous wrapper for parse_product_optimized to use in sync contexts.

        Args:
            url: Product URL to parse

        Returns:
            ProductData: Structured product data
        """
        import asyncio

        try:
            # Create event loop if none exists
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # If loop is already running, we need to handle differently
                    # This is a fallback for cases where we're in an async context
                    return asyncio.run(self.parse_product_optimized(url))
                else:
                    # Loop exists but not running
                    return asyncio.run(self.parse_product_optimized(url))
            except RuntimeError:
                # No event loop, create one
                return asyncio.run(self.parse_product_optimized(url))
        except Exception as e:
            self.logger.warning(
                f"Optimized parsing failed, falling back to HTML-only: {e}"
            )
            # Fallback to HTML-only parsing
            try:
                html = self._get_html_source(url)
                return self.parse_product_from_html(html, url)
            except Exception as fallback_error:
                self.logger.error(f"Fallback parsing also failed: {fallback_error}")
                # Return error result
                return {
                    "url": url,
                    "name": "",
                    "price": 0.0,
                    "base_price": 0.0,
                    "stock": 0,
                    "in_stock": False,
                    "stock_quantity": 0,
                    "variations": [],
                    "error": f"Both optimized and fallback parsing failed: {str(e)}, {str(fallback_error)}",
                }

    def detect_selectors(self, html: str) -> Dict[str, List[str]]:
        """
        Dynamically detect selectors based on HTML structure with confidence scoring.

        Args:
            html: HTML content to analyze

        Returns:
            Dict[str, List[str]]: Detected selectors by field
        """
        try:
            soup = self._get_bs4_soup(html)
            detected: Dict[str, List[str]] = {"name": [], "price": [], "stock": []}

            # Detection logic with confidence scoring
            detected["name"] = self._detect_name_selectors(soup)
            detected["price"] = self._detect_price_selectors(soup)
            detected["stock"] = self._detect_stock_selectors(soup)

            # Limit results for performance
            for key in detected:
                detected[key] = detected[key][:5]

            return detected

        except Exception as e:
            self.logger.warning(f"Selector detection failed: {e}")
            return {"name": [], "price": [], "stock": []}

    def _get_bs4_soup(self, html: str) -> BeautifulSoup:
        """Get BeautifulSoup object with caching."""
        cache_key = f"soup_{hash(html)}"

        if self.config.cache_enabled and cache_key in self._bs4_cache:
            return self._bs4_cache[cache_key]

        soup = BeautifulSoup(html, "html.parser")

        if self.config.cache_enabled:
            self._bs4_cache[cache_key] = soup

        return soup

    def _detect_name_selectors(self, soup: BeautifulSoup) -> List[str]:
        """Detect name selectors with confidence scoring."""
        selectors = []
        name_keywords = ["product", "name", "title"]

        for tag in soup.find_all(["h1", "h2", "div", "span"]):
            if isinstance(tag, Tag) and hasattr(tag, "attrs") and "class" in tag.attrs:
                class_list = tag.attrs.get("class", [])
                if isinstance(class_list, list):
                    class_names = " ".join(class_list).lower()
                    if any(keyword in class_names for keyword in name_keywords):
                        if tag.get_text(strip=True):
                            cls_str = ".".join(class_list)
                            selector = f"{tag.name}.{cls_str}"
                            if selector not in selectors:
                                selectors.append(selector)

        return selectors

    def _detect_price_selectors(self, soup: BeautifulSoup) -> List[str]:
        """Detect price selectors with confidence scoring."""
        selectors = []
        price_keywords = ["price", "cost", "₽", "rub", "руб"]

        for tag in soup.find_all(["span", "div", "p"]):
            if isinstance(tag, Tag) and hasattr(tag, "attrs") and "class" in tag.attrs:
                class_list = tag.attrs.get("class", [])
                if isinstance(class_list, list):
                    class_names = " ".join(class_list).lower()
                    if any(keyword in class_names for keyword in price_keywords):
                        if tag.get_text(strip=True):
                            cls_str = ".".join(class_list)
                            selector = f"{tag.name}.{cls_str}"
                            if selector not in selectors:
                                selectors.append(selector)

        return selectors

    def _detect_stock_selectors(self, soup: BeautifulSoup) -> List[str]:
        """Detect stock selectors with confidence scoring."""
        selectors = []
        stock_keywords = ["stock", "availab", "количество", "штук", "шт"]

        for tag in soup.find_all(["span", "div", "p"]):
            if isinstance(tag, Tag) and hasattr(tag, "attrs") and "class" in tag.attrs:
                class_list = tag.attrs.get("class", [])
                if isinstance(class_list, list):
                    class_names = " ".join(class_list).lower()
                    if any(keyword in class_names for keyword in stock_keywords):
                        if tag.get_text(strip=True):
                            cls_str = ".".join(class_list)
                            selector = f"{tag.name}.{cls_str}"
                            if selector not in selectors:
                                selectors.append(selector)

        return selectors

    def close(self) -> None:
        """Close the browser page and cleanup resources."""
        try:
            if self.page:
                self.page.context.close()
                self.page = None
            # Clear caches
            self._html_cache.clear()
            self._bs4_cache.clear()
        except Exception as e:
            self.logger.warning(f"Error during cleanup: {e}")

    # Backward compatibility methods
    def extract_name(self) -> Optional[str]:
        """Legacy method for backward compatibility."""
        warnings.warn(
            "extract_name() is deprecated, use parse_product() instead",
            DeprecationWarning,
            stacklevel=2,
        )
        html = self._get_html_source()
        return self._extract_name_with_fallback(html, self._current_url)

    def extract_price(self) -> Optional[float]:
        """Legacy method for backward compatibility."""
        warnings.warn(
            "extract_price() is deprecated, use parse_product() instead",
            DeprecationWarning,
            stacklevel=2,
        )
        html = self._get_html_source()
        return self._extract_price_with_fallback(html, self._current_url)

    def extract_stock(self) -> Optional[int]:
        """Legacy method for backward compatibility."""
        warnings.warn(
            "extract_stock() is deprecated, use parse_product() instead",
            DeprecationWarning,
            stacklevel=2,
        )
        html = self._get_html_source()
        return self._extract_stock_with_fallback(html, self._current_url)

    # Validation hooks for testing
    def validate_extraction(self, field: str, value: Any) -> bool:
        """Validation hook for testing extraction results."""
        if field == "name":
            return isinstance(value, str) and len(value.strip()) > 0
        elif field == "price":
            return isinstance(value, (int, float)) and value >= 0
        elif field == "stock":
            return isinstance(value, int) and value >= 0
        return True

    def get_performance_stats(self) -> Dict[str, Any]:
        """Get performance statistics for monitoring."""
        stats = {
            "html_cache_size": len(self._html_cache),
            "bs4_cache_size": len(self._bs4_cache),
            "config": self.config.__dict__,
        }

        if self.adaptive_learner:
            stats["adaptive_learner"] = self.adaptive_learner.get_memory_stats()

        return stats

    def get_adaptive_selectors(
        self, field: str, url: Optional[str] = None, html: Optional[str] = None
    ) -> List[str]:
        """
        Get intelligent fallback chain of selectors for a field using CMS-aware prioritization.

        Args:
            field: Field type (name, price, stock)
            url: Optional URL for domain-specific learning
            html: Optional HTML for dynamic discovery

        Returns:
            List of selectors in order of preference
        """
        # Use the new prioritized selector chain
        return self._prioritize_selectors(field, url, html)
