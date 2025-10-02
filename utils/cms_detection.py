"""
Comprehensive CMS Detection Module

This module provides advanced CMS detection capabilities for web scraping framework.
Supports detection of major CMS platforms with multiple detection methods and confidence scoring.

Features:
- Meta tag pattern detection
- HTML structure signatures
- URL pattern analysis
- JavaScript framework detection
- File path probing
- Version detection
- Plugin/extension detection
- Custom CMS support
- Performance optimizations with compiled regex
- Early termination for efficiency
- Comprehensive error handling
"""

import json
import re
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any, Union, Set, Sequence
from urllib.parse import urlparse, urljoin
from dataclasses import dataclass
from functools import lru_cache
import time
from collections import Counter

logger = logging.getLogger(__name__)

from utils.helpers import looks_like_guard_html

DOMAIN_HINTS: Dict[str, Tuple[str, float]] = {
    "6wool.ru": ("sixwool", 0.3),
    "mpyarn.ru": ("cm3", 1.05),
    "initki.ru": ("cscart", 1.05),
    "triskeli.ru": ("insales", 1.05),
    "ili-ili.com": ("bitrix", 1.05),
}


@dataclass
class CMSDetectionResult:
    """Result of CMS detection with confidence and metadata."""

    cms_type: Optional[str]
    confidence: float
    detection_methods: List[str]
    version: Optional[str] = None
    plugins: Optional[List[str]] = None
    extensions: Optional[List[str]] = None
    custom_patterns: Optional[Dict[str, Any]] = None
    detection_time: float = 0.0
    error: Optional[str] = None

    def __post_init__(self):
        if self.plugins is None:
            self.plugins = []
        if self.extensions is None:
            self.extensions = []
        if self.custom_patterns is None:
            self.custom_patterns = {}


@dataclass
class CMSConfig:
    """Configuration for CMS detection."""

    enable_version_detection: bool = True
    enable_plugin_detection: bool = True
    enable_file_probing: bool = True
    confidence_threshold: float = 0.6
    max_detection_time: float = 30.0
    custom_cms_patterns: Optional[Dict[str, Dict[str, Any]]] = None
    detection_methods: Optional[Dict[str, Dict[str, Any]]] = None
    method_weights: Optional[Dict[str, float]] = None

    def __post_init__(self):
        if self.custom_cms_patterns is None:
            self.custom_cms_patterns = {}
        if self.detection_methods is None:
            self.detection_methods = {}
        if self.method_weights is None:
            self.method_weights = {}


class CMSDetectionError(Exception):
    """Exception raised for CMS detection errors."""

    pass


