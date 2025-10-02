"""
Adaptive Selector Learner for intelligent CSS selector discovery and optimization.

This module implements an intelligent system that learns and remembers successful
CSS selectors per domain, providing adaptive selector recommendations with
confidence scoring and performance optimization. Now unified with SelectorMemory.
"""

import json
import logging
import time
import warnings
from typing import Dict, List, Optional, Any, Tuple, Union
from functools import lru_cache
from pathlib import Path
import hashlib
from urllib.parse import urlparse
from collections import Counter
import threading
from concurrent.futures import ThreadPoolExecutor
from bs4 import BeautifulSoup, Tag
from core.selector_memory import SelectorMemory, DomainSelectorStore


class AdaptiveSelectorLearner:
    """
    Intelligent selector learning system that adapts to different domains.

    Features:
    - Unified with SelectorMemory for storage and confidence updates
    - HTML structure analysis for selector discovery
    - Real-time learning from ProductParser integration
    - Caching and lazy loading for performance
    - Intelligent fallback chain creation
    """

    def __init__(
        self,
        selector_memory: SelectorMemory,
        cache_size: int = 100,
        config: Optional[Dict[str, Any]] = None,
        ):
        """
        Initialize the Adaptive Selector Learner.

        Args:
            selector_memory: SelectorMemory instance for unified storage
            cache_size: Size of LRU cache for performance optimization
            config: Configuration dictionary from settings
        """
        self.selector_memory = selector_memory
        self.memory_dir = selector_memory.memory_dir

        # Load config if not provided
        if config is None:
            try:
                config_path = Path("config/settings.json")
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
            except Exception as e:
                self.logger.warning(f"Failed to load config: {e}")
                config = {}

        # Get adaptive selectors config
        adaptive_config = config.get("adaptive_selectors", {}) if config else {}

        # Load learning parameters from config
        self.confidence_decay_factor = adaptive_config.get(
            "validation_settings", {}
        ).get("confidence_decay_factor", 0.95)
        self.max_learning_age_days = adaptive_config.get("validation_settings", {}).get(
            "max_learning_age_days", 30
        )
        self.learning_threshold = adaptive_config.get("learning_threshold", 0.8)

        self.logger = logging.getLogger(__name__)
        self._lock = threading.RLock()

        # Performance optimizations
        self._html_cache: Dict[str, BeautifulSoup] = {}
        self._selector_cache: Dict[str, List[str]] = {}
        self._domain_cache: Dict[str, Any] = {}

        # Thread pool for async operations
        self._executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="selector_learner"
        )

        # Initialize caches
        self._init_caches(cache_size)

    def _init_caches(self, cache_size: int) -> None:
        """Initialize LRU caches for performance."""
        self._analyze_html_structure = lru_cache(maxsize=cache_size)(
            self._analyze_html_structure_uncached
        )

    def learn_selectors(
        self, domain: str, html: str, successful_selectors: Dict[str, str]
    ) -> None:
        """
        Learn from successful selector extractions.

        Args:
            domain: Domain name
            html: HTML content where selectors were successful
            successful_selectors: Dict of field -> successful_selector
        """
        with self._lock:
            # Learn from successful selectors using SelectorMemory
            for field, selector in successful_selectors.items():
                if selector:
                    # Update confidence for the successful selector
                    self.selector_memory.update_selector_confidence(
                        domain, field, selector, True
                    )

                    # Also learn similar selectors from HTML analysis
                    similar_selectors = self._find_similar_selectors(
                        html, selector, field
                    )
                    for similar_selector in similar_selectors[:3]:  # Limit to top 3
                        if similar_selector != selector:
                            self.selector_memory.update_selector_confidence(
                                domain, field, similar_selector, True
                            )

            store = self.selector_memory.stores.get(domain)
            if store is not None:
                store.total_learning_sessions += 1

    def test_selector_effectiveness(
        self, domain: str, html: str, field: str, selector: str
    ) -> Tuple[bool, float]:
        """
        Test a selector's effectiveness on given HTML.

        Args:
            domain: Domain name
            html: HTML content to test on
            field: Field type (name, price, stock)
            selector: CSS selector to test

        Returns:
            Tuple of (success, extraction_time)
        """
        start_time = time.time()

        try:
            soup = self._get_soup(html)
            elements = soup.select(selector)

            if elements:
                text = elements[0].get_text(strip=True)
                success = bool(text and len(text) > 0)
            else:
                success = False

        except Exception as e:
            self.logger.debug(f"Selector test failed for {selector}: {e}")
            success = False

        extraction_time = time.time() - start_time

        # Update memory with results using SelectorMemory
        self.selector_memory.update_selector_confidence(
            domain, field, selector, success, extraction_time
        )

        return success, extraction_time

    def get_adaptive_selectors(
        self,
        domain: str,
        field: str,
        html: Optional[str] = None,
        fallback_limit: int = 10,
    ) -> List[str]:
        """
        Get adaptive selectors for a domain and field.

        Args:
            domain: Domain name
            field: Field type (name, price, stock)
            html: Optional HTML for dynamic selector discovery
            fallback_limit: Maximum number of fallback selectors

        Returns:
            List of selectors ordered by confidence
        """
        with self._lock:
            # Get learned selectors from SelectorMemory
            domain_selectors = self.selector_memory.load_domain_selectors(domain)
            adaptive_selectors = domain_selectors.get(field, [])

            # If we have HTML, discover additional selectors
            if html:
                discovered_selectors = self._discover_selectors(html, field)
                for selector in discovered_selectors:
                    if selector not in adaptive_selectors:
                        adaptive_selectors.append(selector)
                        # Add to memory for future use
                        self.selector_memory.update_selector_confidence(
                            domain, field, selector, True
                        )

            # Add generic fallbacks if needed
            if len(adaptive_selectors) < fallback_limit:
                generic_selectors = self._get_generic_selectors(field)
                for selector in generic_selectors:
                    if selector not in adaptive_selectors:
                        adaptive_selectors.append(selector)
                        if len(adaptive_selectors) >= fallback_limit:
                            break

        return adaptive_selectors[:fallback_limit]

    def get_domain_memory(self, domain: str) -> Dict[str, List[str]]:
        """Backward compatible access to domain selector memory."""
        with self._lock:
            store = self.selector_memory.stores.get(domain)
            if store is not None:
                return store
            selectors = self.selector_memory.load_domain_selectors(domain)
            new_store = DomainSelectorStore(domain=domain)
            for field, entries in selectors.items():
                new_store.selectors[field].extend(entries)
            self.selector_memory.stores[domain] = new_store
            return new_store

    def _save_domain_memory(
        self, store: Union[DomainSelectorStore, Dict[str, List[str]]]
    ) -> None:
        """Persist domain memory to storage."""
        with self._lock:
            if isinstance(store, DomainSelectorStore):
                self.selector_memory._save_domain_store(store)
            else:
                domain = getattr(store, "domain", None)
                if not domain:
                    return
                domain_store = self.selector_memory.stores.get(domain)
                if domain_store:
                    self.selector_memory._save_domain_store(domain_store)

    def update_selector_confidence(
        self,
        domain: str,
        field: str,
        selector: str,
        success: bool,
        extraction_time: float = 0.0,
    ) -> None:
        """
        Update confidence score for a selector.

        Args:
            domain: Domain name
            field: Field type
            selector: CSS selector
            success: Whether extraction was successful
            extraction_time: Time taken for extraction
        """
        # Update confidence using SelectorMemory
        self.selector_memory.update_selector_confidence(
            domain, field, selector, success, extraction_time
        )

    def _analyze_html_structure_uncached(self, html: str) -> Dict[str, Any]:
        """Analyze HTML structure for pattern recognition."""
        try:
            soup = self._get_soup(html)
            patterns = {
                "has_structured_data": bool(
                    soup.find("script", {"type": "application/ld+json"})
                ),
                "has_microdata": bool(soup.find(attrs={"itemtype": True})),
                "common_classes": self._extract_common_classes(soup),
                "common_ids": self._extract_common_ids(soup),
                "timestamp": time.time(),
            }
            return patterns
        except Exception as e:
            self.logger.debug(f"HTML structure analysis failed: {e}")
            return {}

    def _discover_selectors(self, html: str, field: str) -> List[str]:
        """Discover potential selectors for a field from HTML."""
        try:
            soup = self._get_soup(html)
            selectors = []

            if field == "name":
                selectors.extend(self._discover_name_selectors(soup))
            elif field == "price":
                selectors.extend(self._discover_price_selectors(soup))
            elif field == "stock":
                selectors.extend(self._discover_stock_selectors(soup))

            return selectors[:5]  # Limit results
        except Exception as e:
            self.logger.debug(f"Selector discovery failed for {field}: {e}")
            return []

    def _discover_name_selectors(self, soup: BeautifulSoup) -> List[str]:
        """Discover potential name selectors."""
        selectors = []
        name_keywords = ["product", "name", "title", "item", "goods"]

        for tag in soup.find_all(["h1", "h2", "h3", "div", "span", "p"]):
            if isinstance(tag, Tag):
                class_list = tag.get("class")
                if class_list and isinstance(class_list, list):
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
            if isinstance(tag, Tag):
                class_list = tag.get("class")
                if class_list and isinstance(class_list, list):
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
            if isinstance(tag, Tag):
                class_list = tag.get("class")
                if class_list and isinstance(class_list, list):
                    class_str = " ".join(class_list).lower()
                    if any(keyword in class_str for keyword in stock_keywords):
                        selector = f"{tag.name}.{'.'.join(class_list)}"
                        if selector not in selectors:
                            selectors.append(selector)

        return selectors

    def _find_similar_selectors(
        self, html: str, base_selector: str, field: str
    ) -> List[str]:
        """Find selectors similar to a successful one."""
        try:
            soup = self._get_soup(html)
            similar_selectors = []

            # Parse base selector to understand structure
            if "." in base_selector:
                tag, class_name = base_selector.split(".", 1)
                # Find similar elements with different classes
                for element in soup.find_all(tag):
                    if isinstance(element, Tag):
                        class_list = element.get("class")
                        if (
                            class_list
                            and isinstance(class_list, list)
                            and class_name in " ".join(class_list)
                        ):
                            for cls in class_list:
                                if cls != class_name and len(cls) > 2:
                                    similar_selector = f"{tag}.{cls}"
                                    if similar_selector not in similar_selectors:
                                        similar_selectors.append(similar_selector)

            return similar_selectors[:3]
        except Exception:
            return []

    def _get_generic_selectors(self, field: str) -> List[str]:
        """Get generic fallback selectors for a field."""
        generic_selectors = {
            "name": [
                "h1",
                ".product-title",
                ".product-name",
                ".item-title",
                "[data-product-name]",
                '[itemprop="name"]',
                ".title",
            ],
            "price": [
                ".price",
                ".product-price",
                ".cost",
                ".price-current",
                "[data-price]",
                '[itemprop="price"]',
                ".current-price",
            ],
            "stock": [
                ".stock",
                ".availability",
                ".stock-status",
                ".quantity",
                "[data-stock]",
                ".in-stock",
                ".stock-info",
            ],
        }
        return generic_selectors.get(field, [])

    def _get_soup(self, html: str) -> BeautifulSoup:
        """Get BeautifulSoup object with caching."""
        html_hash = hashlib.md5(html.encode()).hexdigest()

        if html_hash in self._html_cache:
            return self._html_cache[html_hash]

        soup = BeautifulSoup(html, "html.parser")
        self._html_cache[html_hash] = soup

        # Limit cache size
        if len(self._html_cache) > 50:
            # Remove oldest entries (simple FIFO)
            oldest_keys = list(self._html_cache.keys())[:10]
            for key in oldest_keys:
                del self._html_cache[key]

        return soup

    def _extract_common_classes(self, soup: BeautifulSoup) -> List[str]:
        """Extract most common CSS classes."""
        classes = []
        for tag in soup.find_all(attrs={"class": True}):
            if isinstance(tag, Tag):
                class_list = tag.get("class")
                if class_list and isinstance(class_list, list):
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

        return list(set(ids))  # Remove duplicates

    def cleanup_memory(self, max_age_days: int = 90) -> Dict[str, int]:
        """
        Cleanup old/unused selectors across all domains.

        Args:
            max_age_days: Maximum age in days for selectors to keep

        Returns:
            Dict of domain -> removed_selectors_count
        """
        # Use SelectorMemory's cleanup functionality
        return self.selector_memory.cleanup_old_selectors(max_age_days)

    def get_memory_stats(self) -> Dict[str, Any]:
        """Get statistics about the selector memory."""
        # Use SelectorMemory's stats functionality
        return self.selector_memory.get_memory_stats()

    def integrate_with_parser(self, parser_instance) -> None:
        """
        Integrate with ProductParser for real-time learning.

        Args:
            parser_instance: ProductParser instance to integrate with
        """
        warnings.warn(
            "integrate_with_parser is deprecated. Use ProductParser's built-in tracking hooks instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        # Monkey patch the extraction methods to learn from successes
        original_extract_name = parser_instance._extract_name_with_fallback
        original_extract_price = parser_instance._extract_price_with_fallback
        original_extract_stock = parser_instance._extract_stock_with_fallback

        def learning_extract_name(html: str, url: Optional[str] = None):
            result, selector_used = original_extract_name(
                html, url, include_selector=True
            )
            return result, selector_used

        def learning_extract_price(html: str, url: Optional[str] = None):
            result, selector_used = original_extract_price(
                html, url, include_selector=True
            )
            return result, selector_used

        def learning_extract_stock(html: str, url: Optional[str] = None):
            result, selector_used = original_extract_stock(
                html, url, include_selector=True
            )
            return result, selector_used

        # Apply the patches
        parser_instance._extract_name_with_fallback = learning_extract_name
        parser_instance._extract_price_with_fallback = learning_extract_price
        parser_instance._extract_stock_with_fallback = learning_extract_stock

    def _extract_domain(self, url: str) -> str:
        """Extract domain from URL."""
        try:
            parsed = urlparse(url)
            return parsed.netloc.replace("www.", "")
        except Exception:
            return "unknown"

    def __del__(self):
        """Cleanup resources on deletion."""
        try:
            self._executor.shutdown(wait=False)
        except Exception:
            pass
