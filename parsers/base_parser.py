from abc import ABC, abstractmethod
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from selenium.webdriver.remote.webelement import WebElement
from typing import List, Optional, Dict, Any, Tuple, Union, Callable
import time
import logging
import hashlib
from functools import lru_cache
from urllib.parse import urlparse
from dataclasses import dataclass
from collections import Counter
from core.adaptive_selector_learner import AdaptiveSelectorLearner
from core.selector_memory import SelectorMemory
from bs4 import BeautifulSoup, Tag
import threading


@dataclass
class CMSConfig:
    """Configuration for CMS-specific extraction."""

    cms_type: Optional[str] = None
    timeout: int = 10000
    retry_attempts: int = 3
    cache_enabled: bool = True
    adaptive_learning: bool = True
    graceful_degradation: bool = True


@dataclass
class ExtractionResult:
    """Result of an extraction operation with metadata."""

    value: Optional[str]
    selector_used: Optional[str]
    confidence_score: float
    extraction_time: float
    cms_type: Optional[str]
    success: bool


class CMSException(Exception):
    """Exception raised for CMS-specific parsing errors."""

    pass


class BaseParser(ABC):
    """
    Enhanced BaseParser with universal CMS support and adaptive capabilities.

    Features:
    - CMS-aware extraction with platform-agnostic patterns
    - Adaptive learning integration with confidence scoring
    - Intelligent selector fallback chains
    - Performance optimizations with caching and batch processing
    - Comprehensive error handling with graceful degradation
    - Testing support and debugging utilities
    """

    def __init__(
        self,
        driver: webdriver.Chrome,
        config: Optional[CMSConfig] = None,
        adaptive_learner: Optional[AdaptiveSelectorLearner] = None,
        selector_memory: Optional[SelectorMemory] = None,
    ):
        """
        Initialize the enhanced BaseParser.

        Args:
            driver: Selenium WebDriver instance
            config: CMS configuration
            adaptive_learner: Optional AdaptiveSelectorLearner instance
            selector_memory: Optional SelectorMemory instance
        """
        self.driver = driver
        self.config = config or CMSConfig()

        # Initialize selector memory for unified storage
        self.selector_memory = selector_memory or SelectorMemory()

        # Initialize adaptive learner with selector memory if not provided
        if adaptive_learner is None:
            self.adaptive_learner = AdaptiveSelectorLearner(
                selector_memory=self.selector_memory
            )
        else:
            self.adaptive_learner = adaptive_learner

        # Initialize logger
        self.logger = logging.getLogger(__name__)

        # Performance and caching
        self._extraction_cache: Dict[str, ExtractionResult] = {}
        self._html_cache: Dict[str, str] = {}
        self._soup_cache: Dict[str, BeautifulSoup] = {}
        self._lock = threading.RLock()

        # Current context
        self._current_url: Optional[str] = None
        self._current_html: Optional[str] = None
        self._detected_cms: Optional[str] = None

        # Statistics
        self._extraction_stats = {
            "total_extractions": 0,
            "successful_extractions": 0,
            "failed_extractions": 0,
            "cache_hits": 0,
            "adaptive_fallbacks": 0,
        }

    def extract_text(
        self, driver: webdriver.Chrome, selectors: List[str]
    ) -> Optional[str]:
        """Extract text from element using multiple selectors"""
        for selector in selectors:
            try:
                element = driver.find_element(By.CSS_SELECTOR, selector)
                return element.text.strip()
            except NoSuchElementException:
                continue
        return None

    def extract_attribute(
        self, driver: webdriver.Chrome, selector: str, attr: str
    ) -> Optional[str]:
        """Extract attribute from element"""
        try:
            element = driver.find_element(By.CSS_SELECTOR, selector)
            return element.get_attribute(attr)
        except NoSuchElementException:
            return None

    def get_page_source(self) -> str:
        """Get current page source"""
        return self.driver.page_source

    # CMS Detection and Platform-Agnostic Methods

    def detect_cms_type(
        self, url: Optional[str] = None, html: Optional[str] = None
    ) -> Optional[str]:
        """
        Detect CMS type from URL or HTML content.

        Args:
            url: Optional URL to analyze
            html: Optional HTML content to analyze

        Returns:
            CMS type string or None if not detected
        """
        cms_indicators = {
            "wordpress": ["wordpress", "wp-", "/wp-", "woocommerce", "wp-content"],
            "joomla": ["joomla", "com_virtuemart", "virtuemart", "jdoc"],
            "magento": ["magento", "mage", "var/cache", "Mage.Cookies"],
            "shopify": ["shopify", "myshopify", "cdn.shopify.com", "shopify-section"],
            "opencart": ["opencart", "route=product", "index.php?route="],
            "bitrix": ["bitrix", "bx", "/bitrix/", "bitrix24"],
            "prestashop": ["prestashop", "presta", "ps_", "id_product"],
            "drupal": ["drupal", "node/", "taxonomy/term", "sites/default"],
            "woocommerce": ["woocommerce", "wc-", "product-type", "add-to-cart"],
        }

        # Check URL first
        if url:
            url_lower = url.lower()
            for cms, indicators in cms_indicators.items():
                if any(indicator in url_lower for indicator in indicators):
                    self._detected_cms = cms
                    return cms

        # Check HTML content
        if html:
            html_lower = html.lower()
            for cms, indicators in cms_indicators.items():
                if any(indicator in html_lower for indicator in indicators):
                    self._detected_cms = cms
                    return cms

        # Check meta tags and scripts in HTML
        if html:
            try:
                soup = self._get_soup(html)
                # Check meta generator
                generator = soup.find("meta", attrs={"name": "generator"})
                if generator and isinstance(generator, Tag):
                    content = generator.get("content")
                    if content and isinstance(content, str):
                        gen_content = content.lower()
                        for cms, indicators in cms_indicators.items():
                            if any(
                                indicator in gen_content for indicator in indicators
                            ):
                                self._detected_cms = cms
                                return cms

                # Check script sources
                for script in soup.find_all("script", src=True):
                    if isinstance(script, Tag):
                        src = script.get("src")
                        if src and isinstance(src, str):
                            src_lower = src.lower()
                            for cms, indicators in cms_indicators.items():
                                if any(
                                    indicator in src_lower for indicator in indicators
                                ):
                                    self._detected_cms = cms
                                    return cms
            except Exception as e:
                self.logger.debug(f"CMS detection from HTML failed: {e}")

        return None

    def get_cms_specific_selectors(
        self, field: str, cms_type: Optional[str] = None
    ) -> List[str]:
        """
        Get CMS-specific selectors for a field.

        Args:
            field: Field type (name, price, stock, etc.)
            cms_type: CMS type, auto-detected if None

        Returns:
            List of CMS-specific selectors
        """
        if not cms_type:
            cms_type = self._detected_cms or self.detect_cms_type(
                self._current_url, self._current_html
            )

        cms_selectors = {
            "wordpress": {
                "name": [
                    "h1.entry-title",
                    ".product-title",
                    ".woocommerce-loop-product__title",
                ],
                "price": [".price", ".woocommerce-Price-amount", ".amount"],
                "stock": [
                    ".stock",
                    ".availability",
                    ".woocommerce-product-details__short-description",
                ],
            },
            "woocommerce": {
                "name": ["h1.product_title", ".woocommerce-loop-product__title"],
                "price": [".woocommerce-Price-amount", "p.price", ".price"],
                "stock": [".stock", ".availability", "p.stock"],
            },
            "shopify": {
                "name": ["h1.product-single__title", ".product__title"],
                "price": [".product__price", ".price-item", "[data-price]"],
                "stock": [".product__inventory", ".availability", "[data-stock]"],
            },
            "opencart": {
                "name": ["h1.title", ".product-title"],
                "price": [".price-new", ".price"],
                "stock": [".stock-status", ".stock"],
            },
            "magento": {
                "name": [".product-name", ".page-title", "h1"],
                "price": [".price", ".price-box", ".regular-price"],
                "stock": [".availability", ".stock", ".product-availability"],
            },
            "prestashop": {
                "name": ["h1.product-name", ".product-title"],
                "price": [".current-price", ".price", ".product-price"],
                "stock": [".product-availability", ".availability", ".stock"],
            },
        }

        if cms_type and cms_type in cms_selectors:
            return cms_selectors[cms_type].get(field, [])
        return []

    # Adaptive Extraction Methods

    def extract_text_adaptive(
        self,
        selectors: List[str],
        field: str = "generic",
        url: Optional[str] = None,
        html: Optional[str] = None,
        use_cache: bool = True,
    ) -> Optional[str]:
        """
        Extract text using adaptive selector fallback chain.

        Args:
            selectors: Base selectors to try
            field: Field type for adaptive learning
            url: Optional URL for domain-specific learning
            html: Optional HTML for dynamic discovery
            use_cache: Whether to use caching

        Returns:
            Extracted text or None
        """
        start_time = time.time()
        cache_key = None

        if use_cache and self.config.cache_enabled:
            cache_key = self._generate_cache_key("text", selectors, field, url)
            if cache_key in self._extraction_cache:
                self._extraction_stats["cache_hits"] += 1
                cached = self._extraction_cache[cache_key]
                if time.time() - cached.extraction_time < 300:  # 5 min cache
                    return cached.value

        # Build adaptive selector chain
        adaptive_selectors = self._build_adaptive_selector_chain(
            selectors, field, url, html
        )

        # Try extraction with each selector
        for selector in adaptive_selectors:
            try:
                result = self._extract_with_selector(selector)
                if result and result.strip():
                    extraction_time = time.time() - start_time

                    # Update learning systems
                    self._update_adaptive_learning(
                        field, selector, True, extraction_time, url
                    )

                    # Cache result
                    if cache_key:
                        self._extraction_cache[cache_key] = ExtractionResult(
                            value=result,
                            selector_used=selector,
                            confidence_score=1.0,
                            extraction_time=extraction_time,
                            cms_type=self._detected_cms,
                            success=True,
                        )

                    self._extraction_stats["successful_extractions"] += 1
                    return result.strip()

            except Exception as e:
                self.logger.debug(f"Selector {selector} failed: {e}")
                self._update_adaptive_learning(
                    field, selector, False, time.time() - start_time, url
                )
                continue

        self._extraction_stats["failed_extractions"] += 1
        return None

    def extract_attribute_adaptive(
        self,
        selectors: List[str],
        attribute: str,
        field: str = "generic",
        url: Optional[str] = None,
        html: Optional[str] = None,
        use_cache: bool = True,
    ) -> Optional[str]:
        """
        Extract attribute using adaptive selector fallback chain.

        Args:
            selectors: Base selectors to try
            attribute: Attribute name to extract
            field: Field type for adaptive learning
            url: Optional URL for domain-specific learning
            html: Optional HTML for dynamic discovery
            use_cache: Whether to use caching

        Returns:
            Extracted attribute value or None
        """
        start_time = time.time()
        cache_key = None

        if use_cache and self.config.cache_enabled:
            cache_key = self._generate_cache_key(
                "attr", selectors, field, url, attribute
            )
            if cache_key in self._extraction_cache:
                self._extraction_stats["cache_hits"] += 1
                cached = self._extraction_cache[cache_key]
                if time.time() - cached.extraction_time < 300:
                    return cached.value

        # Build adaptive selector chain
        adaptive_selectors = self._build_adaptive_selector_chain(
            selectors, field, url, html
        )

        # Try extraction with each selector
        for selector in adaptive_selectors:
            try:
                element = self.driver.find_element(By.CSS_SELECTOR, selector)
                value = element.get_attribute(attribute)
                if value and value.strip():
                    extraction_time = time.time() - start_time

                    # Update learning systems
                    self._update_adaptive_learning(
                        field, selector, True, extraction_time, url
                    )

                    # Cache result
                    if cache_key:
                        self._extraction_cache[cache_key] = ExtractionResult(
                            value=value,
                            selector_used=selector,
                            confidence_score=1.0,
                            extraction_time=extraction_time,
                            cms_type=self._detected_cms,
                            success=True,
                        )

                    self._extraction_stats["successful_extractions"] += 1
                    return value.strip()

            except Exception as e:
                self.logger.debug(f"Attribute extraction failed for {selector}: {e}")
                self._update_adaptive_learning(
                    field, selector, False, time.time() - start_time, url
                )
                continue

        self._extraction_stats["failed_extractions"] += 1
        return None

    def test_selector_effectiveness(
        self,
        selector: str,
        field: str,
        url: Optional[str] = None,
        html: Optional[str] = None,
    ) -> Tuple[bool, float]:
        """
        Test a selector's effectiveness on current page.

        Args:
            selector: CSS selector to test
            field: Field type
            url: Optional URL for context
            html: Optional HTML for testing

        Returns:
            Tuple of (success, extraction_time)
        """
        start_time = time.time()

        try:
            if html:
                # Test on provided HTML
                soup = self._get_soup(html)
                elements = soup.select(selector)
                success = bool(elements and elements[0].get_text(strip=True))
            else:
                # Test on current page
                element = self.driver.find_element(By.CSS_SELECTOR, selector)
                text = element.text.strip()
                success = bool(text)

        except Exception as e:
            self.logger.debug(f"Selector test failed for {selector}: {e}")
            success = False

        extraction_time = time.time() - start_time

        # Update adaptive learning
        if self.adaptive_learner:
            domain = self._extract_domain(url) if url else "unknown"
            self.adaptive_learner.test_selector_effectiveness(
                domain, html or self._current_html or "", field, selector
            )

        return success, extraction_time

    def discover_selectors(
        self, field: str, html: Optional[str] = None, limit: int = 5
    ) -> List[str]:
        """
        Discover potential selectors for a field from HTML structure.

        Args:
            field: Field type to discover selectors for
            html: Optional HTML content
            limit: Maximum number of selectors to return

        Returns:
            List of discovered selectors
        """
        html_content = html or self._current_html or self.get_page_source()
        if not html_content:
            return []

        try:
            soup = self._get_soup(html_content)
            selectors = []

            if field == "name":
                selectors = self._discover_name_selectors(soup)
            elif field == "price":
                selectors = self._discover_price_selectors(soup)
            elif field == "stock":
                selectors = self._discover_stock_selectors(soup)
            else:
                # Generic discovery
                selectors = self._discover_generic_selectors(soup, field)

            # Use adaptive learner if available
            if self.adaptive_learner and self._current_url:
                domain = self._extract_domain(self._current_url)
                learned_selectors = self.adaptive_learner.get_adaptive_selectors(
                    domain, field, html_content, limit
                )
                selectors.extend(learned_selectors)

            # Remove duplicates and limit
            unique_selectors = []
            seen = set()
            for selector in selectors:
                if selector not in seen:
                    unique_selectors.append(selector)
                    seen.add(selector)

            return unique_selectors[:limit]

        except Exception as e:
            self.logger.warning(f"Selector discovery failed for {field}: {e}")
            return []

    # Intelligent Element Detection and Waiting

    def wait_for_element_adaptive(
        self,
        selectors: List[str],
        timeout: Optional[int] = None,
        cms_aware: bool = True,
    ) -> Optional[Any]:
        """
        Wait for element using adaptive strategies based on CMS type.

        Args:
            selectors: Selectors to wait for
            timeout: Timeout in milliseconds
            cms_aware: Whether to use CMS-specific waiting strategies

        Returns:
            WebElement if found, None otherwise
        """
        timeout = timeout or self.config.timeout

        # CMS-specific waiting adjustments
        if cms_aware and self._detected_cms:
            timeout = self._adjust_timeout_for_cms(timeout)

        # Try each selector with adaptive waiting
        for selector in selectors:
            try:
                wait = WebDriverWait(self.driver, timeout / 1000)
                element = wait.until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, selector))
                )
                return element
            except TimeoutException:
                continue

        return None

    def detect_element_patterns(self, html: Optional[str] = None) -> Dict[str, Any]:
        """
        Detect common element patterns in the page.

        Args:
            html: Optional HTML content

        Returns:
            Dictionary of detected patterns
        """
        html_content = html or self._current_html or self.get_page_source()
        if not html_content:
            return {}

        try:
            soup = self._get_soup(html_content)
            patterns = {
                "has_structured_data": bool(
                    soup.find("script", {"type": "application/ld+json"})
                ),
                "has_microdata": bool(soup.find(attrs={"itemtype": True})),
                "has_json_ld": bool(
                    soup.find("script", {"type": "application/ld+json"})
                ),
                "common_classes": self._extract_common_classes(soup),
                "common_ids": self._extract_common_ids(soup),
                "form_count": len(soup.find_all("form")),
                "script_count": len(soup.find_all("script")),
                "meta_tags": self._extract_meta_tags(soup),
            }
            return patterns
        except Exception as e:
            self.logger.debug(f"Pattern detection failed: {e}")
            return {}

    # Performance Enhancements

    def batch_extract_text(
        self,
        selector_field_pairs: List[Tuple[List[str], str]],
        url: Optional[str] = None,
        html: Optional[str] = None,
    ) -> Dict[str, Optional[str]]:
        """
        Batch extract multiple text fields efficiently.

        Args:
            selector_field_pairs: List of (selectors, field_name) tuples
            url: Optional URL for context
            html: Optional HTML for extraction

        Returns:
            Dictionary of field_name -> extracted_value
        """
        results = {}
        html_content = html or self._current_html or self.get_page_source()

        # Pre-analyze HTML if provided
        soup = self._get_soup(html_content) if html_content else None

        for selectors, field in selector_field_pairs:
            try:
                # Try adaptive extraction first
                result = self.extract_text_adaptive(
                    selectors, field, url, html_content, use_cache=True
                )
                results[field] = result
            except Exception as e:
                self.logger.debug(f"Batch extraction failed for {field}: {e}")
                results[field] = None

        return results

    def clear_cache(self, pattern: Optional[str] = None) -> int:
        """
        Clear extraction cache, optionally by pattern.

        Args:
            pattern: Optional pattern to match cache keys

        Returns:
            Number of entries cleared
        """
        with self._lock:
            if pattern:
                keys_to_remove = [
                    k for k in self._extraction_cache.keys() if pattern in k
                ]
                for key in keys_to_remove:
                    del self._extraction_cache[key]
                return len(keys_to_remove)
            else:
                cleared = len(self._extraction_cache)
                self._extraction_cache.clear()
                self._html_cache.clear()
                self._soup_cache.clear()
                return cleared

    # Error Handling and Graceful Degradation

    def extract_with_fallback(
        self,
        primary_method: Callable,
        fallback_methods: List[Callable],
        *args,
        **kwargs,
    ) -> Any:
        """
        Execute extraction with multiple fallback methods.

        Args:
            primary_method: Primary extraction method
            fallback_methods: List of fallback methods
            *args, **kwargs: Arguments for methods

        Returns:
            Extraction result or None
        """
        # Try primary method
        try:
            result = primary_method(*args, **kwargs)
            if result is not None:
                return result
        except Exception as e:
            self.logger.debug(f"Primary method failed: {e}")

        # Try fallbacks
        for fallback in fallback_methods:
            try:
                result = fallback(*args, **kwargs)
                if result is not None:
                    return result
            except Exception as e:
                self.logger.debug(f"Fallback method failed: {e}")
                continue

        return None

    # Testing and Validation Support

    def validate_extraction(self, field: str, value: Any) -> bool:
        """
        Validate extraction result for a field.

        Args:
            field: Field type
            value: Extracted value

        Returns:
            True if valid, False otherwise
        """
        if value is None:
            return False

        if field == "name":
            return isinstance(value, str) and len(value.strip()) > 0
        elif field == "price":
            try:
                float(value.replace(",", "").replace(" ", ""))
                return True
            except (ValueError, AttributeError):
                return False
        elif field == "stock":
            try:
                int(value)
                return True
            except (ValueError, AttributeError):
                return False

        return True

    def get_extraction_stats(self) -> Dict[str, Any]:
        """
        Get extraction performance statistics.

        Returns:
            Dictionary of statistics
        """
        total = self._extraction_stats["total_extractions"]
        successful = self._extraction_stats["successful_extractions"]

        return {
            "total_extractions": total,
            "successful_extractions": successful,
            "failed_extractions": self._extraction_stats["failed_extractions"],
            "success_rate": successful / total if total > 0 else 0.0,
            "cache_hits": self._extraction_stats["cache_hits"],
            "adaptive_fallbacks": self._extraction_stats["adaptive_fallbacks"],
            "cache_size": len(self._extraction_cache),
            "detected_cms": self._detected_cms,
            "current_url": self._current_url,
        }

    # Debugging Utilities

    def debug_selector(
        self, selector: str, html: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Debug a selector's behavior.

        Args:
            selector: CSS selector to debug
            html: Optional HTML content

        Returns:
            Debug information dictionary
        """
        debug_info = {
            "selector": selector,
            "found_elements": 0,
            "element_html": None,
            "element_text": None,
            "element_attributes": {},
            "errors": [],
        }

        try:
            if html:
                soup = self._get_soup(html)
                elements = soup.select(selector)
                debug_info["found_elements"] = len(elements)
                if elements:
                    element = elements[0]
                    debug_info["element_html"] = str(element)[:500]  # Limit size
                    if isinstance(element, Tag):
                        debug_info["element_text"] = element.get_text(strip=True)
                        debug_info["element_attributes"] = dict(element.attrs)
            else:
                elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                debug_info["found_elements"] = len(elements)
                if elements:
                    element = elements[0]
                    outer_html = element.get_attribute("outerHTML")
                    debug_info["element_html"] = (
                        outer_html[:500] if outer_html else None
                    )
                    debug_info["element_text"] = element.text
                    debug_info["element_attributes"] = {
                        "id": element.get_attribute("id"),
                        "class": element.get_attribute("class"),
                        "data-*": {
                            k: v
                            for k, v in element.__dict__.items()
                            if k.startswith("data_") and v
                        },
                    }

        except Exception as e:
            debug_info["errors"].append(str(e))

        return debug_info

    def set_context(
        self, url: Optional[str] = None, html: Optional[str] = None
    ) -> None:
        """
        Set current context for adaptive operations.

        Args:
            url: Current URL
            html: Current HTML content
        """
        self._current_url = url
        self._current_html = html
        if url or html:
            self._detected_cms = self.detect_cms_type(url, html)

    # Private Helper Methods

    def _extract_domain(self, url: str) -> str:
        """Extract domain from URL."""
        try:
            parsed = urlparse(url)
            return parsed.netloc.replace("www.", "")
        except Exception:
            return "unknown"

    def _build_adaptive_selector_chain(
        self,
        base_selectors: List[str],
        field: str,
        url: Optional[str],
        html: Optional[str],
    ) -> List[str]:
        """Build adaptive selector chain with fallbacks."""
        chain = list(base_selectors)  # Copy base selectors

        # Add CMS-specific selectors
        cms_selectors = self.get_cms_specific_selectors(field, self._detected_cms)
        chain.extend(cms_selectors)

        # Add adaptive selectors from learning
        if self.adaptive_learner and url:
            domain = self._extract_domain(url)
            adaptive_selectors = self.adaptive_learner.get_adaptive_selectors(
                domain, field, html, fallback_limit=5
            )
            chain.extend(adaptive_selectors)

        # Add discovered selectors
        if html:
            discovered = self.discover_selectors(field, html, limit=3)
            chain.extend(discovered)

        # Remove duplicates while preserving order
        seen = set()
        unique_chain = []
        for selector in chain:
            if selector not in seen:
                unique_chain.append(selector)
                seen.add(selector)

        return unique_chain

    def _extract_with_selector(self, selector: str) -> Optional[str]:
        """Extract text using a single selector."""
        element = self.driver.find_element(By.CSS_SELECTOR, selector)
        return element.text.strip()

    def _update_adaptive_learning(
        self,
        field: str,
        selector: str,
        success: bool,
        extraction_time: float,
        url: Optional[str],
    ) -> None:
        """Update adaptive learning systems."""
        if not url:
            return

        domain = self._extract_domain(url)

        # Update adaptive learner
        if self.adaptive_learner:
            self.adaptive_learner.update_selector_confidence(
                domain, field, selector, success, extraction_time
            )

        # Update selector memory
        elif self.selector_memory:
            self.selector_memory.update_selector_confidence(
                domain, field, selector, success, extraction_time
            )

    def _generate_cache_key(
        self,
        extraction_type: str,
        selectors: List[str],
        field: str,
        url: Optional[str],
        attribute: Optional[str] = None,
    ) -> str:
        """Generate cache key for extraction results."""
        key_parts = [extraction_type, field, str(sorted(selectors))]
        if url:
            key_parts.append(url)
        if attribute:
            key_parts.append(attribute)

        key_string = "|".join(key_parts)
        return hashlib.md5(key_string.encode()).hexdigest()

    def _get_soup(self, html: str) -> BeautifulSoup:
        """Get BeautifulSoup object with caching."""
        html_hash = hashlib.md5(html.encode()).hexdigest()

        if html_hash in self._soup_cache:
            return self._soup_cache[html_hash]

        soup = BeautifulSoup(html, "html.parser")
        self._soup_cache[html_hash] = soup

        # Limit cache size
        if len(self._soup_cache) > 10:
            oldest_keys = list(self._soup_cache.keys())[:5]
            for key in oldest_keys:
                del self._soup_cache[key]

        return soup

    def _adjust_timeout_for_cms(self, base_timeout: int) -> int:
        """Adjust timeout based on CMS type."""
        cms_timeouts = {
            "wordpress": base_timeout,
            "shopify": int(base_timeout * 1.2),  # Shopify can be slower
            "magento": int(base_timeout * 1.5),  # Magento often slower
            "woocommerce": int(base_timeout * 1.1),
            "opencart": base_timeout,
            "prestashop": int(base_timeout * 1.3),
        }

        cms_type = self._detected_cms
        if cms_type and cms_type in cms_timeouts:
            return cms_timeouts[cms_type]

        return base_timeout

    def _discover_name_selectors(self, soup: BeautifulSoup) -> List[str]:
        """Discover potential name selectors."""
        selectors = []
        name_keywords = ["product", "name", "title", "item", "goods"]

        for tag in soup.find_all(["h1", "h2", "h3", "div", "span", "p"]):
            if isinstance(tag, Tag) and tag.get("class"):
                class_list = tag.get("class")
                if isinstance(class_list, list):
                    class_str = " ".join(class_list).lower()
                    if any(keyword in class_str for keyword in name_keywords):
                        selector = f"{tag.name}.{'.'.join(class_list)}"
                        if selector not in selectors:
                            selectors.append(selector)

        return selectors

    def _discover_price_selectors(self, soup: BeautifulSoup) -> List[str]:
        """Discover potential price selectors."""
        selectors = []
        price_keywords = ["price", "cost", "цена", "руб", "₽"]

        for tag in soup.find_all(["span", "div", "p", "strong"]):
            if isinstance(tag, Tag) and tag.get("class"):
                class_list = tag.get("class")
                if isinstance(class_list, list):
                    class_str = " ".join(class_list).lower()
                    if any(keyword in class_str for keyword in price_keywords):
                        selector = f"{tag.name}.{'.'.join(class_list)}"
                        if selector not in selectors:
                            selectors.append(selector)

        return selectors

    def _discover_stock_selectors(self, soup: BeautifulSoup) -> List[str]:
        """Discover potential stock selectors."""
        selectors = []
        stock_keywords = ["stock", "avail", "quantity", "колич", "штук"]

        for tag in soup.find_all(["span", "div", "p"]):
            if isinstance(tag, Tag) and tag.get("class"):
                class_list = tag.get("class")
                if isinstance(class_list, list):
                    class_str = " ".join(class_list).lower()
                    if any(keyword in class_str for keyword in stock_keywords):
                        selector = f"{tag.name}.{'.'.join(class_list)}"
                        if selector not in selectors:
                            selectors.append(selector)

        return selectors

    def _discover_generic_selectors(self, soup: BeautifulSoup, field: str) -> List[str]:
        """Discover generic selectors for any field."""
        selectors = []

        # Look for data attributes
        for element in soup.find_all(attrs={f"data-{field}": True}):
            if isinstance(element, Tag):
                selector = f"[data-{field}]"
                if selector not in selectors:
                    selectors.append(selector)

        # Look for common class patterns
        for element in soup.find_all(attrs={"class": True}):
            if isinstance(element, Tag) and element.get("class"):
                class_list = element.get("class")
                if isinstance(class_list, list):
                    class_str = " ".join(class_list).lower()
                    if field.lower() in class_str:
                        selector = f"{element.name}.{'.'.join(class_list)}"
                        if selector not in selectors:
                            selectors.append(selector)

        return selectors

    def _extract_common_classes(self, soup: BeautifulSoup) -> List[str]:
        """Extract most common CSS classes."""
        classes = []
        for tag in soup.find_all(attrs={"class": True}):
            if isinstance(tag, Tag):
                class_list = tag.get("class")
                if isinstance(class_list, list):
                    classes.extend(class_list)

        class_counts = Counter(classes)
        return [cls for cls, count in class_counts.most_common(10) if count > 1]

    def _extract_common_ids(self, soup: BeautifulSoup) -> List[str]:
        """Extract most common IDs."""
        ids = []
        for tag in soup.find_all(attrs={"id": True}):
            if isinstance(tag, Tag):
                tag_id = tag.get("id")
                if tag_id and isinstance(tag_id, str):
                    ids.append(tag_id)

        return list(set(ids))

    def _extract_meta_tags(self, soup: BeautifulSoup) -> Dict[str, str]:
        """Extract meta tags."""
        meta_tags = {}
        for meta in soup.find_all("meta"):
            if isinstance(meta, Tag):
                name = meta.get("name") or meta.get("property")
                content = meta.get("content")
                if (
                    name
                    and content
                    and isinstance(name, str)
                    and isinstance(content, str)
                ):
                    meta_tags[name] = content

        return meta_tags