class CMSDetection:
    """
    Advanced CMS detection system with comprehensive pattern matching.

    Supports detection of:
    - WordPress (including WooCommerce)
    - Joomla
    - Magento
    - Shopify
    - Bitrix
    - OpenCart
    - Drupal
    - PrestaShop
    - Squarespace
    - Wix
    - Custom CMS platforms
    """

    def __init__(self, config: Optional[CMSConfig] = None):
        self.config = config or CMSConfig()
        self._compiled_patterns = {}
        self._detection_cache: Dict[str, CMSDetectionResult] = {}
        self._initialize_patterns()
        self._domain_hints = DOMAIN_HINTS.copy()

        try:
            settings_text = Path("config/settings.json").read_text(encoding="utf-8")
            settings_data = json.loads(settings_text)
        except FileNotFoundError:
            settings_data = {}
        except json.JSONDecodeError as exc:
            logger.warning(
                "Failed to parse config/settings.json for CMS overrides: %s",
                exc,
            )
            settings_data = {}

        for domain, payload in settings_data.get("cms_detection", {}).get("domain_overrides", {}).items():
            if not isinstance(payload, dict):
                continue
            force = payload.get("force")
            floor = payload.get("confidence_floor")
            if force:
                score = float(floor) if isinstance(floor, (int, float)) else 1.05
                score = max(score, 1.05)
                self._domain_hints[domain.lower()] = (force, score)

    def _initialize_patterns(self) -> None:
        """Initialize compiled regex patterns for performance."""
        if self.config.detection_methods:
            # Build patterns from settings
            self.cms_patterns = self._build_patterns_from_settings()
            # Set method weights from settings
            self._set_method_weights_from_settings()
        else:
            # Use hardcoded patterns
            self.cms_patterns = self._get_hardcoded_patterns()

        # Add custom CMS patterns from config
        if self.config.custom_cms_patterns:
            self.cms_patterns.update(self.config.custom_cms_patterns)

        # Compile all patterns for performance
        for cms, patterns in self.cms_patterns.items():
            self._compiled_patterns[cms] = patterns

    def _build_patterns_from_settings(self) -> Dict[str, Dict[str, Any]]:
        """Build CMS patterns from settings.json detection_methods."""
        cms_patterns: Dict[str, Dict[str, Any]] = {}
        detection_methods = self.config.detection_methods or {}

        if not isinstance(detection_methods, dict) or not detection_methods:
            logger.warning("cms detection_methods configuration must be a mapping; falling back to defaults")
            return cms_patterns

        sanitised_methods: Dict[str, Dict[str, Any]] = {}
        for method_name, method_config in detection_methods.items():
            if isinstance(method_config, dict):
                sanitised_methods[method_name] = method_config
            else:
                logger.warning("Ignoring detection method '%s' with invalid configuration type %s", method_name, type(method_config).__name__)

        detection_methods = sanitised_methods

        if not detection_methods:
            return cms_patterns

        # Get all CMS names from all methods
        all_cms: Set[str] = set()
        for method_config in detection_methods.values():
            if not isinstance(method_config, dict):
                continue
            indicators = method_config.get("indicators")
            if isinstance(indicators, dict):
                all_cms.update(indicators.keys())
            patterns = method_config.get("patterns")
            if isinstance(patterns, dict):
                all_cms.update(patterns.keys())
            selectors = method_config.get("selectors")
            if isinstance(selectors, dict):
                all_cms.update(selectors.keys())

        for cms in all_cms:
            cms_patterns[cms] = {
                "meta_tags": [],
                "html_patterns": [],
                "url_patterns": [],
                "js_patterns": [],
                "file_paths": [],
                "version_patterns": [],
                "plugin_patterns": [],
                "extension_patterns": [],
                "app_patterns": [],
                "module_patterns": [],
            }

            # Build meta_tags from html_meta indicators
            indicators = self._normalise_string_list(
                self._safe_get_nested(detection_methods, ("html_meta", "indicators", cms)),
                f"html_meta.indicators.{cms}",
            )
            for indicator in indicators:
                cms_patterns[cms]["meta_tags"].append(
                    re.compile(re.escape(indicator), re.IGNORECASE)
                )

            # Build html_patterns from css_selectors selectors
            selectors = self._normalise_string_list(
                self._safe_get_nested(detection_methods, ("css_selectors", "selectors", cms)),
                f"css_selectors.selectors.{cms}",
            )
            for selector in selectors:
                cms_patterns[cms]["html_patterns"].append(
                    re.compile(re.escape(selector), re.IGNORECASE)
                )

            # Build url_patterns from url_patterns patterns
            url_patterns = self._normalise_string_list(
                self._safe_get_nested(detection_methods, ("url_patterns", "patterns", cms)),
                f"url_patterns.patterns.{cms}",
            )
            for pattern in url_patterns:
                cms_patterns[cms]["url_patterns"].append(
                    re.compile(re.escape(pattern), re.IGNORECASE)
                )

        return cms_patterns

    def _set_method_weights_from_settings(self) -> None:
        """Set method weights from settings.json detection_methods."""
        if not self.config.detection_methods:
            return

        method_weights = {}
        detection_methods = self.config.detection_methods

        # Map settings methods to internal method names
        method_mapping = {
            "html_meta": "meta_tags",
            "url_patterns": "url_patterns",
            "css_selectors": "html_patterns",
        }

        for settings_method, config in detection_methods.items():
            if settings_method in method_mapping and "weight" in config:
                internal_method = method_mapping[settings_method]
                method_weights[internal_method] = config["weight"]

        # Set default weights for methods not in settings
        default_weights = {
            "js_frameworks": 0.8,
            "file_paths": 0.6,
        }
        for method, weight in default_weights.items():
            if method not in method_weights:
                method_weights[method] = weight

        self.config.method_weights = method_weights

    def _safe_get_nested(self, container: Any, path: Sequence[str]) -> Any:
        current = container
        for key in path:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
        return current

    def _normalise_string_list(self, values: Any, context: str) -> List[str]:
        if values is None:
            return []
        if not isinstance(values, (list, tuple, set)):
            logger.warning("Expected list of strings for %s, got %s", context, type(values).__name__)
            return []
        cleaned: List[str] = []
        for value in values:
            if isinstance(value, str) and value.strip():
                cleaned.append(value.strip())
            else:
                logger.warning("Ignoring non-string value in %s: %r", context, value)
        return cleaned

    def _validate_selector_profile(
        self, profile: Any, context: str
    ) -> Optional[Dict[str, List[str]]]:
        if profile is None:
            return None
        if not isinstance(profile, dict):
            logger.warning("Expected dict for %s, got %s", context, type(profile).__name__)
            return None

        validated: Dict[str, List[str]] = {}
        for key, value in profile.items():
            validated[key] = self._normalise_string_list(value, f"{context}.{key}")
        return validated

    def _get_hardcoded_patterns(self) -> Dict[str, Dict[str, Any]]:
        """Get hardcoded CMS patterns."""
        return {
            "wordpress": {
                "meta_tags": [
                    re.compile(r"wordpress", re.IGNORECASE),
                    re.compile(r"wp-", re.IGNORECASE),
                    re.compile(r"woocommerce", re.IGNORECASE),
                ],
                "html_patterns": [
                    re.compile(r"wp-content", re.IGNORECASE),
                    re.compile(r"wp-includes", re.IGNORECASE),
                    re.compile(r'class=["\']wp-', re.IGNORECASE),
                    re.compile(r'id=["\']wp-', re.IGNORECASE),
                ],
                "url_patterns": [
                    re.compile(r"/wp-admin/", re.IGNORECASE),
                    re.compile(r"/wp-content/", re.IGNORECASE),
                    re.compile(r"/wp-includes/", re.IGNORECASE),
                    re.compile(r"/wp-json/", re.IGNORECASE),
                ],
                "js_patterns": [
                    re.compile(
                        r'typeof\s+wp\s*!==?\s*["\']undefined["\']', re.IGNORECASE
                    ),
                    re.compile(r"wp\.", re.IGNORECASE),
                ],
                "file_paths": [
                    "/wp-admin/install.php",
                    "/wp-login.php",
                    "/wp-content/themes/",
                    "/wp-includes/js/jquery/jquery.js",
                    "/wp-json/wp/v2/",
                ],
                "version_patterns": [
                    re.compile(r"wordpress\s*([\d.]+)", re.IGNORECASE),
                    re.compile(r"version:\s*([\d.]+)", re.IGNORECASE),
                ],
                "plugin_patterns": [
                    re.compile(r"wp-content/plugins/([^/]+)", re.IGNORECASE),
                    re.compile(r"woocommerce", re.IGNORECASE),
                    re.compile(r"contact-form-7", re.IGNORECASE),
                    re.compile(r"yootheme", re.IGNORECASE),
                ],
            },
            "joomla": {
                "meta_tags": [
                    re.compile(r"joomla", re.IGNORECASE),
                    re.compile(r"com_virtuemart", re.IGNORECASE),
                    re.compile(r"virtuemart", re.IGNORECASE),
                    re.compile(r"jdoc", re.IGNORECASE),
                ],
                "html_patterns": [
                    re.compile(r"joomla", re.IGNORECASE),
                    re.compile(r"com_content", re.IGNORECASE),
                    re.compile(r'class=["\']mod-', re.IGNORECASE),
                    re.compile(r'id=["\']mod-', re.IGNORECASE),
                ],
                "url_patterns": [
                    re.compile(r"/administrator/", re.IGNORECASE),
                    re.compile(r"/components/", re.IGNORECASE),
                    re.compile(r"/modules/", re.IGNORECASE),
                    re.compile(r"/index\.php\?option=", re.IGNORECASE),
                ],
                "js_patterns": [
                    re.compile(
                        r'typeof\s+Joomla\s*!==?\s*["\']undefined["\']', re.IGNORECASE
                    ),
                    re.compile(r"Joomla\.", re.IGNORECASE),
                ],
                "file_paths": [
                    "/administrator/manifests/files/joomla.xml",
                    "/administrator/",
                    "/components/",
                    "/modules/",
                    "/plugins/",
                ],
                "version_patterns": [
                    re.compile(r"joomla[\s!]*([\d.]+)", re.IGNORECASE),
                    re.compile(r"version[\s:]*([\d.]+)", re.IGNORECASE),
                ],
                "extension_patterns": [
                    re.compile(r"com_([^/]+)", re.IGNORECASE),
                    re.compile(r"mod_([^/]+)", re.IGNORECASE),
                    re.compile(r"virtuemart", re.IGNORECASE),
                ],
            },
            "magento": {
                "meta_tags": [
                    re.compile(r"magento", re.IGNORECASE),
                    re.compile(r"mage", re.IGNORECASE),
                ],
                "html_patterns": [
                    re.compile(r"magento", re.IGNORECASE),
                    re.compile(r"var\s+formkey", re.IGNORECASE),
                    re.compile(r'class=["\']product-', re.IGNORECASE),
                    re.compile(r"data-mage", re.IGNORECASE),
                ],
                "url_patterns": [
                    re.compile(r"/media/", re.IGNORECASE),
                    re.compile(r"/skin/", re.IGNORECASE),
                    re.compile(r"/js/", re.IGNORECASE),
                    re.compile(r"/index\.php/", re.IGNORECASE),
                ],
                "js_patterns": [
                    re.compile(
                        r'typeof\s+Mage\s*!==?\s*["\']undefined["\']', re.IGNORECASE
                    ),
                    re.compile(r"Mage\.", re.IGNORECASE),
                ],
                "file_paths": [
                    "/app/etc/local.xml",
                    "/skin/frontend/",
                    "/media/catalog/",
                    "/js/varien/",
                    "/var/cache/",
                ],
                "version_patterns": [
                    re.compile(r"magento\s*([\d.]+)", re.IGNORECASE),
                    re.compile(r"version[\s:]*([\d.]+)", re.IGNORECASE),
                ],
                "extension_patterns": [
                    re.compile(r"community/([^/]+)", re.IGNORECASE),
                    re.compile(r"local/([^/]+)", re.IGNORECASE),
                ],
            },
            "shopify": {
                "meta_tags": [
                    re.compile(r"shopify", re.IGNORECASE),
                    re.compile(r"myshopify", re.IGNORECASE),
                ],
                "html_patterns": [
                    re.compile(r"shopify", re.IGNORECASE),
                    re.compile(r"cdn\.shopify\.com", re.IGNORECASE),
                    re.compile(r'class=["\']shopify-', re.IGNORECASE),
                    re.compile(r"data-shopify", re.IGNORECASE),
                ],
                "url_patterns": [
                    re.compile(r"myshopify\.com", re.IGNORECASE),
                    re.compile(r"/collections/", re.IGNORECASE),
                    re.compile(r"/products/", re.IGNORECASE),
                    re.compile(r"/cart", re.IGNORECASE),
                ],
                "js_patterns": [
                    re.compile(
                        r'typeof\s+Shopify\s*!==?\s*["\']undefined["\']', re.IGNORECASE
                    ),
                    re.compile(r"Shopify\.", re.IGNORECASE),
                ],
                "file_paths": [
                    "/admin/",
                    "/collections/",
                    "/products/",
                    "/cart",
                    "/checkout",
                ],
                "version_patterns": [
                    re.compile(r"shopify\s*theme\s*([\d.]+)", re.IGNORECASE),
                ],
                "app_patterns": [
                    re.compile(r"shopify\.apps/([^/]+)", re.IGNORECASE),
                ],
            },
            "bitrix": {
                "meta_tags": [
                    re.compile(r"bitrix", re.IGNORECASE),
                    re.compile(r"b24", re.IGNORECASE),
                    re.compile(r"bx:component", re.IGNORECASE),
                ],
                "html_patterns": [
                    re.compile(r"bitrix", re.IGNORECASE),
                    re.compile(r"bx-", re.IGNORECASE),
                    re.compile(r"/bitrix/", re.IGNORECASE),
                    re.compile(r"JCCatalogElement", re.IGNORECASE),
                    re.compile(r"data-bx-id", re.IGNORECASE),
                ],
                "url_patterns": [
                    re.compile(r"/bitrix/", re.IGNORECASE),
                    re.compile(r"/upload/", re.IGNORECASE),
                    re.compile(r"iblock", re.IGNORECASE),
                    re.compile(r"/local/", re.IGNORECASE),
                    re.compile(r"/ajax/", re.IGNORECASE),
                ],
                "js_patterns": [
                    re.compile(
                        r'typeof\s+BX\s*!==?\s*["\']undefined["\']', re.IGNORECASE
                    ),
                    re.compile(r"BX\.", re.IGNORECASE),
                    re.compile(r"window\.JCCatalogElement", re.IGNORECASE),
                    re.compile(r"BX\.message", re.IGNORECASE),
                ],
                "file_paths": [
                    "/bitrix/admin/",
                    "/upload/iblock/",
                    "/bitrix/php_interface/",
                    "/bitrix/components/",
                    "/local/components/",
                    "/local/templates/",
                ],
                "version_patterns": [
                    re.compile(r"bitrix\s*([\d.]+)", re.IGNORECASE),
                ],
                "module_patterns": [
                    re.compile(r"/bitrix/modules/([^/]+)", re.IGNORECASE),
                    re.compile(r"/local/components/([^/]+)", re.IGNORECASE),
                ],
                "variations": {
                    "selectors": [
                        ".product-item-detail-properties",
                        ".sku-props",
                        ".product-offers",
                        ".bx_catalog_item_scu",
                        ".product-item-scu",
                        "#bx-component-scope",
                    ],
                    "attributes": [
                        ".product-item-detail-properties select",
                        "select[name^='sku']",
                        "select[name^='PROP']",
                        ".product-offers select",
                        ".sku-item select",
                        "#bx-component-scope select",
                    ],
                    "swatches": [
                        ".product-item-scu-item",
                        ".bx-item-detail-scu-item",
                        ".sku-props__item",
                        "[data-entity='sku-line-block'] .sku-line-list-item",
                    ],
                    "price_update": [
                        ".product-item-detail-price-current",
                        ".catalog-item-price",
                        ".bx-price",
                        "#bx-component-scope [data-entity='price']",
                    ],
                    "stock_update": [
                        ".product-item-detail-quantity",
                        ".bx-availability",
                        ".product-availability",
                        "#bx-component-scope [data-entity='quantity-block']",
                    ],
                    "json_data": [
                        "script:contains('offers')",
                        "script:contains('offersData')",
                        "script:contains('OFFER_DATA')",
                        "script:contains('skuProps')",
                        "script:contains('JCCatalogElement')",
                    ],
                },
            },
            "sixwool": {
                "meta_tags": [
                    re.compile(r"6wool", re.IGNORECASE),
                    re.compile(r"jawoll", re.IGNORECASE),
                    re.compile(r"bm_custom_theme", re.IGNORECASE),
                ],
                "html_patterns": [
                    re.compile(r"6wool", re.IGNORECASE),
                    re.compile(r"data-sixwool", re.IGNORECASE),
                    re.compile(r"sixwool-variation", re.IGNORECASE),
                    re.compile(r"ddos-guard", re.IGNORECASE),
                    re.compile(r"JCCatalogElement", re.IGNORECASE),
                ],
                "url_patterns": [
                    re.compile(r"6wool\\.ru", re.IGNORECASE),
                    re.compile(r"/local/templates/6wool", re.IGNORECASE),
                    re.compile(r"/ajax/variation", re.IGNORECASE),
                    re.compile(r"/ajax/catalog", re.IGNORECASE),
                ],
                "js_patterns": [
                    re.compile(r"window\.sixwool", re.IGNORECASE),
                    re.compile(r"sixwoolOffers", re.IGNORECASE),
                    re.compile(r"BX\.", re.IGNORECASE),
                    re.compile(r"JCCatalogElement", re.IGNORECASE),
                ],
                "file_paths": [
                    "/local/templates/6wool/",
                    "/ajax/catalog/",
                    "/local/components/bitrix/",
                ],
                "version_patterns": [
                    re.compile(r"sixwool\s*([\d.]+)", re.IGNORECASE),
                ],
                "module_patterns": [
                    re.compile(r"sixwool", re.IGNORECASE),
                    re.compile(r"/local/components/bitrix/([^/]+)", re.IGNORECASE),
                ],
                "variations": {
                    "selectors": [
                        ".product-detail",
                        "#bx-component-scope",
                        "[data-sixwool-variation]",
                        ".product-info",
                    ],
                    "attributes": [
                        "[data-sixwool-variation] select",
                        "[data-sixwool-variation] input[type=radio]",
                        "#bx-component-scope select",
                        ".product-detail select",
                        "[data-entity='sku-line-block'] select",
                    ],
                    "swatches": [
                        "[data-sixwool-swatch]",
                        ".sixwool-swatch",
                        ".product-detail__swatch",
                        "[data-entity='sku-line-block'] .sku-line-list-item",
                    ],
                    "price_update": [
                        "#bx-component-scope [data-entity='price']",
                        "[data-sixwool-price]",
                        ".product-detail-price",
                        ".product-price .current-price",
                    ],
                    "stock_update": [
                        "[data-sixwool-stock]",
                        ".product-detail-stock",
                        ".product-availability",
                        "[data-entity='quantity-block']",
                    ],
                    "json_data": [
                        "script[data-sixwool-json]",
                        "script:contains('sixwoolOffers')",
                        "script:contains('JCCatalogElement')",
                    ],
                },
                "confidence_weight": 0.92,
                "fallback_selectors": {
                    "name": [
                        "h1",
                        ".product-detail-title",
                        ".product-title",
                    ],
                    "price": [
                        ".product-price",
                        "[itemprop='price']",
                        ".current-price",
                    ],
                    "stock": [
                        ".product-availability",
                        ".product-detail-stock",
                        "[data-entity='quantity-block']",
                    ],
                    "variations": [
                        "select[name*='OFFER']",
                        "[data-entity='sku-line-block'] select",
                        "[data-sixwool-variation]",
                    ],
                },
                "wait_for_selectors": [
                    "#bx-component-scope",
                    "[data-sixwool-variation]",
                    ".product-detail",
                ],
                "playwright_wait_states": ["networkidle", "load"],
                "api_support": True,
            },
            "insales": {
                "meta_tags": [
                    re.compile(r"insales", re.IGNORECASE),
                ],
                "html_patterns": [
                    re.compile(r"insales", re.IGNORECASE),
                    re.compile(r"data-product", re.IGNORECASE),
                    re.compile(r"/collection/", re.IGNORECASE),
                    re.compile(r"InSales", re.IGNORECASE),
                ],
                "url_patterns": [
                    re.compile(r"/collection", re.IGNORECASE),
                    re.compile(r"/product", re.IGNORECASE),
                    re.compile(r"/products\.json", re.IGNORECASE),
                ],
                "js_patterns": [
                    re.compile(r"Insales", re.IGNORECASE),
                    re.compile(r"InSales", re.IGNORECASE),
                    re.compile(r"InsalesApp", re.IGNORECASE),
                ],
                "file_paths": [
                    "/admin/",
                    "/collections/",
                    "/products.json",
                ],
                "variations": {
                    "selectors": [
                        ".product-variants",
                        ".variant-options",
                        ".variants-list",
                        "[data-variants]",
                        ".js-product-variants",
                    ],
                    "attributes": [
                        "select[name^='option']",
                        "select[name^='variant']",
                        "[data-variant-select]",
                        "[data-option-select]",
                    ],
                    "swatches": [
                        ".variant-option",
                        ".option-values",
                        ".product-variants__item",
                    ],
                    "price_update": [
                        "[data-price]",
                        ".js-product-price",
                        ".product-price",
                    ],
                    "stock_update": [
                        "[data-stock]",
                        ".product-availability",
                        ".js-variant-stock",
                    ],
                    "json_data": [
                        "script:contains('variants')",
                        "script:contains('InSales')",
                        "[data-product]",
                        "[data-variants]",
                        "script[type='application/json']",
                    ],
                },
            },
            "cm3": {
                "meta_tags": [
                    re.compile(r"cm3", re.IGNORECASE),
                    re.compile(r"cmshop", re.IGNORECASE),
                    re.compile(r"generator\s*[:=]\s*[\"']?cm3", re.IGNORECASE),
                ],
                "html_patterns": [
                    re.compile(r"cm3-", re.IGNORECASE),
                    re.compile(r"cmshop", re.IGNORECASE),
                    re.compile(r"class=[\"\']cm3-", re.IGNORECASE),
                    re.compile(r"id=[\"\']cm3-", re.IGNORECASE),
                ],
                "url_patterns": [
                    re.compile(r"/cm3/", re.IGNORECASE),
                    re.compile(r"/cmshop/", re.IGNORECASE),
                    re.compile(r"cm3=", re.IGNORECASE),
                ],
                "js_patterns": [
                    re.compile(r"CM3\.", re.IGNORECASE),
                    re.compile(r"cmshop", re.IGNORECASE),
                    re.compile(r"window\.CM3", re.IGNORECASE),
                ],
                "file_paths": [
                    "/cm3/admin/",
                    "/cmshop/",
                    "/cm3/templates/",
                ],
                "version_patterns": [
                    re.compile(r"cm3\s*([\d.]+)", re.IGNORECASE),
                    re.compile(r"cmshop\s*([\d.]+)", re.IGNORECASE),
                ],
                "variations": {
                    "selectors": [
                        ".product-options select",
                        ".cm3-variants",
                        ".product-variants select",
                    ],
                    "attributes": [
                        "select[name^='option']",
                        "select[name^='variant']",
                    ],
                    "swatches": [
                        ".cm3-variant-item",
                        ".cm3-swatch",
                    ],
                    "price_update": [
                        ".cm3-price",
                        ".product-price",
                    ],
                    "stock_update": [
                        ".cm3-stock",
                        ".product-stock",
                    ],
                    "json_data": [
                        "script:contains('CM3ProductData')",
                        "script:contains('cm3Variants')",
                    ],
                },
            },
            "cscart": {
                "meta_tags": [
                    re.compile(r"cs-cart", re.IGNORECASE),
                    re.compile(r"cscart", re.IGNORECASE),
                    re.compile(r"tygh", re.IGNORECASE),
                ],
                "html_patterns": [
                    re.compile(r"ty-", re.IGNORECASE),
                    re.compile(r"cs-cart", re.IGNORECASE),
                    re.compile(r"data-ca-product-id", re.IGNORECASE),
                    re.compile(r"cm-picker", re.IGNORECASE),
                ],
                "url_patterns": [
                    re.compile(r"/dispatch=", re.IGNORECASE),
                    re.compile(r"index\.php\?dispatch=", re.IGNORECASE),
                    re.compile(r"/app/", re.IGNORECASE),
                ],
                "js_patterns": [
                    re.compile(r"Tygh", re.IGNORECASE),
                    re.compile(r"csCart", re.IGNORECASE),
                    re.compile(r"Tygh\.", re.IGNORECASE),
                ],
                "file_paths": [
                    "/admin.php",
                    "/var/cache/",
                    "/design/themes/",
                    "/app/functions/fn.common.php",
                ],
                "version_patterns": [
                    re.compile(r"cs-cart\s*([\d.]+)", re.IGNORECASE),
                    re.compile(r"cscart\s*([\d.]+)", re.IGNORECASE),
                ],
                "variations": {
                    "selectors": [
                        ".ty-product-options select",
                        ".cm-picker-variant",
                        ".ty-variants select",
                    ],
                    "attributes": [
                        "select[name^='product_data']",
                        "select[name^='option_']",
                        "[data-ca-product-id] select",
                    ],
                    "swatches": [
                        ".ty-product-options__item",
                        ".ty-swatches__item",
                        ".cm-picker-option",
                    ],
                    "price_update": [
                        ".ty-price",
                        ".ty-price-num",
                        ".product-price",
                    ],
                    "stock_update": [
                        ".ty-qty-in-stock",
                        ".ty-product-availability",
                        ".availability",
                    ],
                    "json_data": [
                        "script:contains('Tygh')",
                        "script:contains('product_options')",
                        "script:contains('option_combinations')",
                    ],
                },
            },
            "opencart": {
                "meta_tags": [
                    re.compile(r"opencart", re.IGNORECASE),
                ],
                "html_patterns": [
                    re.compile(r"opencart", re.IGNORECASE),
                    re.compile(r"route=product", re.IGNORECASE),
                    re.compile(r"index\.php\?route=", re.IGNORECASE),
                ],
                "url_patterns": [
                    re.compile(r"route=product", re.IGNORECASE),
                    re.compile(r"/index\.php\?route=", re.IGNORECASE),
                    re.compile(r"/catalog/", re.IGNORECASE),
                ],
                "js_patterns": [
                    re.compile(
                        r'typeof\s+opencart\s*!==?\s*["\']undefined["\']', re.IGNORECASE
                    ),
                ],
                "file_paths": [
                    "/admin/",
                    "/catalog/",
                    "/system/library/",
                    "/vqmod/",
                ],
                "version_patterns": [
                    re.compile(r"opencart\s*([\d.]+)", re.IGNORECASE),
                ],
                "extension_patterns": [
                    re.compile(r"/vqmod/vqcache/([^/]+)", re.IGNORECASE),
                ],
            },
            "drupal": {
                "meta_tags": [
                    re.compile(r"drupal", re.IGNORECASE),
                ],
                "html_patterns": [
                    re.compile(r"drupal", re.IGNORECASE),
                    re.compile(r"node-", re.IGNORECASE),
                    re.compile(r"taxonomy", re.IGNORECASE),
                ],
                "url_patterns": [
                    re.compile(r"/node/", re.IGNORECASE),
                    re.compile(r"/user/", re.IGNORECASE),
                    re.compile(r"/admin/", re.IGNORECASE),
                    re.compile(r"/sites/default/", re.IGNORECASE),
                ],
                "js_patterns": [
                    re.compile(
                        r'typeof\s+Drupal\s*!==?\s*["\']undefined["\']', re.IGNORECASE
                    ),
                    re.compile(r"Drupal\.", re.IGNORECASE),
                ],
                "file_paths": [
                    "/user/login",
                    "/admin/",
                    "/sites/default/",
                    "/modules/",
                    "/themes/",
                ],
                "version_patterns": [
                    re.compile(r"drupal\s*([\d.]+)", re.IGNORECASE),
                ],
                "module_patterns": [
                    re.compile(r"/modules/([^/]+)", re.IGNORECASE),
                ],
            },
            "prestashop": {
                "meta_tags": [
                    re.compile(r"prestashop", re.IGNORECASE),
                    re.compile(r"presta", re.IGNORECASE),
                ],
                "html_patterns": [
                    re.compile(r"prestashop", re.IGNORECASE),
                    re.compile(r"id_product", re.IGNORECASE),
                    re.compile(r"ps_", re.IGNORECASE),
                ],
                "url_patterns": [
                    re.compile(r"/product\.php", re.IGNORECASE),
                    re.compile(r"/category\.php", re.IGNORECASE),
                    re.compile(r"/index\.php\?id_product=", re.IGNORECASE),
                ],
                "js_patterns": [
                    re.compile(
                        r'typeof\s+prestashop\s*!==?\s*["\']undefined["\']',
                        re.IGNORECASE,
                    ),
                    re.compile(r"prestashop\.", re.IGNORECASE),
                ],
                "file_paths": [
                    "/admin/",
                    "/modules/",
                    "/themes/",
                    "/override/",
                ],
                "version_patterns": [
                    re.compile(r"prestashop\s*([\d.]+)", re.IGNORECASE),
                ],
                "module_patterns": [
                    re.compile(r"/modules/([^/]+)", re.IGNORECASE),
                ],
            },
            "squarespace": {
                "meta_tags": [
                    re.compile(r"squarespace", re.IGNORECASE),
                ],
                "html_patterns": [
                    re.compile(r"squarespace", re.IGNORECASE),
                    re.compile(r"squarespace\.com", re.IGNORECASE),
                    re.compile(r"data-image", re.IGNORECASE),
                ],
                "url_patterns": [
                    re.compile(r"squarespace\.com", re.IGNORECASE),
                ],
                "js_patterns": [
                    re.compile(r"Squarespace", re.IGNORECASE),
                ],
                "file_paths": [
                    "/config",
                    "/assets/",
                ],
                "version_patterns": [
                    re.compile(r"squarespace\s*([\d.]+)", re.IGNORECASE),
                ],
            },
            "wix": {
                "meta_tags": [
                    re.compile(r"wix", re.IGNORECASE),
                ],
                "html_patterns": [
                    re.compile(r"wix", re.IGNORECASE),
                    re.compile(r"wix\.com", re.IGNORECASE),
                    re.compile(r"data-wix", re.IGNORECASE),
                ],
                "url_patterns": [
                    re.compile(r"wix\.com", re.IGNORECASE),
                ],
                "js_patterns": [
                    re.compile(r"Wix", re.IGNORECASE),
                ],
                "file_paths": [
                    "/_partials/",
                    "/pro-gallery/",
                ],
                "version_patterns": [
                    re.compile(r"wix\s*([\d.]+)", re.IGNORECASE),
                ],
            },
        }

    def detect_cms_by_patterns(
        self,
        url: Optional[str] = None,
        html: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        scripts: Optional[List[str]] = None,
    ) -> CMSDetectionResult:
        """
        Detect CMS using comprehensive pattern matching.

        Args:
            url: Optional URL to analyze
            html: Optional HTML content to analyze
            headers: Optional HTTP headers
            scripts: Optional list of script sources

        Returns:
            CMSDetectionResult with detection details
        """
        start_time = time.time()
        cache_key = self._generate_cache_key(url, html)

        # Check cache first
        if cache_key in self._detection_cache:
            cached = self._detection_cache[cache_key]
            if time.time() - cached.detection_time < self.config.max_detection_time:
                return cached

        try:
            detection_scores: Dict[str, float] = {}
            detection_methods: List[str] = []
            all_plugins = []
            all_extensions = []

            domain = ""
            domain_hint_data: Optional[Tuple[str, float]] = None
            if url:
                try:
                    domain = urlparse(url).netloc.lower()
                except Exception:  # noqa: BLE001
                    domain = ""
                if domain.startswith("www."):
                    domain = domain[4:]
                domain_hint_data = self._domain_hints.get(domain)
                if not domain_hint_data and domain:
                    if domain.startswith("www."):
                        domain_hint_data = self._domain_hints.get(domain[4:])

            if domain_hint_data:
                hint_cms, hint_score = domain_hint_data
                if domain:
                    detection_methods.append(f"domain_hint:{domain}")
                detection_scores[hint_cms] = max(
                    detection_scores.get(hint_cms, 0.0), hint_score
                )

            # Method 1: Meta tag detection
            if html:
                meta_result = self._detect_by_meta_tags(html)
                if meta_result:
                    cms, score, methods = meta_result
                    weight = (
                        self.config.method_weights.get("meta_tags", 1.0)
                        if self.config.method_weights
                        else 1.0
                    )
                    detection_scores[cms] = (
                        detection_scores.get(cms, 0) + score * weight
                    )
                    detection_methods.extend(methods)

            # Method 2: HTML pattern detection
            if html:
                html_result = self._detect_by_html_patterns(html)
                if html_result:
                    cms, score, methods = html_result
                    weight = (
                        self.config.method_weights.get("html_patterns", 1.0)
                        if self.config.method_weights
                        else 1.0
                    )
                    detection_scores[cms] = (
                        detection_scores.get(cms, 0) + score * weight
                    )
                    detection_methods.extend(methods)

            # Method 3: URL pattern detection
            if url:
                url_result = self._detect_by_url_patterns(url)
                if url_result:
                    cms, score, methods = url_result
                    weight = (
                        self.config.method_weights.get("url_patterns", 1.0)
                        if self.config.method_weights
                        else 1.0
                    )
                    detection_scores[cms] = (
                        detection_scores.get(cms, 0) + score * weight
                    )
                    detection_methods.extend(methods)

            # Method 4: JavaScript framework detection
            if html or scripts:
                js_result = self._detect_by_js_frameworks(html, scripts)
                if js_result:
                    cms, score, methods = js_result
                    weight = (
                        self.config.method_weights.get("js_frameworks", 1.0)
                        if self.config.method_weights
                        else 1.0
                    )
                    detection_scores[cms] = (
                        detection_scores.get(cms, 0) + score * weight
                    )
                    detection_methods.extend(methods)

            # Method 5: File path probing (if enabled)
            if self.config.enable_file_probing and url:
                file_result = self._detect_by_file_paths(url)
                if file_result:
                    cms, score, methods = file_result
                    weight = (
                        self.config.method_weights.get("file_paths", 1.0)
                        if self.config.method_weights
                        else 1.0
                    )
                    detection_scores[cms] = (
                        detection_scores.get(cms, 0) + score * weight
                    )
                    detection_methods.extend(methods)

            if (
                html
                and domain_hint_data
                and domain_hint_data[0] == "sixwool"
                and looks_like_guard_html(html)
            ):
                detection_methods.append("ddos_guard_signature")
                detection_scores["sixwool"] = detection_scores.get("sixwool", 0) + 0.2

            # Early termination if high confidence reached
            max_score = max(detection_scores.values()) if detection_scores else 0
            if max_score >= self.config.confidence_threshold:
                best_cms = max(
                    detection_scores.keys(), key=lambda k: detection_scores[k]
                )
                if domain_hint_data and best_cms != domain_hint_data[0]:
                    hint_cms, hint_score = domain_hint_data
                    candidate_score = detection_scores.get(hint_cms, hint_score)
                    if candidate_score >= max_score * 0.6:
                        best_cms = hint_cms
                        max_score = max(max_score, candidate_score)
                confidence = min(max_score, 1.0)
                if domain_hint_data and best_cms == domain_hint_data[0]:
                    confidence = max(confidence, domain_hint_data[1])

                # Get version and plugins if enabled
                version = None
                plugins = []
                extensions = []

                if self.config.enable_version_detection and html:
                    version = self._detect_version(best_cms, html)

                if self.config.enable_plugin_detection and html:
                    plugins, extensions = self._detect_plugins_and_extensions(
                        best_cms, html, url
                    )

                result = CMSDetectionResult(
                    cms_type=best_cms,
                    confidence=confidence,
                    detection_methods=list(set(detection_methods)),
                    version=version,
                    plugins=plugins,
                    extensions=extensions,
                    detection_time=time.time() - start_time,
                )

                # Cache result
                self._detection_cache[cache_key] = result
                return result

            # Apply domain-based fallback when no patterns matched
            if not detection_scores and domain_hint_data:
                hint_cms, hint_score = domain_hint_data
                detection_scores[hint_cms] = hint_score
                detection_methods.append("fallback_domain_assignment")

            # If no high confidence detection, return best guess or None
            if detection_scores:
                best_cms = max(
                    detection_scores.keys(), key=lambda k: detection_scores[k]
                )
                if domain_hint_data and best_cms != domain_hint_data[0]:
                    hint_cms, hint_score = domain_hint_data
                    top_score = detection_scores.get(best_cms, 0.0)
                    candidate_score = detection_scores.get(hint_cms, hint_score)
                    if candidate_score and top_score and candidate_score >= top_score * 0.6:
                        best_cms = hint_cms
                confidence = min(
                    detection_scores.get(best_cms, max(detection_scores.values())),
                    1.0,
                )
                if domain_hint_data and best_cms == domain_hint_data[0]:
                    confidence = max(confidence, domain_hint_data[1])

                result = CMSDetectionResult(
                    cms_type=best_cms if confidence >= 0.3 else None,
                    confidence=confidence,
                    detection_methods=list(set(detection_methods)),
                    detection_time=time.time() - start_time,
                )
            else:
                result = CMSDetectionResult(
                    cms_type=None,
                    confidence=0.0,
                    detection_methods=[],
                    detection_time=time.time() - start_time,
                )

            # Cache result
            self._detection_cache[cache_key] = result
            return result

        except Exception as e:
            logger.error(f"CMS detection failed: {e}")
            return CMSDetectionResult(
                cms_type=None,
                confidence=0.0,
                detection_methods=[],
                detection_time=time.time() - start_time,
                error=str(e),
            )

    def calculate_detection_confidence(
        self, detection_scores: Dict[str, float], methods_used: List[str]
    ) -> float:
        """
        Calculate overall detection confidence based on scores and methods.

        Args:
            detection_scores: Dictionary of CMS -> score mappings
            methods_used: List of detection methods used

        Returns:
            Confidence score between 0.0 and 1.0
        """
        if not detection_scores:
            return 0.0

        # Base confidence from scores
        max_score = max(detection_scores.values())
        method_multiplier = min(
            len(methods_used) * 0.2, 1.0
        )  # Bonus for multiple methods

        confidence = min(max_score * method_multiplier, 1.0)

        # Adjust based on method reliability
        method_weights = {
            "meta_tags": 1.0,
            "html_patterns": 0.9,
            "url_patterns": 0.7,
            "js_frameworks": 0.8,
            "file_paths": 0.6,
        }

        weighted_sum = sum(method_weights.get(method, 0.5) for method in methods_used)
        method_confidence = min(weighted_sum / len(methods_used), 1.0)

        return min(confidence * method_confidence, 1.0)

    def validate_cms_detection(
        self, cms_type: str, url: Optional[str] = None, html: Optional[str] = None
    ) -> bool:
        """
        Validate CMS detection result.

        Args:
            cms_type: Detected CMS type
            url: Optional URL for validation
            html: Optional HTML for validation

        Returns:
            True if validation passes, False otherwise
        """
        if not cms_type or cms_type not in self.cms_patterns:
            return False

        try:
            if cms_type == "sixwool":
                if url and "6wool.ru" in url:
                    return True
                if html and "6wool" in html.lower():
                    return True

            patterns = self.cms_patterns[cms_type]
            validation_score = 0
            max_score = 0

            # Check meta tags
            if html and "meta_tags" in patterns:
                max_score += 1
                if any(pattern.search(html) for pattern in patterns["meta_tags"]):
                    validation_score += 1

            # Check HTML patterns
            if html and "html_patterns" in patterns:
                max_score += 1
                if any(pattern.search(html) for pattern in patterns["html_patterns"]):
                    validation_score += 1

            # Check URL patterns
            if url and "url_patterns" in patterns:
                max_score += 1
                if any(pattern.search(url) for pattern in patterns["url_patterns"]):
                    validation_score += 1

            # Require at least 2 out of 3 validations
            return validation_score >= 2 if max_score >= 2 else validation_score > 0

        except Exception as e:
            logger.error(f"CMS validation failed for {cms_type}: {e}")
            return False

    def get_variation_selectors(
        self, variation_type: str, cms_type: Optional[str] = None
    ) -> List[str]:
        """
        Get CMS-specific variation selectors.

        Args:
            variation_type: Type of variation selector (selectors, attributes, swatches, price_update, stock_update, json_data)
            cms_type: CMS type, if known

        Returns:
            List of CMS-specific variation selectors
        """
        if cms_type and cms_type in self.cms_patterns:
            cms_data = self.cms_patterns[cms_type]
            profile = self._validate_selector_profile(
                cms_data.get("variations"), f"{cms_type}.variations"
            )
            if profile:
                selectors = list(profile.get(variation_type, []))
                if cms_type == "sixwool":
                    fallback_profile = self._validate_selector_profile(
                        self.cms_patterns.get("bitrix", {}).get("variations"),
                        "bitrix.variations",
                    )
                    if not selectors and fallback_profile:
                        selectors = list(fallback_profile.get(variation_type, []))
                    elif fallback_profile:
                        selectors.extend(fallback_profile.get(variation_type, []))
                # Deduplicate while preserving order
                deduped: List[str] = []
                for selector in selectors:
                    if selector not in deduped:
                        deduped.append(selector)
                if cms_type == "cm3" and ".cm3-variants select" not in deduped:
                    deduped.append(".cm3-variants select")
                if cms_type == "insales" and ".js-product-variants select" not in deduped:
                    deduped.append(".js-product-variants select")
                return deduped

        # Return generic selectors if CMS-specific not found
        generic_variation_selectors = {
            "selectors": [
                ".product-options",
                ".product-variants",
                ".variations",
                ".configurable-options",
                ".product-attributes"
            ],
            "attributes": [
                "select[name*='option']",
                "select[name*='attribute']",
                "select[name*='variant']",
                "input[name*='option']",
                "input[name*='attribute']"
            ],
            "swatches": [
                ".color-swatch",
                ".size-swatch",
                ".swatch-option",
                ".variant-button",
                ".option-button"
            ],
            "price_update": [
                ".price",
                ".current-price",
                ".product-price",
                ".price-box",
                "[data-price]"
            ],
            "stock_update": [
                ".stock",
                ".availability",
                ".inventory",
                "[data-stock]",
                ".product-availability"
            ],
            "json_data": [
                "script[type='application/json']",
                "script:contains('product')",
                "script:contains('variant')",
                "script:contains('option')"
            ]
        }

        return generic_variation_selectors.get(variation_type, [])

    def get_cms_specific_selectors(
        self, field: str, cms_type: Optional[str] = None
    ) -> List[str]:
        """
        Get CMS-specific selectors for a field.

        Args:
            field: Field type (name, price, stock, etc.)
            cms_type: CMS type, if known

        Returns:
            List of CMS-specific selectors
        """
        cms_selectors = {
            "wordpress": {
                "name": [
                    "h1.entry-title",
                    ".product-title",
                    ".woocommerce-loop-product__title",
                    "h1.product_title",
                ],
                "price": [".price", ".woocommerce-Price-amount", ".amount", "p.price"],
                "stock": [
                    ".stock",
                    ".availability",
                    ".woocommerce-product-details__short-description",
                    "p.stock",
                ],
                "description": [
                    ".entry-content",
                    ".woocommerce-product-details__short-description",
                    ".product-description",
                ],
                "image": [
                    ".wp-post-image",
                    ".woocommerce-product-gallery__image img",
                    ".product-image img",
                ],
                "category": [
                    ".posted_in a",
                    ".woocommerce-breadcrumb a",
                    ".product-category a",
                ],
            },
            "woocommerce": {
                "name": ["h1.product_title", ".woocommerce-loop-product__title"],
                "price": [".woocommerce-Price-amount", "p.price", ".price"],
                "stock": [".stock", "p.stock", ".availability"],
                "description": [
                    ".woocommerce-product-details__short-description",
                    ".product-description",
                ],
                "image": [
                    ".woocommerce-product-gallery__image img",
                    ".product-image img",
                ],
                "category": [".woocommerce-breadcrumb a", ".product-category a"],
                "variations": {
                    "selectors": [
                        ".variations_form .variations",
                        ".variable-products-wrapper",
                        ".product-variants",
                        ".variation_id",
                        ".single_variation_wrap"
                    ],
                    "attributes": [
                        ".variations select",
                        ".variations .value select",
                        "select[name^='attribute_']"
                    ],
                    "swatches": [
                        ".variation-swatches",
                        ".swatch-color",
                        ".swatch-image",
                        ".color-swatch",
                        ".size-swatch"
                    ],
                    "price_update": [
                        ".woocommerce-variation-price",
                        ".single_variation .price",
                        ".price-wrapper .price",
                        ".variation-price"
                    ],
                    "stock_update": [
                        ".woocommerce-variation-availability",
                        ".single_variation .availability",
                        ".variation-stock"
                    ],
                    "json_data": [
                        "script:contains('product_variations')",
                        "script:contains('wc_product_variations')",
                        "form.variations_form[data-product_variations]"
                    ]
                },
            },
            "shopify": {
                "name": ["h1.product-single__title", ".product__title"],
                "price": [".product__price", ".price-item", "[data-price]"],
                "stock": [".product__inventory", ".availability", "[data-stock]"],
                "description": [".product__description", ".product-description"],
                "image": [".product__image img", ".product-gallery img"],
                "category": [".breadcrumb a", ".product__breadcrumb a"],
                "variations": {
                    "selectors": [
                        ".product-form__input",
                        ".variant-selects",
                        ".product-variants",
                        ".product-form__option",
                        "variant-picker"
                    ],
                    "attributes": [
                        ".product-form__input select",
                        "select[name='id']",
                        ".variant-input",
                        "fieldset.product-form__input"
                    ],
                    "swatches": [
                        ".variant-input--button",
                        ".color-input",
                        ".size-input",
                        ".variant-button",
                        "label.variant-input"
                    ],
                    "price_update": [
                        ".price__current",
                        ".product__price--current",
                        "[data-product-price]",
                        ".price-item--on-sale"
                    ],
                    "stock_update": [
                        ".product__inventory",
                        "[data-product-inventory]",
                        ".variant-inventory"
                    ],
                    "json_data": [
                        "script[data-product-json]",
                        "script:contains('product:')",
                        "script[type='application/ld+json']"
                    ]
                },
            },
            "opencart": {
                "name": ["h1.title", ".product-title"],
                "price": [".price-new", ".price"],
                "stock": [".stock-status", ".stock"],
                "description": ["#tab-description", ".product-description"],
                "image": [".product-images img", ".product-image img"],
                "category": [".breadcrumb a", ".category-link"],
                "variations": {
                    "selectors": [
                        ".product-option",
                        "#input-option",
                        ".form-group.required",
                        ".options",
                        ".product-options"
                    ],
                    "attributes": [
                        "select[name^='option']",
                        "input[name^='option']",
                        ".form-control",
                        ".option-value select"
                    ],
                    "swatches": [
                        ".option-color",
                        ".option-image",
                        ".radio-inline",
                        ".checkbox-inline",
                        "label.thumbnail"
                    ],
                    "price_update": [
                        ".price-update",
                        "#price-special",
                        "#price-old",
                        "#price-new"
                    ],
                    "stock_update": [
                        ".stock-status",
                        "#stock-status"
                    ],
                    "json_data": [
                        "script:contains('option')",
                        "script:contains('product_id')"
                    ]
                },
            },
            "magento": {
                "name": [".product-name", ".page-title", "h1"],
                "price": [".price", ".price-box", ".regular-price"],
                "stock": [".availability", ".stock", ".product-availability"],
                "description": [".product-description", ".description"],
                "image": [".product-image img", ".product-gallery img"],
                "category": [".breadcrumbs a", ".category-link"],
                "variations": {
                    "selectors": [
                        ".swatch-attribute",
                        ".super-attribute-select",
                        ".configurable-options",
                        ".product-options-wrapper",
                        ".field.configurable"
                    ],
                    "attributes": [
                        ".super-attribute-select",
                        "select.super-attribute-select",
                        ".swatch-select",
                        ".field select"
                    ],
                    "swatches": [
                        ".swatch-option",
                        ".swatch-option-color",
                        ".swatch-option-text",
                        ".color-swatch",
                        ".size-swatch"
                    ],
                    "price_update": [
                        ".price-box",
                        ".price-wrapper",
                        ".regular-price",
                        ".special-price"
                    ],
                    "stock_update": [
                        ".availability",
                        ".stock.available",
                        ".stock.unavailable"
                    ],
                    "json_data": [
                        "script:contains('spConfig')",
                        "script:contains('configurable')",
                        "script[type='text/x-magento-init']"
                    ]
                },
            },
            "prestashop": {
                "name": ["h1.product-name", ".product-title"],
                "price": [".current-price", ".price", ".product-price"],
                "stock": [".product-availability", ".availability", ".stock"],
                "description": [".product-description", "#product-description"],
                "image": [".product-images img", ".product-image img"],
                "category": [".breadcrumb a", ".category-link"],
                "variations": {
                    "selectors": [
                        ".product-variants",
                        ".product-customization",
                        ".product-features",
                        ".js-product-variants",
                        ".attribute-list"
                    ],
                    "attributes": [
                        "select[name^='group']",
                        ".attribute-list select",
                        ".product-variants select",
                        "input[name^='attribute']"
                    ],
                    "swatches": [
                        ".color-option",
                        ".texture-option",
                        ".size-option",
                        ".attribute-color",
                        "label.color"
                    ],
                    "price_update": [
                        ".current-price",
                        ".product-price",
                        ".price-display"
                    ],
                    "stock_update": [
                        ".product-availability",
                        "#product-availability",
                        ".stock-quantity"
                    ],
                    "json_data": [
                        "script:contains('combinations')",
                        "script:contains('attributes')",
                        "script:contains('prestashop')"
                    ]
                },
            },
            "joomla": {
                "name": ["h1.page-title", ".item-title", "h1"],
                "price": [".price", ".product-price"],
                "stock": [".stock", ".availability"],
                "description": [".item-description", ".product-description"],
                "image": [".item-image img", ".product-image img"],
                "category": [".category-link", ".breadcrumb a"],
            },
            "drupal": {
                "name": ["h1.page-title", ".node-title", "h1"],
                "price": [".price", ".field-price"],
                "stock": [".stock", ".availability"],
                "description": [".node-content", ".field-description"],
                "image": [".field-image img", ".node-image img"],
                "category": [".breadcrumb a", ".field-category a"],
            },
            "bitrix": {
                "name": [".bx-title", ".product-item-detail-title", "h1"],
                "price": [
                    ".bx-price",
                    ".product-item-detail-price-current",
                    ".catalog-item-price",
                ],
                "stock": [
                    ".product-item-detail-quantity",
                    ".bx-availability",
                    ".product-availability",
                ],
                "description": [
                    ".product-item-detail-description",
                    ".bx-item-detail-tab-content",
                ],
                "image": [
                    ".product-item-detail-slider-container img",
                    ".bx-slider-pager img",
                ],
                "category": [
                    ".breadcrumb a",
                    ".catalog-section-list a",
                ],
                "variations": {
                    "selectors": [
                        ".product-item-detail-properties",
                        ".sku-props",
                        ".product-offers",
                        ".bx_catalog_item_scu",
                        ".product-item-scu",
                    ],
                    "attributes": [
                        ".product-item-detail-properties select",
                        "select[name^='sku']",
                        "select[name^='PROP']",
                        ".product-offers select",
                        ".sku-item select",
                    ],
                    "swatches": [
                        ".product-item-scu-item",
                        ".bx-item-detail-scu-item",
                        ".sku-props__item",
                    ],
                    "price_update": [
                        ".product-item-detail-price-current",
                        ".catalog-item-price",
                        ".bx-price",
                    ],
                    "stock_update": [
                        ".product-item-detail-quantity",
                        ".bx-availability",
                        ".product-availability",
                    ],
                    "json_data": [
                        "script:contains('offers')",
                        "script:contains('offersData')",
                        "script:contains('OFFER_DATA')",
                        "script:contains('skuProps')",
                    ],
                },
            },
            "insales": {
                "name": [
                    ".product-title",
                    "h1[itemprop='name']",
                    ".product__title",
                ],
                "price": [
                    "[data-price]",
                    ".product-price",
                    ".js-product-price",
                ],
                "stock": [
                    "[data-stock]",
                    ".product-availability",
                    ".js-variant-stock",
                ],
                "description": [
                    ".product-description",
                    "[data-product-description]",
                ],
                "image": [
                    ".product-gallery img",
                    "[data-product-image]",
                ],
                "category": [
                    ".breadcrumbs a",
                    ".product-collections a",
                ],
                "variations": {
                    "selectors": [
                        ".product-variants",
                        ".variant-options",
                        ".variants-list",
                        "[data-variants]",
                        ".js-product-variants",
                    ],
                    "attributes": [
                        "select[name^='option']",
                        "select[name^='variant']",
                        "[data-variant-select]",
                        "[data-option-select]",
                    ],
                    "swatches": [
                        ".variant-option",
                        ".option-values",
                        ".product-variants__item",
                    ],
                    "price_update": [
                        "[data-price]",
                        ".js-product-price",
                        ".product-price",
                    ],
                    "stock_update": [
                        "[data-stock]",
                        ".product-availability",
                        ".js-variant-stock",
                    ],
                    "json_data": [
                        "script:contains('variants')",
                        "script:contains('InSales')",
                        "[data-product]",
                        "[data-variants]",
                        "script[type='application/json']",
                    ],
                },
            },
        }

        if cms_type and cms_type in cms_selectors:
            return cms_selectors[cms_type].get(field, [])
        elif cms_type == "custom" and self.config.custom_cms_patterns:
            # Check custom patterns
            for custom_cms, patterns in self.config.custom_cms_patterns.items():
                if "selectors" in patterns:
                    result = patterns["selectors"].get(field)
                    if isinstance(result, list):
                        return result
                    else:
                        return []
        else:
            # Return generic selectors
            generic_selectors = {
                "name": ["h1", ".product-title", ".item-title", '[itemprop="name"]'],
                "price": [".price", '[itemprop="price"]', ".product-price"],
                "stock": [".stock", ".availability", '[itemprop="availability"]'],
                "description": [
                    ".description",
                    ".product-description",
                    '[itemprop="description"]',
                ],
                "image": ['img[itemprop="image"]', ".product-image img"],
                "category": [".breadcrumb a", ".category-link"],
            }
            result = generic_selectors.get(field, [])
            return result if isinstance(result, list) else []

    def _detect_by_meta_tags(self, html: str) -> Optional[Tuple[str, float, List[str]]]:
        """Detect CMS by meta tags."""
        try:
            for cms, patterns in self.cms_patterns.items():
                if "meta_tags" in patterns:
                    matches = 0
                    for pattern in patterns["meta_tags"]:
                        if pattern.search(html):
                            matches += 1
                    if matches > 0:
                        confidence = min(matches / len(patterns["meta_tags"]), 1.0)
                        return cms, confidence, ["meta_tags"]
            return None
        except Exception as e:
            logger.debug(f"Meta tag detection failed: {e}")
            return None

    def _detect_by_html_patterns(
        self, html: str
    ) -> Optional[Tuple[str, float, List[str]]]:
        """Detect CMS by HTML patterns."""
        try:
            for cms, patterns in self.cms_patterns.items():
                if "html_patterns" in patterns:
                    matches = 0
                    for pattern in patterns["html_patterns"]:
                        if pattern.search(html):
                            matches += 1
                    if matches > 0:
                        confidence = min(matches / len(patterns["html_patterns"]), 1.0)
                        return cms, confidence, ["html_patterns"]
            return None
        except Exception as e:
            logger.debug(f"HTML pattern detection failed: {e}")
            return None

    def _detect_by_url_patterns(
        self, url: str
    ) -> Optional[Tuple[str, float, List[str]]]:
        """Detect CMS by URL patterns."""
        try:
            for cms, patterns in self.cms_patterns.items():
                if "url_patterns" in patterns:
                    matches = 0
                    for pattern in patterns["url_patterns"]:
                        if pattern.search(url):
                            matches += 1
                    if matches > 0:
                        confidence = min(matches / len(patterns["url_patterns"]), 1.0)
                        return cms, confidence, ["url_patterns"]
            return None
        except Exception as e:
            logger.debug(f"URL pattern detection failed: {e}")
            return None

    def _detect_by_js_frameworks(
        self, html: Optional[str] = None, scripts: Optional[List[str]] = None
    ) -> Optional[Tuple[str, float, List[str]]]:
        """Detect CMS by JavaScript frameworks."""
        try:
            content_to_check = []
            if html:
                content_to_check.append(html)
            if scripts:
                content_to_check.extend(scripts)

            for cms, patterns in self.cms_patterns.items():
                if "js_patterns" in patterns:
                    matches = 0
                    for content in content_to_check:
                        for pattern in patterns["js_patterns"]:
                            if pattern.search(content):
                                matches += 1
                                break  # One match per content source is enough
                    if matches > 0:
                        confidence = min(
                            (
                                matches / len(content_to_check)
                                if content_to_check
                                else 1.0
                            ),
                            1.0,
                        )
                        return cms, confidence, ["js_frameworks"]
            return None
        except Exception as e:
            logger.debug(f"JS framework detection failed: {e}")
            return None

    def _detect_by_file_paths(
        self, base_url: str
    ) -> Optional[Tuple[str, float, List[str]]]:
        """Detect CMS by probing file paths."""
        # This would require HTTP requests, simplified for now
        # In real implementation, would make HEAD requests to check file existence
        try:
            for cms, patterns in self.cms_patterns.items():
                if "file_paths" in patterns:
                    # Simulate file path checking (would need actual HTTP client)
                    matches = 0
                    for path in patterns["file_paths"]:
                        if isinstance(path, str):
                            full_url = urljoin(base_url, path)
                            # Placeholder: in real implementation, check if URL returns 200
                            # For now, just check if path contains CMS-specific patterns
                            if any(keyword in path.lower() for keyword in cms.split()):
                                matches += 1
                    if matches > 0:
                        confidence = min(matches / len(patterns["file_paths"]), 1.0)
                        return cms, confidence, ["file_paths"]
            return None
        except Exception as e:
            logger.debug(f"File path detection failed: {e}")
            return None

    def _detect_version(self, cms_type: str, html: str) -> Optional[str]:
        """Detect CMS version."""
        try:
            if (
                cms_type in self.cms_patterns
                and "version_patterns" in self.cms_patterns[cms_type]
            ):
                for pattern in self.cms_patterns[cms_type]["version_patterns"]:
                    match = pattern.search(html)
                    if match:
                        return match.group(1)
            return None
        except Exception as e:
            logger.debug(f"Version detection failed for {cms_type}: {e}")
            return None

    def _detect_plugins_and_extensions(
        self, cms_type: str, html: str, url: Optional[str] = None
    ) -> Tuple[List[str], List[str]]:
        """Detect plugins and extensions."""
        plugins = []
        extensions = []

        try:
            patterns = self.cms_patterns.get(cms_type, {})

            # Detect plugins
            if "plugin_patterns" in patterns:
                for pattern in patterns["plugin_patterns"]:
                    matches = pattern.findall(html)
                    plugins.extend(matches)

            if "app_patterns" in patterns and url:
                for pattern in patterns["app_patterns"]:
                    matches = pattern.findall(url)
                    plugins.extend(matches)

            # Detect extensions/modules
            if "extension_patterns" in patterns:
                for pattern in patterns["extension_patterns"]:
                    matches = pattern.findall(html)
                    extensions.extend(matches)

            if "module_patterns" in patterns:
                for pattern in patterns["module_patterns"]:
                    matches = pattern.findall(html)
                    extensions.extend(matches)

            # Remove duplicates
            plugins = list(set(plugins))
            extensions = list(set(extensions))

            return plugins, extensions

        except Exception as e:
            logger.debug(f"Plugin/extension detection failed for {cms_type}: {e}")
            return [], []

    def _generate_cache_key(self, url: Optional[str], html: Optional[str]) -> str:
        """Generate cache key for detection results."""
        if url:
            domain = urlparse(url).netloc
            return domain if domain else "empty"
        return "empty"

    def clear_cache(self) -> None:
        """Clear detection cache."""
        self._detection_cache.clear()

    def get_supported_cms(self) -> List[str]:
        """Get list of supported CMS platforms."""
        return list(self.cms_patterns.keys())

    def add_custom_cms(self, name: str, patterns: Dict[str, Any]) -> None:
        """
        Add custom CMS patterns.

        Args:
            name: CMS name
            patterns: Dictionary of detection patterns
        """
        self.cms_patterns[name] = patterns
        self._compiled_patterns[name] = patterns
        if self.config.custom_cms_patterns is None:
            self.config.custom_cms_patterns = {}
        self.config.custom_cms_patterns[name] = patterns


# Convenience functions for integration


def detect_cms_by_patterns(
    url: Optional[str] = None,
    html: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
    scripts: Optional[List[str]] = None,
    config: Optional[CMSConfig] = None,
) -> CMSDetectionResult:
    """
    Convenience function for CMS detection.

    Args:
        url: Optional URL to analyze
        html: Optional HTML content to analyze
        headers: Optional HTTP headers
        scripts: Optional list of script sources
        config: Optional CMS configuration

    Returns:
        CMSDetectionResult with detection details
    """
    detector = CMSDetection(config)
    return detector.detect_cms_by_patterns(url, html, headers, scripts)


def calculate_detection_confidence(
    detection_scores: Dict[str, float], methods_used: List[str]
) -> float:
    """
    Convenience function for confidence calculation.

    Args:
        detection_scores: Dictionary of CMS -> score mappings
        methods_used: List of detection methods used

    Returns:
        Confidence score between 0.0 and 1.0
    """
    detector = CMSDetection()
    return detector.calculate_detection_confidence(detection_scores, methods_used)


def validate_cms_detection(
    cms_type: str, url: Optional[str] = None, html: Optional[str] = None
) -> bool:
    """
    Convenience function for CMS validation.

    Args:
        cms_type: Detected CMS type
        url: Optional URL for validation
        html: Optional HTML for validation

    Returns:
        True if validation passes, False otherwise
    """
    detector = CMSDetection()
    return detector.validate_cms_detection(cms_type, url, html)


def get_cms_specific_selectors(field: str, cms_type: Optional[str] = None) -> List[str]:
    """
    Convenience function for getting CMS-specific selectors.

    Args:
        field: Field type (name, price, stock, etc.)
        cms_type: CMS type, if known

    Returns:
        List of CMS-specific selectors
    """
    detector = CMSDetection()
    return detector.get_cms_specific_selectors(field, cms_type)


# Integration helpers for existing classes


def integrate_with_sitemap_analyzer(sitemap_analyzer_instance) -> None:
    """
    Integrate CMS detection with SitemapAnalyzer.

    Args:
        sitemap_analyzer_instance: Instance of SitemapAnalyzer to enhance
    """
    detector = CMSDetection()

    # Add CMS detection methods to SitemapAnalyzer
    sitemap_analyzer_instance.detect_cms_comprehensive = (
        lambda page: detector.detect_cms_by_patterns(url=page.url, html=page.content())
    )
    sitemap_analyzer_instance.get_cms_selectors = (
        lambda field, cms: detector.get_cms_specific_selectors(field, cms)
    )


def integrate_with_product_parser(product_parser_instance) -> None:
    """
    Integrate CMS detection with ProductParser.

    Args:
        product_parser_instance: Instance of ProductParser to enhance
    """
    detector = CMSDetection()

    # Add CMS-aware methods to ProductParser
    original_detect_cms = getattr(product_parser_instance, "detect_cms_type", None)
    if original_detect_cms:
        product_parser_instance.detect_cms_advanced = (
            lambda url=None, html=None: detector.detect_cms_by_patterns(url, html)
        )
    else:
        product_parser_instance.detect_cms_type = (
            lambda url=None, html=None: detector.detect_cms_by_patterns(
                url, html
            ).cms_type
        )

    product_parser_instance.get_cms_selectors = (
        lambda field, cms=None: detector.get_cms_specific_selectors(field, cms)
    )
