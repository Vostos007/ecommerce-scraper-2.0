from typing import List, Dict, Optional, Tuple, Any, Set, TYPE_CHECKING
import logging
from itertools import product
from pathlib import Path
from urllib.parse import urljoin, urlparse
import html
import ast
import os
import hashlib
import copy

import requests
from functools import lru_cache
import json
import time
import re

from core.antibot_manager import AntibotManager
from utils.helpers import (
    get_variation_type,
    get_variation_type_details,
    clean_price,
    parse_stock,
    sanitize_text,
)
from utils.cms_detection import CMSDetection, CMSDetectionResult

if TYPE_CHECKING:  # pragma: no cover
    from bs4 import BeautifulSoup  # type: ignore
    from playwright.sync_api import Page
else:
    BeautifulSoup = Any  # type: ignore
    Page = Any  # type: ignore


@lru_cache(maxsize=1)
def _bs():
    from bs4 import BeautifulSoup  # локальный импорт для снижения cold start

    return BeautifulSoup


SETTINGS_PATH = Path("config/settings.json")


@lru_cache(maxsize=1)
def _load_settings() -> Dict[str, Any]:
    try:
        with SETTINGS_PATH.open("r", encoding="utf-8") as file:
            return json.load(file)
    except FileNotFoundError:
        logging.getLogger(__name__).warning(
            "Settings file not found, using fallback defaults"
        )
    except json.JSONDecodeError:
        logging.getLogger(__name__).warning(
            "Failed to decode settings.json, using fallback defaults"
        )
    return {}


class VariationParser:
    REQUEST_TIMEOUT = 20

    def __init__(
        self,
        antibot_manager: Optional[AntibotManager] = None,
        page: Optional[Page] = None,
        cms_type: Optional[str] = None,
    ):
        self.antibot_manager = antibot_manager
        self.page = page
        self.cms_type = cms_type
        self._cms_detector: Optional[CMSDetection] = None
        self._selectors: Optional[List[str]] = None
        self._currency_defaults: Optional[Dict[str, str]] = None
        self._cms_selector_flags: Optional[Dict[str, Any]] = None
        self._api_credentials: Optional[Dict[str, Dict[str, Any]]] = None
        self._current_url: Optional[str] = None
        self._bitrix_json_cache: Dict[str, List[Dict[str, Any]]] = {}

    @property
    def cms_detector(self) -> CMSDetection:
        if self._cms_detector is None:
            self._cms_detector = CMSDetection()
        return self._cms_detector

    @property
    def selectors(self) -> List[str]:
        if self._selectors is None:
            self._selectors = self._load_selectors()
        return self._selectors

    @property
    def currency_defaults(self) -> Dict[str, str]:
        if self._currency_defaults is None:
            self._currency_defaults = self._load_currency_defaults()
        return self._currency_defaults

    @property
    def cms_selector_flags(self) -> Dict[str, Any]:
        if self._cms_selector_flags is None:
            self._cms_selector_flags = self._load_cms_selector_flags()
        return self._cms_selector_flags

    @property
    def api_credentials(self) -> Dict[str, Dict[str, Any]]:
        if self._api_credentials is None:
            self._api_credentials = self._load_api_credentials()
        return self._api_credentials

    def _load_selectors(self) -> List[str]:
        config = _load_settings()
        variations = config.get("opencart_selectors", {}).get(
            "variations",
            ['.options select[name*="option"]', ".form-group select"],
        )
        if isinstance(variations, list):
            return variations
        if isinstance(variations, str):
            return [variations]
        return self._get_generic_selectors()

    def _load_currency_defaults(self) -> Dict[str, str]:
        """Load domain-specific currency defaults from configuration."""

        config = _load_settings()
        overrides = config.get("variation_currency_defaults", {})
        mapping: Dict[str, str] = {}
        if isinstance(overrides, dict):
            for domain, currency in overrides.items():
                if not domain or not currency:
                    continue
                domain_key = str(domain).lower().lstrip(" ").rstrip("/")
                currency_code = sanitize_text(str(currency).upper())
                if domain_key:
                    mapping[domain_key] = currency_code

        if "6wool.ru" not in mapping:
            mapping["6wool.ru"] = "RUB"

        return mapping

    def _load_api_credentials(self) -> Dict[str, Dict[str, Any]]:
        """Load optional API credential metadata from configuration."""

        self._cms_selector_flags = self._load_cms_selector_flags()
        config = _load_settings()
        credentials = config.get("api_credentials", {})
        return credentials if isinstance(credentials, dict) else {}

    def _load_cms_selector_flags(self) -> Dict[str, Any]:
        config = _load_settings()
        selectors_config = config.get("cms_selectors")
        return selectors_config if isinstance(selectors_config, dict) else {}

    def _api_support_enabled(self, cms_type: Optional[str]) -> bool:
        if not cms_type:
            return False

        selectors_entry: Dict[str, Any] = {}
        entry = self.cms_selector_flags.get(cms_type)
        if isinstance(entry, dict):
            selectors_entry = entry

        api_support = selectors_entry.get("api_support") if selectors_entry else None

        cred_entry: Dict[str, Any] = {}
        if isinstance(self.api_credentials, dict):
            cred_entry = self.api_credentials.get(cms_type) or {}
            if api_support is None and isinstance(cred_entry, dict):
                api_support = cred_entry.get("api_support")

        if isinstance(api_support, bool):
            return api_support

        return bool(cred_entry)

    def _sixwool_price_fallback_enabled(self) -> bool:
        entry = self.cms_selector_flags.get("sixwool", {}) if isinstance(self.cms_selector_flags, dict) else {}

        if isinstance(entry, dict):
            flag = entry.get("allow_price_fallback")
            if isinstance(flag, bool):
                return flag
            if flag is not None:
                return bool(flag)

        return False

    def _get_generic_selectors(self) -> List[str]:
        """Get generic variation selectors as fallback."""
        return [
            '.options select[name*="option"]',
            ".form-group select",
            ".product-options select",
            ".variations select",
            ".configurable-options select"
        ]

    def _resolve_currency_override(self, url: Optional[str]) -> Optional[str]:
        if not url or not isinstance(url, str):
            return None
        domain = urlparse(url).netloc.lower()
        if ":" in domain:
            domain = domain.split(":", 1)[0]
        if domain.startswith("www."):
            domain = domain[4:]
        return self.currency_defaults.get(domain)

    def detect_cms_and_get_selectors(self, url: Optional[str] = None, html: Optional[str] = None) -> Tuple[Optional[str], Dict[str, List[str]]]:
        """
        Detect CMS platform and get variation-specific selectors.

        Returns:
            Tuple of (cms_type, selectors_dict)
        """
        logger = logging.getLogger(__name__)

        # Use provided CMS type if available
        if self.cms_type:
            cms_type = self.cms_type
            logger.info(f"Using pre-detected CMS type: {cms_type}")
        else:
            # Detect CMS
            detection_result = self.cms_detector.detect_cms_by_patterns(url=url, html=html)
            cms_type = detection_result.cms_type
            if cms_type:
                logger.info(f"Detected CMS: {cms_type} (confidence: {detection_result.confidence:.2f})")
                self.cms_type = cms_type
            else:
                logger.warning("Could not detect CMS, using generic selectors")

        # Get variation selectors for detected CMS
        selectors_dict = {}
        variation_types = ["selectors", "attributes", "swatches", "price_update", "stock_update", "json_data"]

        for var_type in variation_types:
            selectors_dict[var_type] = self.cms_detector.get_variation_selectors(var_type, cms_type)

        selectors_dict["price_update"] = self._merge_selector_lists(
            [
                ".price-new",
                ".product-price",
                ".price",
                ".current-price",
                ".autocalc-product-price",
                'span[itemprop="price"]',
            ],
            [s for s in selectors_dict.get("price_update", []) if s != "[data-price]"]
        )
        selectors_dict["stock_update"] = self._merge_selector_lists(
            [
                ".product-availability",
                ".stock-status",
                ".stock",
                ".availability",
                'span[itemprop="availability"]',
            ],
            selectors_dict.get("stock_update", [])
        )
        selectors_dict["attributes"] = self._merge_selector_lists(
            [
                ".product-option__value select",
                'select[name^="option"]',
            ],
            selectors_dict.get("attributes", [])
        )

        return cms_type, selectors_dict

    def extract_variations(
        self, html: Optional[str] = None, page: Optional[Page] = None, url: Optional[str] = None
    ) -> List[Dict]:
        """Extract product variations with price/stock for each option, with fallback to static parsing."""
        variations = []
        self.page = page if page else self.page
        if url:
            self._current_url = url

        logger = logging.getLogger(__name__)

        if html is None and self.page is not None:
            try:
                html = self.page.content()
            except Exception:
                html = None

        if url and "sittingknitting.ru" in url:
            try:
                sitting_variations = self._extract_sittingknitting_variations(html, url)
                if sitting_variations:
                    return sitting_variations
            except Exception as exc:
                logger.debug(
                    "SittingKnitting variation extraction failed: %s", exc
                )

        # Detect CMS and get appropriate selectors
        cms_type, cms_selectors = self.detect_cms_and_get_selectors(url=url, html=html)

        if self.page is None:
            logger.info(
                "Playwright page unavailable, using static HTML parsing fallback"
            )
            if html is None:
                logger.warning("No HTML provided for static parsing")
                return []
            return self.extract_variations_static(html, cms_selectors, cms_type)

        logger.info(f"Using interactive Playwright parsing for CMS: {cms_type or 'unknown'}")

        # Try table first (static)
        html_content = html or self.page.content()
        table_variations = self.parse_variation_table(html_content)
        if table_variations:
            logger.info(f"Found {len(table_variations)} variations from table parsing")
            return table_variations

        try:
            self.page.wait_for_selector(".options", timeout=15000)
        except:
            logger.warning("No .options found")
            return []

        # Find variation selects using CMS-specific selectors
        attribute_selectors = cms_selectors.get("attributes", ['select[name^="option"]'])
        selects = []
        seen_selects = set()

        for selector in attribute_selectors:
            try:
                found_selects = self.page.query_selector_all(selector)
                for found in found_selects:
                    ref = id(found)
                    if ref not in seen_selects:
                        selects.append(found)
                        seen_selects.add(ref)
            except Exception as e:
                logger.debug(f"Selector {selector} failed: {e}")
                continue

        if not selects:
            logger.warning(
                f"No variation selects found using selectors: {attribute_selectors}"
            )
            fallback = self.extract_variations_static(
                html_content, cms_selectors, cms_type
            )
            if fallback:
                logger.info(
                    "Falling back to static variation extraction due to missing selects"
                )
                return fallback
            return []

        for select in selects:
            if select.is_visible():
                # Get type with enhanced classification
                select_name = select.get_attribute("name")
                if select_name:
                    label_locator = self.page.locator(
                        f'label[for*="{select_name}"]'
                    ).first
                    label_text = (
                        label_locator.inner_text().strip()
                        if label_locator.is_visible()
                        else "Variation"
                    )
                    var_type = "Variation"  # fallback
                else:
                    label_text = "Variation"
                    var_type = "Variation"

                # Get options
                options = select.query_selector_all("option")
                option_count = len(options)
                placeholder_terms = {
                    "select",
                    "choose",
                    "option",
                    "выберите",
                    "не выбрано",
                    "-",
                }

                for opt in options:
                    if opt.is_visible():
                        value = opt.get_attribute("value") or opt.inner_text().strip()
                        value_normalized = value.strip() if isinstance(value, str) else ""
                        if value_normalized and value_normalized.lower() not in placeholder_terms:
                            # Select option
                            select.select_option(value=value)
                            self.page.wait_for_timeout(2000)  # Wait for update
                            if self.antibot_manager:
                                self.antibot_manager.human_delay(1)  # Additional human delay

                            # Extract updated price/stock using CMS-specific selectors
                            updated_price = self.extract_price(cms_selectors=cms_selectors)
                            updated_stock = self.extract_stock(cms_selectors=cms_selectors)

                            if updated_price is None:
                                updated_price = 0.0

                            if updated_stock is None:
                                updated_stock = 0

                            # Enhanced classification
                            classification = self.classify_variation_type(
                                label_text, value_normalized
                            )
                            display_name = self.format_variation_display_name(
                                classification["type"], value_normalized
                            )

                            # Determine sort order
                            sort_order = 999
                            if classification["type"] == "size":
                                # Size-specific ordering
                                size_order = {
                                    "XS": 1,
                                    "S": 2,
                                    "M": 3,
                                    "L": 4,
                                    "XL": 5,
                                    "XXL": 6,
                                    "XXXL": 7,
                                }
                                sort_order = size_order.get(value.upper(), 99)

                            variation = {
                                "type": classification["type"],
                                "value": value_normalized,
                                "price": updated_price,
                                "stock": updated_stock,
                                "display_name": display_name,
                                "sort_order": sort_order,
                                "category": classification["category"],
                                "confidence_score": classification["confidence"],
                            }

                            validated = self.validate_variation_data(variation)
                            if validated:
                                variations.append(validated)

                            # Reset to first option
                            if option_count > 1:
                                select.select_option(index=0)
                                self.page.wait_for_timeout(500)
                                if self.antibot_manager:
                                    self.antibot_manager.human_delay(0.5)

        logger.info(f"Extracted {len(variations)} variations via interactive parsing")
        return variations

    def _extract_js_var(self, html_content: Optional[str], var_name: str) -> Optional[str]:
        if not html_content:
            return None

        pattern = re.compile(rf"var\s+{re.escape(var_name)}\s*=\s*(.*?);", re.DOTALL)
        match = pattern.search(html_content)
        if not match:
            return None

        raw_value = match.group(1).strip()
        # Remove inline comments
        raw_value = raw_value.split("\n", 1)[0].split("//", 1)[0].strip()

        if raw_value.endswith(";"):
            raw_value = raw_value[:-1].strip()

        if raw_value.startswith(("'", '"')) and raw_value.endswith(("'", '"')):
            raw_value = raw_value[1:-1]

        return raw_value.replace("\\/", "/").replace("\\\"", '"')

    def _extract_sittingknitting_variations(
        self, html: Optional[str], url: Optional[str]
    ) -> List[Dict[str, Any]]:
        logger = logging.getLogger(__name__)

        if not url:
            return []

        page_html = html
        if not page_html:
            try:
                response = requests.get(url, timeout=self.REQUEST_TIMEOUT)
                response.raise_for_status()
                page_html = response.text
            except Exception as exc:
                logger.debug("Failed to fetch HTML for SittingKnitting: %s", exc)
                return []

        soup = _bs()(page_html, "html.parser")
        container = soup.select_one("div.elementSku")
        if not container:
            return []

        properties: List[Dict[str, Any]] = []
        for prop in soup.select("div.elementSkuProperty"):
            prop_name = prop.get("data-name")
            if not prop_name:
                continue

            values = []
            for li in prop.select("li.elementSkuPropertyValue"):
                value = li.get("data-value")
                if value:
                    values.append(value.strip())

            if not values:
                continue

            properties.append(
                {
                    "name": prop_name,
                    "values": values,
                    "level": prop.get("data-level") or str(len(properties) + 1),
                    "highload": prop.get("data-highload", "N"),
                }
            )

        if not properties:
            return []

        props_entries = [
            f"{prop['name']}:{value}"
            for prop in properties
            for value in prop["values"]
        ]
        props_string = ";".join(props_entries)
        if props_string:
            props_string += ";"

        highload_entries = [
            prop["name"] for prop in properties if prop["highload"].upper() == "Y"
        ]
        highload_string = ";".join(highload_entries)
        if highload_string:
            highload_string += ";"

        ajax_path = self._extract_js_var(page_html, "elementAjaxPath")
        ajax_url = urljoin(url, ajax_path) if ajax_path else urljoin(
            url, "/local/templates/sittingknitting/components/unlimtech/catalog.item/detail/ajax.php"
        )

        site_id = self._extract_js_var(page_html, "SITE_ID") or "s1"
        count_properties = self._extract_js_var(page_html, "countTopProperties")

        combinations = list(product(*(prop["values"] for prop in properties)))
        if not combinations:
            return []

        max_variations = 200
        if len(combinations) > max_variations:
            logger.warning(
                "SittingKnitting variation count %s exceeds %s; truncating",
                len(combinations),
                max_variations,
            )
            combinations = combinations[:max_variations]

        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": url,
            "User-Agent": "Mozilla/5.0 (compatible; VariationParser/1.0; +https://sittingknitting.ru)",
        }

        variations_map: Dict[str, Dict[str, Any]] = {}

        with requests.Session() as session:
            session.headers.update(headers)

            for combo in combinations:
                attributes = {
                    prop["name"]: value
                    for prop, value in zip(properties, combo)
                }

                params_entries = [
                    f"{prop['name']}:{value}"
                    for prop, value in zip(properties, combo)
                ]
                params_string = ";".join(params_entries)
                if params_string:
                    params_string += ";"

                payload = {
                    "act": "selectSku",
                    "props": props_string,
                    "params": params_string,
                    "level": properties[len(combo) - 1]["level"] if combo else "1",
                    "iblock_id": container.get("data-iblock-id", ""),
                    "prop_id": container.get("data-prop-id", ""),
                    "product_id": container.get("data-product-id", ""),
                    "highload": highload_string,
                    "price-code": container.get("data-price-code", ""),
                    "deactivated": container.get("data-deactivated", "N"),
                    "siteId": site_id,
                }

                if count_properties:
                    payload["countProperties"] = count_properties

                try:
                    response = session.post(
                        ajax_url,
                        data=payload,
                        timeout=self.REQUEST_TIMEOUT,
                    )
                    response.raise_for_status()
                    json_data = response.json()
                except Exception as exc:
                    logger.debug("Variant request failed for %s: %s", combo, exc)
                    continue

                if not json_data or not isinstance(json_data, list):
                    continue

                product_data = json_data[0].get("PRODUCT", {})
                if not product_data:
                    continue

                variant_id = str(product_data.get("ID", "")).strip()
                detail_url = product_data.get("DETAIL_PAGE_URL") or ""
                detail_url = detail_url.replace("\\/", "/")
                variant_url = urljoin(url, detail_url) if detail_url else url

                price_value = product_data.get("PRICE", {}).get("RESULT_PRICE", {}).get(
                    "DISCOUNT_PRICE"
                )
                if isinstance(price_value, str):
                    price = clean_price(html.unescape(price_value))
                elif price_value is not None:
                    try:
                        price = float(price_value)
                    except (TypeError, ValueError):
                        price = None
                else:
                    fallback_price = product_data.get("PRICE", {}).get("DISCOUNT_PRICE")
                    price = clean_price(html.unescape(fallback_price)) if fallback_price else None

                stock_raw = product_data.get("CATALOG_QUANTITY")
                try:
                    stock = int(float(stock_raw)) if stock_raw is not None else None
                except (TypeError, ValueError):
                    stock = parse_stock(str(stock_raw)) if stock_raw is not None else None

                can_buy = product_data.get("CAN_BUY") == "Y"
                in_stock = bool(stock is not None and stock > 0) or can_buy

                attributes_clean = {
                    sanitize_text(str(key)): sanitize_text(str(value))
                    for key, value in attributes.items()
                }
                if attributes_clean:
                    if len(attributes_clean) == 1:
                        display_value = next(iter(attributes_clean.values()))
                    else:
                        display_value = " / ".join(
                            f"{key}: {value}" for key, value in attributes_clean.items()
                        )
                else:
                    display_value = sanitize_text(
                        html.unescape(product_data.get("NAME", ""))
                    )

                variation_type = "variant"
                if len(attributes_clean) == 1:
                    prop_name = next(iter(attributes_clean)).lower()
                    if "tsvet" in prop_name or "color" in prop_name:
                        variation_type = "color"
                    else:
                        derived = get_variation_type(prop_name.replace("_", " "))
                        variation_type = derived if derived and derived != "unknown" else "variant"

                sku = None
                properties_block = product_data.get("PROPERTIES", {})
                for key in ("CML2_ARTICLE", "ARTNUMBER", "SKU"):
                    prop_entry = properties_block.get(key)
                    if isinstance(prop_entry, dict):
                        sku_candidate = prop_entry.get("VALUE")
                        if sku_candidate:
                            sku = sanitize_text(str(sku_candidate))
                            break

                map_key = variant_id or display_value
                variations_map[map_key] = {
                    "type": variation_type,
                    "value": display_value,
                    "price": price,
                    "stock": stock,
                    "in_stock": in_stock,
                    "variant_id": variant_id or None,
                    "sku": sku,
                    "url": variant_url,
                    "attributes": attributes_clean,
                }

                if len(properties) == 1:
                    continue

                time.sleep(0.02)

        variations_list = list(variations_map.values())
        variations_list.sort(key=lambda item: item.get("value", ""))
        return variations_list

    def parse_variation_table(self, html: str) -> List[Dict]:
        """Parse variations from table format using BS4 - primary fallback for static sites."""
        logger = logging.getLogger(__name__)
        try:
            soup = _bs()(html, "lxml")
            tables = soup.select(
                ".product-variations, .variation-table, table.variations, .variations-table"
            )

            variations = []
            for table in tables:
                rows = table.select("tr")[1:]  # Skip header
                for row in rows:
                    cells = row.select("td")
                    if len(cells) >= 3:
                        size = cells[0].get_text(strip=True)
                        color = cells[1].get_text(strip=True) if len(cells) > 1 else ""
                        price_text = cells[2].get_text(strip=True)

                        price = clean_price(price_text) if price_text else None

                        # Combine size and color
                        var_value = (
                            f"{size} {color}".strip()
                            if size and color
                            else size or color
                        )
                        var_type = (
                            "size_color"
                            if size and color
                            else ("size" if size else "color")
                        )

                        # Enhanced classification
                        classification = self.classify_variation_type(
                            "Size/Color", var_value
                        )
                        display_name = self.format_variation_display_name(
                            classification["type"], var_value
                        )

                        variation = {
                            "type": var_type,
                            "value": var_value,
                            "price": price,
                            "stock": None,  # Table may not have stock
                            "display_name": display_name,
                            "sort_order": 999,
                            "category": classification["category"],
                            "confidence_score": classification["confidence"],
                        }

                        validated = self.validate_variation_data(variation)
                        if validated:
                            variations.append(validated)

            logger.debug(f"Parsed {len(variations)} variations from table")
            return variations
        except Exception as e:
            logger.error(f"Error parsing variation table: {e}")
            return []

    def extract_variations_static(self, html: str, cms_selectors: Optional[Dict[str, List[str]]] = None, cms_type: Optional[str] = None) -> List[Dict]:
        """Pure HTML parsing for variations without Playwright interactions."""
        logger = logging.getLogger(__name__)
        try:
            soup = _bs()(html, "lxml")

            # Sixwool-specific parsing path leveraging Bitrix helpers + AJAX endpoints
            if cms_type == "sixwool":
                sixwool_variations = self._parse_sixwool_variations(
                    html,
                    cms_selectors or {},
                )
                if sixwool_variations:
                    return self._deduplicate_variations(sixwool_variations)

            # Try to find variation selects using CMS-specific selectors
            variations = []
            selects = []

            if cms_selectors:
                attribute_selectors = cms_selectors.get("attributes", ['select[name^="option"]'])
                for selector in attribute_selectors:
                    try:
                        found_selects = soup.select(selector)
                        selects.extend(found_selects)
                    except Exception as e:
                        logger.debug(f"Selector {selector} failed: {e}")
                        continue
            else:
                # Fallback to generic selectors
                selects = soup.select('select[name^="option"]')

            if not selects:
                if cms_selectors and cms_type:
                    json_variations = self.extract_variations_from_json(
                        html, cms_selectors, cms_type
                    )
                    if json_variations:
                        return json_variations
                return self.parse_variation_table(html)

            base_price = self.extract_price_static(html, cms_selectors)
            base_stock = self.extract_stock_static(html, cms_selectors)

            for select in selects:
                select_name = select.get("name", "")
                if select_name:
                    label = soup.select_one(f'label[for*="{select_name}"]')
                    label_text = label.get_text(strip=True) if label else "Variation"
                else:
                    label_text = "Variation"

                options = select.select("option")
                placeholder_terms = {
                    "select",
                    "choose",
                    "option",
                    "выберите",
                    "не выбрано",
                    "-",
                }
                for opt in options:
                    option_id = opt.get("value")
                    label_value = opt.get_text(strip=True)
                    if label_value:
                        label_value = " ".join(label_value.split())
                        label_value = re.sub(r"\+\s*[\d.,]+\s*[рp]\.", "", label_value, flags=re.IGNORECASE)
                        label_value = label_value.replace("\\", "").strip()
                    value = label_value or (option_id.strip() if isinstance(option_id, str) else "")
                    value_normalized = value.strip() if isinstance(value, str) else ""
                    if not value_normalized or value_normalized.lower() in placeholder_terms:
                        continue

                    option_price = self._extract_option_price(opt, base_price)
                    option_stock = self._extract_option_stock(opt, base_stock)

                    if option_price is None:
                        option_price = 0.0

                    if option_stock is None:
                        option_stock = 0

                    classification = self.classify_variation_type(label_text, value)
                    display_name = self.format_variation_display_name(
                        classification["type"], value
                    )

                    option_price = self._extract_option_price(opt, base_price)
                    option_stock = self._extract_option_stock(opt, base_stock)

                    if option_price is None:
                        option_price = 0.0

                    if option_stock is None:
                        option_stock = 0

                    classification = self.classify_variation_type(label_text, value_normalized)
                    display_name = self.format_variation_display_name(
                        classification["type"], value_normalized
                    )

                    variation = {
                        "type": classification["type"],
                        "value": value_normalized,
                        "option_id": option_id,
                        "price": option_price,
                        "stock": option_stock,
                        "display_name": display_name,
                        "sort_order": 999,
                        "category": classification["category"],
                        "confidence_score": classification["confidence"],
                    }

                    validated = self.validate_variation_data(variation)
                    if validated:
                        variations.append(validated)

            logger.debug(f"Extracted {len(variations)} static variations")
            return self._deduplicate_variations(variations)

        except Exception as e:
            logger.error(f"Error in static variation extraction: {e}")
            return self.parse_variation_table(html)  # Ultimate fallback

    def extract_variations_from_json(self, html: str, cms_selectors: Dict[str, List[str]], cms_type: str) -> List[Dict]:
        """Extract variations from JSON data embedded in HTML (common in modern CMS)."""
        logger = logging.getLogger(__name__)
        variations = []

        try:
            if cms_type == "sixwool":
                sixwool_variations = self._parse_sixwool_variations(
                    html,
                    cms_selectors,
                )
                if sixwool_variations:
                    return self._deduplicate_variations(sixwool_variations)

            soup = _bs()(html, "lxml")
            json_selectors = cms_selectors.get("json_data", [])

            shop2_variant_attributes: Dict[str, Dict[str, str]] = {}

            base_url = self._current_url
            if not base_url and self.page is not None:
                try:
                    base_url = self.page.url
                except Exception:
                    base_url = None

            origin = None
            if base_url:
                parsed = urlparse(base_url)
                if parsed.scheme and parsed.netloc:
                    origin = f"{parsed.scheme}://{parsed.netloc}"

            product_id: Optional[str] = None
            if cms_type in {"cscart", "insales", "cm3"}:
                product_id = self._extract_product_id(html, cms_type)

            api_enabled = self._api_support_enabled(cms_type)

            if cms_type == "cscart" and origin and api_enabled:
                api_variations = self._parse_cscart_api(product_id, origin)
                if api_variations:
                    logger.info(
                        "Extracted %d variations from CS-Cart API", len(api_variations)
                    )
                    return self._deduplicate_variations(api_variations)

            if cms_type == "insales" and origin and api_enabled:
                api_variations = self._parse_insales_admin_api(product_id, origin)
                if api_variations:
                    logger.info(
                        "Extracted %d variations from InSales Admin API", len(api_variations)
                    )
                    return self._deduplicate_variations(api_variations)

            for selector in json_selectors:
                try:
                    scripts = soup.select(selector)
                    for script in scripts:
                        script_content = script.get_text() if script else ""
                        if not script_content:
                            continue

                        # Try to extract JSON data based on CMS type
                        if cms_type == "bitrix":
                            variations.extend(self._parse_bitrix_json(script_content))
                        elif cms_type == "insales":
                            variations.extend(self._parse_insales_json(script_content))
                        elif cms_type == "cm3":
                            if "shop2.init" in script_content:
                                mapping = self._parse_shop2_init(script_content)
                                if mapping:
                                    for key, value in mapping.items():
                                        if key not in shop2_variant_attributes:
                                            shop2_variant_attributes[key] = value
                            variations.extend(self._parse_cm3_json(script_content))
                        elif cms_type == "shopify":
                            variations.extend(self._parse_shopify_json(script_content))
                        elif cms_type == "woocommerce" or cms_type == "wordpress":
                            variations.extend(self._parse_woocommerce_json(script_content))
                        elif cms_type == "magento":
                            variations.extend(self._parse_magento_json(script_content))
                        else:
                            if "offers" in script_content and not variations:
                                bitrix_variations = self._parse_bitrix_json(script_content)
                                if bitrix_variations:
                                    variations.extend(bitrix_variations)
                                    continue
                            if "InSales" in script_content or "variants" in script_content:
                                insales_variations = self._parse_insales_json(script_content)
                                if insales_variations:
                                    variations.extend(insales_variations)
                                    continue
                            variations.extend(self._parse_generic_json(script_content))

                except Exception as e:
                    logger.debug(f"Failed to parse JSON with selector {selector}: {e}")
                    continue

            if cms_type == "insales" and not variations:
                data_nodes = soup.select("[data-product], [data-variants]")
                for node in data_nodes:
                    raw = node.get("data-product") or node.get("data-variants")
                    if not raw:
                        continue
                    parsed = self._safe_json_loads(raw)
                    if not parsed:
                        continue
                    variations.extend(self._parse_insales_variants(parsed))

            if cms_type == "bitrix" and not variations:
                for script in soup.find_all("script"):
                    script_content = script.get_text() if script else ""
                    if not script_content:
                        continue
                    variations.extend(self._parse_bitrix_json(script_content))
                    if variations:
                        logger.debug(
                            "Bitrix fallback discovered %d variations via script scan",
                            len(variations),
                        )
                        return self._deduplicate_variations(variations)

            if cms_type == "cm3":
                shop2_variations = self._parse_shop2_variations_from_dom(
                    soup,
                    shop2_variant_attributes,
                )
                if shop2_variations:
                    variations.extend(shop2_variations)

            logger.info(f"Extracted {len(variations)} variations from JSON data")
            return self._deduplicate_variations(variations)

        except Exception as e:
            logger.error(f"Error extracting variations from JSON: {e}")
            return []

    def _parse_sixwool_variations(
        self,
        html: Optional[str],
        cms_selectors: Optional[Dict[str, Any]] = None,
    ) -> List[Dict]:
        """Parse variations for 6wool.ru leveraging Bitrix helpers and AJAX fallbacks."""

        logger = logging.getLogger(__name__)
        if not html:
            return []

        selectors_cfg: Dict[str, Any] = cms_selectors or {}
        variations: List[Dict] = []
        soup = _bs()(html, "lxml")

        # 1) Attempt AJAX endpoints first – they provide the richest data
        ajax_variations = self._fetch_sixwool_ajax_variations(soup, selectors_cfg)
        if ajax_variations:
            logger.debug("sixwool AJAX parser yielded %d variations", len(ajax_variations))
            variations.extend(ajax_variations)

        # 2) Inline JSON blocks (Bitrix offers / custom globals)
        script_blocks = soup.find_all("script")
        for script in script_blocks:
            script_content = script.get_text() if script else ""
            if not script_content or script_content.strip() == "":
                continue
            lowered = script_content.lower()
            if any(anchor in lowered for anchor in ("sixwooloffers", "jccatalogelement", "offers")):
                bitrix_variations = self._parse_bitrix_json(script_content)
                if bitrix_variations:
                    variations.extend(bitrix_variations)
                else:
                    generic_variations = self._parse_generic_json(script_content)
                    if generic_variations:
                        variations.extend(generic_variations)
                continue

            json_candidate = self._safe_json_loads(script_content)
            if json_candidate and isinstance(json_candidate, (dict, list)):
                if isinstance(json_candidate, dict) and "offers" in json_candidate:
                    variations.extend(self._parse_bitrix_json(json.dumps(json_candidate)))
                else:
                    variations.extend(self._parse_generic_json(json.dumps(json_candidate)))

        # 3) Data attributes with embedded JSON payloads
        data_nodes = soup.select("[data-sixwool-json], script[data-sixwool-json]")
        for node in data_nodes:
            raw = node.get("data-sixwool-json") if node else None
            if not raw and node:
                raw = node.get_text() or node.string or ""
            parsed = self._safe_json_loads(raw) if raw else None
            if parsed:
                variations.extend(self._parse_bitrix_json(json.dumps(parsed)))

        if variations:
            return self._deduplicate_variations(variations)

        # 4) Static fallback – build variations from hydrated DOM
        base_price = self.extract_price_static(html, selectors_cfg)
        base_stock = self.extract_stock_static(html, selectors_cfg)
        allow_price_fallback = self._sixwool_price_fallback_enabled()

        containers = soup.select("[data-sixwool-variation], [data-entity='sku-line-block']")
        seen_pairs: Set[Tuple[str, str]] = set()

        for container in containers:
            label_text = self._guess_sixwool_label(container) or "Variation"

            selects = container.select("select")
            for select in selects:
                for opt in select.select("option"):
                    option_value = (opt.get("value") or "").strip()
                    option_text = sanitize_text(opt.get_text())
                    if not option_text or option_text.lower().startswith("выберите"):
                        continue
                    if not option_value:
                        continue
                    key = (label_text, option_text)
                    if key in seen_pairs:
                        continue
                    price_hint = opt.get("data-price") or opt.get("data-price-value")
                    option_price = self._coerce_price(price_hint, base_price)
                    option_stock = self._coerce_stock(opt.get("data-quantity"), base_stock)
                    built = self._build_sixwool_variation(
                        label_text,
                        option_text,
                        option_price,
                        option_stock,
                        fallback_price=base_price,
                        allow_price_fallback=allow_price_fallback,
                        extras={
                            "option_id": option_value or option_text,
                            "attributes": {label_text: option_text},
                        },
                    )
                    if built:
                        variations.append(built)
                        seen_pairs.add(key)

            radios = container.select("input[type='radio']")
            for radio in radios:
                value = (
                    radio.get("data-name")
                    or radio.get("aria-label")
                    or radio.get("value")
                    or radio.get("data-value")
                )
                value = sanitize_text(value or "")
                if not value:
                    continue
                key = (label_text, value)
                if key in seen_pairs:
                    continue
                price_hint = radio.get("data-price") or radio.get("data-price-value")
                radio_price = self._coerce_price(price_hint, base_price)
                stock_hint = radio.get("data-stock") or radio.get("data-quantity")
                radio_stock = self._coerce_stock(stock_hint, base_stock)
                built = self._build_sixwool_variation(
                    label_text,
                    value,
                    radio_price,
                    radio_stock,
                    fallback_price=base_price,
                    allow_price_fallback=allow_price_fallback,
                    extras={
                        "option_id": radio.get("value") or value,
                        "attributes": {label_text: value},
                    },
                )
                if built:
                    variations.append(built)
                    seen_pairs.add(key)

        return self._deduplicate_variations(variations)

    def _fetch_sixwool_ajax_variations(
        self,
        soup: BeautifulSoup,
        selectors_cfg: Dict[str, Any],
    ) -> List[Dict]:
        logger = logging.getLogger(__name__)
        base_url = self._current_url or ""
        endpoints = self._discover_sixwool_ajax_endpoints(soup, selectors_cfg)
        if not endpoints or not base_url:
            return []

        ajax_config = selectors_cfg.get("ajax_endpoints") if isinstance(selectors_cfg, dict) else {}
        headers = {}
        if isinstance(ajax_config, dict):
            headers = ajax_config.get("headers") or {}
        timeout = 15
        if isinstance(ajax_config, dict) and isinstance(ajax_config.get("timeout"), (int, float)):
            timeout = int(ajax_config["timeout"])

        variations: List[Dict] = []
        for endpoint in endpoints:
            full_url = urljoin(base_url, endpoint)
            payload = self._http_get_json(full_url, headers=headers, timeout=timeout)
            if not payload:
                continue
            try:
                serialized = json.dumps(payload)
            except (TypeError, ValueError):
                logger.debug("Unable to serialize sixwool AJAX payload from %s", full_url)
                continue
            ajax_variations = self._parse_bitrix_json(serialized)
            if ajax_variations:
                logger.debug(
                    "sixwool AJAX endpoint %s produced %d variations",
                    full_url,
                    len(ajax_variations),
                )
                variations.extend(ajax_variations)

        return self._deduplicate_variations(variations)

    def _discover_sixwool_ajax_endpoints(
        self,
        soup: BeautifulSoup,
        selectors_cfg: Dict[str, Any],
    ) -> List[str]:
        endpoints: List[str] = []

        ajax_cfg = selectors_cfg.get("ajax_endpoints") if isinstance(selectors_cfg, dict) else {}
        if isinstance(ajax_cfg, dict):
            offers = ajax_cfg.get("offers")
            if isinstance(offers, list):
                endpoints.extend([str(endpoint) for endpoint in offers if endpoint])

        attribute_names = [
            "data-ajax-url",
            "data-offers-url",
            "data-sixwool-endpoint",
            "data-endpoint",
        ]
        for attr in attribute_names:
            for node in soup.select(f"[{attr}]"):
                value = node.get(attr)
                if value:
                    endpoints.append(value)

        # Regex discovery from inline scripts
        script_content = "\n".join(
            script.get_text() or "" for script in soup.find_all("script")
        )
        pattern = re.compile(r"/ajax/[\w\-/]+\.php", re.IGNORECASE)
        endpoints.extend(pattern.findall(script_content))

        normalized: List[str] = []
        for endpoint in endpoints:
            if not endpoint:
                continue
            endpoint = endpoint.strip()
            if endpoint.startswith("http") or endpoint.startswith("//"):
                normalized_endpoint = endpoint
            else:
                normalized_endpoint = endpoint if endpoint.startswith("/") else f"/{endpoint}"
            if normalized_endpoint not in normalized:
                normalized.append(normalized_endpoint)
        return normalized

    def _guess_sixwool_label(self, node: Any) -> Optional[str]:
        if not node:
            return None

        candidates = [
            getattr(node, "get", lambda *args, **kwargs: None)("data-sixwool-label"),
            getattr(node, "get", lambda *args, **kwargs: None)("data-entity"),
            getattr(node, "get", lambda *args, **kwargs: None)("aria-label"),
        ]

        for selector in (
            "[data-sixwool-label]",
            ".product-detail__sku-title",
            ".product-detail__sku-name",
            ".sku-line-block__title",
            "label",
        ):
            label_node = None
            try:
                label_node = node.select_one(selector)
            except Exception:  # noqa: BLE001
                label_node = None
            if label_node and label_node.get_text():
                candidates.append(label_node.get_text())

        for candidate in candidates:
            text = sanitize_text(candidate or "")
            if text:
                return text
        return None

    def _coerce_price(self, raw_value: Optional[Any], fallback: Optional[float]) -> Optional[float]:
        if raw_value is None or raw_value == "":
            return fallback
        try:
            coerced = clean_price(str(raw_value))
            return coerced if coerced is not None else fallback
        except Exception:  # noqa: BLE001
            return fallback

    def _coerce_stock(
        self,
        raw_value: Optional[Any],
        fallback: Optional[int],
    ) -> Optional[int]:
        if raw_value is None or raw_value == "":
            return fallback
        parsed = parse_stock(str(raw_value))
        return parsed if parsed is not None else fallback

    def _build_sixwool_variation(
        self,
        label: str,
        value: str,
        price: Optional[float],
        stock: Optional[int],
        fallback_price: Optional[float] = None,
        allow_price_fallback: bool = False,
        extras: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict]:
        logger = logging.getLogger(__name__)

        effective_price = price
        if effective_price is None and allow_price_fallback:
            if fallback_price is not None:
                logger.debug(
                    "sixwool variation %s missing price hint; using fallback price %.2f",
                    value,
                    fallback_price,
                )
                effective_price = fallback_price
            else:
                logger.warning(
                    "sixwool variation %s missing price and fallback; dropping entry",
                    value,
                )

        if effective_price is None:
            return None

        classification = self.classify_variation_type(label, value)
        display_name = self.format_variation_display_name(classification["type"], value)

        variation: Dict[str, Any] = {
            "type": classification["type"],
            "value": value,
            "price": effective_price,
            "stock": stock,
            "display_name": display_name,
            "sort_order": 999,
            "category": classification["category"],
            "confidence_score": classification["confidence"],
        }

        if extras:
            if extras.get("option_id"):
                variation["option_id"] = extras["option_id"]
            if extras.get("sku"):
                variation["sku"] = extras["sku"]
            if extras.get("attributes"):
                variation["attributes"] = extras["attributes"]

        currency = self._resolve_currency_override(self._current_url)
        if currency:
            variation["currency"] = currency

        return self.validate_variation_data(variation)

    def _http_get_json(
        self,
        url: Optional[str],
        headers: Optional[Dict[str, str]] = None,
        timeout: int = 10,
    ) -> Optional[Any]:
        """Perform a guarded HTTP GET returning parsed JSON with lightweight retries."""

        if not url:
            return None

        logger = logging.getLogger(__name__)
        request_headers: Dict[str, str] = dict(headers or {})
        request_headers.setdefault("Accept", "application/json")
        parsed = urlparse(url)
        domain = parsed.netloc.lower()

        if self.page is not None and hasattr(self.page, "request"):
            try:
                response = self.page.request.get(
                    url,
                    headers=request_headers or None,
                    timeout=timeout * 1000,
                )
                try:
                    if response.ok:
                        return response.json()
                    logger.debug(
                        "Playwright request to %s returned status %s",
                        url,
                        response.status,
                    )
                finally:
                    try:
                        response.dispose()
                    except Exception:
                        pass
            except Exception as exc:  # noqa: BLE001
                logger.debug("Playwright request failed for %s: %s", url, exc)

        session = requests.Session()
        cookies_dict: Dict[str, str] = {}
        if self.page is not None and hasattr(self.page, "context"):
            try:
                context_cookies = self.page.context.cookies()
                for cookie in context_cookies or []:
                    name = cookie.get("name")
                    value = cookie.get("value")
                    if not name or value is None:
                        continue
                    cookies_dict[name] = str(value)
                    domain_attr = cookie.get("domain")
                    path_attr = cookie.get("path") or "/"
                    try:
                        if domain_attr:
                            session.cookies.set(
                                name,
                                value,
                                domain=domain_attr.lstrip("."),
                                path=path_attr,
                            )
                        else:
                            session.cookies.set(name, value, path=path_attr)
                    except Exception:
                        session.cookies.set(name, value)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Failed to extract cookies from Playwright context: %s", exc)

        if domain.endswith("6wool.ru") and self.antibot_manager:
            try:
                manager_payload = self.antibot_manager.fetch_json_via_flaresolverr(
                    url,
                    headers=request_headers,
                    cookies=cookies_dict,
                    timeout=timeout,
                )
                if manager_payload:
                    return manager_payload
            except Exception as exc:  # noqa: BLE001
                logger.debug("Antibot manager fetch failed for %s: %s", url, exc)

        max_attempts = 3
        effective_timeout = (
            min(timeout, self.REQUEST_TIMEOUT) if self.REQUEST_TIMEOUT else timeout
        )

        try:
            for attempt in range(1, max_attempts + 1):
                try:
                    response = session.get(
                        url,
                        headers=request_headers,
                        timeout=effective_timeout,
                    )
                    if 200 <= response.status_code < 300:
                        if not response.content:
                            logger.debug("Empty response body for %s", url)
                            return None
                        try:
                            return response.json()
                        except ValueError:
                            logger.debug("Failed to decode JSON from %s", url)
                            return None

                    logger.debug(
                        "Unexpected status %s from %s (attempt %s)",
                        response.status_code,
                        url,
                        attempt,
                    )
                except requests.RequestException as exc:
                    logger.debug(
                        "JSON request error for %s (attempt %s): %s",
                        url,
                        attempt,
                        exc,
                    )

                time.sleep(min(1.5, 0.5 * attempt))
        finally:
            session.close()

        logger.debug("Giving up on %s after %s attempts", url, max_attempts)
        return None

    def _extract_product_id(self, html: Optional[str], cms_type: str) -> Optional[str]:
        """Attempt to derive product identifier from HTML content or current URL."""

        soup = _bs()(html, "lxml") if html else None
        candidates: List[str] = []

        if soup:
            meta = soup.find("meta", attrs={"property": "product:id"}) or soup.find(
                "meta", attrs={"name": "product[id]"}
            )
            if meta:
                meta_content = meta.get("content")
                if meta_content:
                    candidates.append(meta_content)

            if cms_type == "cscart":
                node = soup.select_one("[data-ca-product-id]")
                if node and node.get("data-ca-product-id"):
                    candidates.append(node["data-ca-product-id"])

                hidden_inputs = soup.find_all("input", attrs={"name": re.compile(r"product_data\[(\d+)\]")})
                for hidden in hidden_inputs:
                    match = re.search(r"product_data\[(\d+)\]", hidden.get("name", ""))
                    if match:
                        candidates.append(match.group(1))

            if cms_type == "insales":
                node = soup.select_one("[data-product-id]")
                if node and node.get("data-product-id"):
                    candidates.append(node["data-product-id"])

                dataset = node.get("data-product") if node else None
                if dataset:
                    parsed = self._safe_json_loads(dataset)
                    if isinstance(parsed, dict):
                        pid = parsed.get("id") or parsed.get("product_id")
                        if pid:
                            candidates.append(str(pid))

                meta_insales = soup.find("meta", attrs={"name": "insales-product-id"})
                if meta_insales and meta_insales.get("content"):
                    candidates.append(meta_insales.get("content"))

            if cms_type == "cm3":
                cm3_node = soup.select_one("[data-product-id]") or soup.select_one(
                    "[data-cm3-product-id]"
                )
                if cm3_node:
                    for attr in ("data-product-id", "data-cm3-product-id"):
                        value = cm3_node.get(attr)
                        if value:
                            candidates.append(value)

                meta_cm3 = soup.find("meta", attrs={"name": re.compile("cm3", re.IGNORECASE)})
                if meta_cm3 and meta_cm3.get("content"):
                    candidates.append(meta_cm3.get("content"))

        url_source = self._current_url
        if not url_source and self.page is not None:
            try:
                url_source = self.page.url
            except Exception:
                url_source = None

        if url_source:
            url_patterns = [
                r"product_id=([\w-]+)",
                r"product/([\w-]+)",
                r"prod(?:uct)?-([\w-]+)",
                r"item/(\d+)",
            ]
            for pattern in url_patterns:
                match = re.search(pattern, url_source, re.IGNORECASE)
                if match:
                    candidates.append(match.group(1))

        for candidate in candidates:
            if candidate is None:
                continue
            value = sanitize_text(str(candidate))
            if value:
                return value

        return None

    def _parse_cscart_api(self, product_id: Optional[str], base_origin: Optional[str]) -> List[Dict]:
        """Fetch CS-Cart product variations using available API endpoints."""

        logger = logging.getLogger(__name__)

        if not product_id or not base_origin:
            logger.debug("CS-Cart API skipped due to missing product id or base URL")
            return []

        credentials = self.api_credentials.get("cscart", {}) if isinstance(self.api_credentials, dict) else {}

        env_name = "CSCART_API_KEY"
        if isinstance(credentials, dict):
            env_name = credentials.get("api_key_env", env_name)

        api_key = os.environ.get(env_name)
        if not api_key:
            logger.debug("CS-Cart API key not provided; skipping API integration")
            return []

        timeout = credentials.get("timeout", 10)
        api_pattern = credentials.get("api_url_pattern") or "/api/2.0/products/{product_id}"
        admin_pattern = credentials.get("admin_api_pattern") or "/admin.php?dispatch=products.get&product_id={product_id}"
        admin_variants_pattern = credentials.get("admin_variants_pattern")

        endpoints: List[str] = []
        for pattern in (admin_variants_pattern, api_pattern, admin_pattern):
            if not pattern:
                continue
            endpoints.append(urljoin(base_origin, pattern.format(product_id=product_id)))

        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
        }

        variations: List[Dict] = []

        def _normalize_options(option_payload: Any) -> Dict[str, Dict[str, str]]:
            option_map: Dict[str, Dict[str, str]] = {}
            if isinstance(option_payload, dict):
                iterator = option_payload.values()
            elif isinstance(option_payload, list):
                iterator = option_payload
            else:
                return option_map

            for option in iterator:
                if not isinstance(option, dict):
                    continue
                option_id = str(option.get("option_id") or option.get("id") or option.get("optionId") or "").strip()
                if not option_id:
                    continue
                label = sanitize_text(str(option.get("option_name") or option.get("name") or option.get("description") or "Option"))
                variant_map: Dict[str, str] = {}
                variant_payload = option.get("variants") or option.get("option_variants") or option.get("values")
                if isinstance(variant_payload, dict):
                    items = variant_payload.values()
                elif isinstance(variant_payload, list):
                    items = variant_payload
                else:
                    items = []
                for variant in items:
                    if isinstance(variant, dict):
                        variant_id = str(
                            variant.get("variant_id")
                            or variant.get("id")
                            or variant.get("variantId")
                            or variant.get("value_id")
                            or variant.get("value")
                            or ""
                        ).strip()
                        variant_name = sanitize_text(
                            str(
                                variant.get("variant_name")
                                or variant.get("name")
                                or variant.get("value")
                                or variant.get("description")
                                or variant_id
                            )
                        )
                        if variant_id and variant_name:
                            variant_map[variant_id] = variant_name
                if label and variant_map:
                    option_map[option_id] = {"label": label, "variants": variant_map}

            return option_map

        def _collect_variations(payload: Any) -> None:
            if not isinstance(payload, dict):
                return

            option_payload = (
                payload.get("product_options")
                or payload.get("options")
                or payload.get("product_options_data")
                or payload.get("productOptions")
            )
            option_map = _normalize_options(option_payload)

            combinations = (
                payload.get("option_combinations")
                or payload.get("combinations")
                or payload.get("product_options_inventory")
                or payload.get("inventory")
            )

            if isinstance(combinations, dict):
                combination_iterable = combinations.values()
            elif isinstance(combinations, list):
                combination_iterable = combinations
            else:
                combination_iterable = []

            for combination in combination_iterable:
                if not isinstance(combination, dict):
                    continue

                raw_mapping = combination.get("combination") or combination.get("variation") or combination.get("options")
                mapping: Dict[str, str] = {}
                if isinstance(raw_mapping, dict):
                    mapping = {str(k): str(v) for k, v in raw_mapping.items()}
                elif isinstance(raw_mapping, list):
                    for item in raw_mapping:
                        if isinstance(item, dict):
                            opt_id = str(item.get("option_id") or item.get("optionId") or item.get("id") or "").strip()
                            var_id = str(item.get("variant_id") or item.get("variantId") or item.get("value") or "").strip()
                            if opt_id and var_id:
                                mapping[opt_id] = var_id

                attributes: Dict[str, str] = {}
                for opt_id, var_id in mapping.items():
                    option_info = option_map.get(opt_id)
                    if not option_info:
                        continue
                    variant_name = option_info["variants"].get(var_id) or option_info["variants"].get(str(var_id))
                    if variant_name:
                        attributes[option_info["label"]] = variant_name

                price_candidates = [
                    combination.get("price"),
                    combination.get("price_calc"),
                    combination.get("modifier"),
                    combination.get("combination_price"),
                    payload.get("price"),
                    payload.get("list_price"),
                    payload.get("base_price"),
                ]
                price_value: Optional[float] = None
                for candidate in price_candidates:
                    if candidate is None:
                        continue
                    price_value = clean_price(str(candidate))
                    if price_value is not None:
                        break

                if price_value is None:
                    continue

                stock_raw = (
                    combination.get("amount")
                    or combination.get("quantity")
                    or combination.get("inventory")
                    or combination.get("in_stock")
                )
                if isinstance(stock_raw, (int, float)):
                    stock_value: Optional[int] = int(stock_raw)
                elif stock_raw is not None:
                    stock_value = parse_stock(str(stock_raw))
                else:
                    stock_value = None

                attribute_values = list(attributes.values())
                attribute_labels = list(attributes.keys())
                combined_value = " / ".join(attribute_values) if attribute_values else sanitize_text(str(combination.get("combination_hash") or combination.get("product_code") or combination.get("sku") or ""))
                primary_label = attribute_labels[0] if attribute_labels else "Variant"

                classification = self.classify_variation_type(primary_label, combined_value)
                display_name = self.format_variation_display_name(classification["type"], combined_value)

                variation = {
                    "type": classification["type"],
                    "value": combined_value,
                    "price": float(price_value),
                    "stock": stock_value,
                    "sku": sanitize_text(str(combination.get("product_code") or combination.get("sku") or combination.get("code") or "")),
                    "variant_id": str(
                        combination.get("combination_id")
                        or combination.get("inventory_id")
                        or combination.get("id")
                        or ""
                    ),
                    "display_name": display_name,
                    "sort_order": combination.get("position", 999),
                    "category": classification["category"],
                    "confidence_score": classification["confidence"],
                    "attributes": attributes,
                }

                validated = self.validate_variation_data(variation)
                if validated:
                    variations.append(validated)

            if not variations and isinstance(payload.get("variants"), list):
                for variant in payload.get("variants", []):
                    if not isinstance(variant, dict):
                        continue
                    price_value = clean_price(str(variant.get("price") or variant.get("base_price") or variant.get("display_price")))
                    if price_value is None:
                        continue
                    stock_raw = variant.get("amount") or variant.get("quantity") or variant.get("inventory")
                    if isinstance(stock_raw, (int, float)):
                        stock_value = int(stock_raw)
                    elif stock_raw is not None:
                        stock_value = parse_stock(str(stock_raw))
                    else:
                        stock_value = None

                    attributes = {}
                    options_payload = variant.get("options") or variant.get("attributes") or []
                    if isinstance(options_payload, dict):
                        for key, value in options_payload.items():
                            if value is not None:
                                attributes[sanitize_text(str(key))] = sanitize_text(str(value))
                    elif isinstance(options_payload, list):
                        for option in options_payload:
                            if not isinstance(option, dict):
                                continue
                            label = sanitize_text(str(option.get("option_name") or option.get("name") or option.get("title") or "Option"))
                            value = sanitize_text(
                                str(option.get("variant_name") or option.get("value") or option.get("title") or ""))
                            if label and value:
                                attributes[label] = value

                    attribute_values = list(attributes.values())
                    combined_value = " / ".join(attribute_values) if attribute_values else sanitize_text(str(variant.get("variant_name") or variant.get("name") or variant.get("title") or ""))
                    primary_label = list(attributes.keys())[0] if attributes else "Variant"
                    classification = self.classify_variation_type(primary_label, combined_value)
                    display_name = self.format_variation_display_name(classification["type"], combined_value)

                    variation = {
                        "type": classification["type"],
                        "value": combined_value,
                        "price": float(price_value),
                        "stock": stock_value,
                        "sku": sanitize_text(str(variant.get("product_code") or variant.get("sku") or variant.get("code") or "")),
                        "variant_id": str(variant.get("variant_id") or variant.get("id") or ""),
                        "display_name": display_name,
                        "sort_order": variant.get("position", 999),
                        "category": classification["category"],
                        "confidence_score": classification["confidence"],
                        "attributes": attributes,
                    }

                    validated = self.validate_variation_data(variation)
                    if validated:
                        variations.append(validated)

        for endpoint in endpoints:
            payload = self._http_get_json(endpoint, headers=headers, timeout=timeout)
            if payload is None and "api_key" not in endpoint:
                payload = self._http_get_json(
                    f"{endpoint}{'&' if '?' in endpoint else '?'}api_key={api_key}",
                    headers={"Accept": "application/json"},
                    timeout=timeout,
                )

            if payload:
                if isinstance(payload, list):
                    for item in payload:
                        _collect_variations(item)
                else:
                    _collect_variations(payload)

            if variations:
                break

        if not variations:
            logger.debug("CS-Cart API did not return variation data for product %s", product_id)

        return variations

    def _parse_insales_admin_api(
        self, product_id: Optional[str], shop_origin: Optional[str]
    ) -> List[Dict]:
        """Retrieve variant details from the InSales Admin API."""

        logger = logging.getLogger(__name__)

        if not product_id or not shop_origin:
            logger.debug("InSales Admin API skipped due to missing product id or origin")
            return []

        credentials = self.api_credentials.get("insales", {}) if isinstance(self.api_credentials, dict) else {}
        timeout = credentials.get("timeout", 10)
        api_pattern = credentials.get("api_url_pattern") or "/admin/products/{product_id}/variants.json"
        endpoint = urljoin(shop_origin, api_pattern.format(product_id=product_id))

        env_name = "INSALES_API_KEY"
        if isinstance(credentials, dict):
            env_name = credentials.get("api_key_env", env_name)

        api_key = os.environ.get(env_name)
        if not api_key:
            logger.debug("InSales API key not provided; skipping Admin API call")
            return []

        headers = {
            "Accept": "application/json",
            "X-Shop-Api-Key": api_key,
        }

        payload = self._http_get_json(endpoint, headers=headers, timeout=timeout)
        if payload is None:
            logger.debug("InSales Admin API returned no payload for product %s", product_id)
            return []

        variants_payload: Any
        if isinstance(payload, list):
            variants_payload = payload
        elif isinstance(payload, dict):
            variants_payload = payload.get("variants") or payload.get("data") or payload.get("items")
        else:
            variants_payload = None

        if not isinstance(variants_payload, list):
            logger.debug("InSales Admin API payload missing variants list for %s", product_id)
            return []

        variations: List[Dict] = []

        for variant in variants_payload:
            if not isinstance(variant, dict):
                continue

            option_names = variant.get("option_names")
            attributes: Dict[str, str] = {}
            if isinstance(option_names, list):
                for option in option_names:
                    if isinstance(option, dict):
                        label = option.get("option_name") or option.get("title") or option.get("name")
                        value = option.get("value") or option.get("option_value") or option.get("title")
                    else:
                        label = None
                        value = option
                    if label and value:
                        attributes[sanitize_text(str(label))] = sanitize_text(str(value))
            else:
                for field in ("option1", "option2", "option3"):
                    value = variant.get(field)
                    if value:
                        attributes[f"Option {field[-1]}"] = sanitize_text(str(value))

            price_raw = variant.get("price") or variant.get("base_price") or variant.get("sale_price")
            price_value = clean_price(str(price_raw)) if price_raw is not None else None
            if price_value is None:
                continue

            stock_raw = (
                variant.get("inventory_quantity")
                or variant.get("quantity")
                or variant.get("available")
                or variant.get("stock")
            )
            if isinstance(stock_raw, (int, float)):
                stock_value: Optional[int] = int(stock_raw)
            elif stock_raw is not None:
                stock_value = parse_stock(str(stock_raw))
            else:
                stock_value = None

            attribute_values = list(attributes.values())
            display_value = " / ".join(attribute_values) if attribute_values else sanitize_text(
                str(variant.get("title") or variant.get("name") or variant.get("sku") or product_id)
            )
            attribute_labels = list(attributes.keys())
            label_text = attribute_labels[0] if attribute_labels else "Variant"

            classification = self.classify_variation_type(label_text, display_value)
            display_name = self.format_variation_display_name(classification["type"], display_value)

            variation = {
                "type": classification["type"],
                "value": display_value,
                "price": float(price_value),
                "stock": stock_value,
                "sku": sanitize_text(str(variant.get("sku") or variant.get("barcode") or variant.get("article") or "")),
                "variant_id": str(variant.get("id") or variant.get("ID") or ""),
                "display_name": display_name,
                "sort_order": variant.get("position", 999),
                "category": classification["category"],
                "confidence_score": classification["confidence"],
                "attributes": attributes,
            }

            availability_flag = variant.get("available")
            if isinstance(availability_flag, bool):
                variation["in_stock"] = availability_flag

            validated = self.validate_variation_data(variation)
            if validated:
                variations.append(validated)

        if not variations:
            logger.debug("InSales Admin API did not yield validated variations for %s", product_id)

        return variations

    def _parse_cm3_json(self, script_content: str) -> List[Dict]:
        """Parse CM3-specific JSON payloads for product variations."""

        variations: List[Dict] = []

        anchors = [
            "CM3ProductData",
            "cm3Product",
            "cm3Variants",
            "cm3Config",
            "cm3Data",
        ]

        blocks = self._extract_json_blocks(script_content, anchors)

        payloads: List[Any] = []
        if blocks:
            for block in blocks:
                parsed = self._safe_json_loads(block)
                if parsed is not None:
                    payloads.append(parsed)
        else:
            direct = self._safe_json_loads(script_content)
            if direct is not None:
                payloads.append(direct)

        if not payloads:
            return variations

        def _register_variation(variant: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> None:
            if not isinstance(variant, dict):
                return

            price_candidates = [
                variant.get("price"),
                variant.get("price_value"),
                variant.get("priceValue"),
                variant.get("base_price"),
            ]
            if context:
                price_candidates.extend(
                    [context.get("price"), context.get("base_price"), context.get("original_price")]
                )

            price_value: Optional[float] = None
            for candidate in price_candidates:
                if candidate is None:
                    continue
                price_value = clean_price(str(candidate))
                if price_value is not None:
                    break

            if price_value is None:
                return

            stock_raw = (
                variant.get("stock")
                or variant.get("quantity")
                or variant.get("balance")
                or variant.get("available")
                or variant.get("in_stock")
            )
            if isinstance(stock_raw, (int, float)):
                stock_value: Optional[int] = int(stock_raw)
            elif stock_raw is not None:
                stock_value = parse_stock(str(stock_raw))
            else:
                stock_value = None

            attributes: Dict[str, str] = {}
            options_payload = (
                variant.get("options")
                or variant.get("attributes")
                or variant.get("properties")
                or variant.get("params")
            )
            if isinstance(options_payload, dict):
                for key, value in options_payload.items():
                    if value is not None:
                        attributes[sanitize_text(str(key))] = sanitize_text(str(value))
            elif isinstance(options_payload, list):
                for option in options_payload:
                    if not isinstance(option, dict):
                        continue
                    label = sanitize_text(str(option.get("name") or option.get("title") or option.get("label") or "Option"))
                    value = sanitize_text(str(option.get("value") or option.get("title") or option.get("label") or ""))
                    if label and value:
                        attributes[label] = value

            attribute_values = list(attributes.values())
            display_value = " / ".join(attribute_values) if attribute_values else sanitize_text(
                str(variant.get("name") or variant.get("title") or variant.get("id") or "Variant")
            )
            attribute_labels = list(attributes.keys())
            label_text = attribute_labels[0] if attribute_labels else sanitize_text(
                str(variant.get("option_name") or variant.get("group") or "Variant")
            )

            classification = self.classify_variation_type(label_text, display_value)
            display_name = self.format_variation_display_name(classification["type"], display_value)

            variation = {
                "type": classification["type"],
                "value": display_value,
                "price": float(price_value),
                "stock": stock_value,
                "sku": sanitize_text(str(variant.get("sku") or variant.get("article") or variant.get("code") or "")),
                "variant_id": str(variant.get("id") or variant.get("variant_id") or ""),
                "display_name": display_name,
                "sort_order": variant.get("position", 999),
                "category": classification["category"],
                "confidence_score": classification["confidence"],
                "attributes": attributes,
            }

            validated = self.validate_variation_data(variation)
            if validated:
                variations.append(validated)

        def _walk(payload: Any, context: Optional[Dict[str, Any]] = None) -> None:
            if isinstance(payload, dict):
                if isinstance(payload.get("variants"), list):
                    for item in payload["variants"]:
                        _register_variation(item, payload)
                for key in ("items", "data", "products", "product", "offers"):
                    nested = payload.get(key)
                    if isinstance(nested, dict):
                        _walk(nested, payload)
                    elif isinstance(nested, list):
                        for entry in nested:
                            _walk(entry, payload)
            elif isinstance(payload, list):
                for entry in payload:
                    _walk(entry, context)

        for payload in payloads:
            _walk(payload)

        return variations

    def _parse_shop2_init(self, script_content: str) -> Dict[str, Dict[str, str]]:
        """Extract attribute hints from shop2.init configuration blobs."""

        mapping: Dict[str, Dict[str, str]] = {}

        if not script_content or "shop2.init" not in script_content:
            return mapping

        match = re.search(r"shop2\\.init\\((\{.*?\})\\);", script_content, re.S)
        if not match:
            return mapping

        payload = match.group(1)
        data = self._safe_json_loads(payload)

        if not isinstance(data, dict):
            return mapping

        product_refs = data.get("productRefs")
        if not isinstance(product_refs, dict):
            return mapping

        for ref in product_refs.values():
            if not isinstance(ref, dict):
                continue

            for attribute_name, combinations in ref.items():
                if not isinstance(combinations, dict):
                    continue

                label = sanitize_text(str(attribute_name))
                if not label:
                    continue

                for raw_value, kind_ids in combinations.items():
                    if not isinstance(kind_ids, list):
                        continue

                    value_text = sanitize_text(str(raw_value))
                    if not value_text or value_text.isdigit():
                        continue

                    for kind_id in kind_ids:
                        variant_key = sanitize_text(str(kind_id))
                        if not variant_key:
                            continue
                        attributes = mapping.setdefault(variant_key, {})
                        attributes[label] = value_text

        return mapping

    def _parse_shop2_variations_from_dom(
        self,
        soup: Optional[BeautifulSoup],
        variant_attributes: Optional[Dict[str, Dict[str, str]]],
    ) -> List[Dict]:
        """Parse variation data from Shop2 HTML structures."""

        logger = logging.getLogger(__name__)
        variations: List[Dict] = []

        if soup is None:
            return variations

        forms = soup.select("form.kind-item__form")
        if not forms:
            forms = soup.select(".kinds-block .kind-item form, .mods_block .kind-item form")
        if not forms:
            return variations

        currency_override = self._resolve_currency_override(self._current_url)

        for sort_order, form in enumerate(forms):
            kind_id_input = form.select_one("input[name='kind_id']")
            kind_id = sanitize_text(kind_id_input.get("value", "")) if kind_id_input else ""

            price_node = form.select_one(
                ".kind-price strong, .price-current strong, .kind-price .price strong"
            )
            price_value = clean_price(price_node.get_text()) if price_node else None
            if price_value is None:
                logger.debug("Skipping Shop2 variation without price")
                continue

            currency_node = form.select_one(
                ".kind-price span, .price-current span, .kind-price .price span"
            )
            currency = sanitize_text(currency_node.get_text()) if currency_node else None
            if not currency and currency_override:
                currency = currency_override

            attributes: Dict[str, str] = {}
            if variant_attributes and kind_id and kind_id in variant_attributes:
                attributes.update(variant_attributes[kind_id])

            meta_input = form.select_one("input[name='meta']")
            if meta_input and meta_input.get("value"):
                meta_raw = html.unescape(meta_input.get("value", ""))
                meta_data = self._safe_json_loads(meta_raw)
                if isinstance(meta_data, dict):
                    for key, value in meta_data.items():
                        label = sanitize_text(str(key))
                        if not label:
                            continue
                        if isinstance(value, list):
                            parts = [sanitize_text(str(item)) for item in value if str(item).strip()]
                            value_text = ", ".join(part for part in parts if part)
                        else:
                            value_text = sanitize_text(str(value))
                        if value_text and not value_text.isdigit():
                            attributes[label] = value_text

            amount_input = form.select_one("input[name='amount']")
            if amount_input:
                minimum = sanitize_text(amount_input.get("data-min", ""))
                step = sanitize_text(amount_input.get("data-multiplicity", ""))
                if minimum:
                    attributes.setdefault("min_quantity", minimum)
                if step:
                    attributes.setdefault("step_quantity", step)

            article_node = form.select_one(".shop2-product-article")
            sku = ""
            if article_node:
                article_text = sanitize_text(article_node.get_text(separator=" "))
                if article_text:
                    parts = article_text.split(":", 1)
                    candidate = parts[1] if len(parts) == 2 else article_text
                    sku = sanitize_text(candidate)
                    if sku:
                        attributes.setdefault("article", sku)

            name_node = form.select_one(".kind-name") or form.select_one(".kind-name a")
            variant_label = sanitize_text(name_node.get_text()) if name_node else ""

            if not variant_label:
                variant_label = attributes.get("name") or attributes.get("Название") or kind_id

            button = form.select_one(".shop-product-btn")
            in_stock = bool(button and not button.has_attr("disabled") and "disabled" not in button.get("class", []))

            stock_value = None if in_stock else 0

            label_text = variant_label or "Вариант"
            display_value = variant_label or kind_id or sku
            classification = self.classify_variation_type(label_text, display_value)
            display_name = self.format_variation_display_name(
                classification["type"],
                display_value,
            )

            variation = {
                "type": classification["type"],
                "value": display_value,
                "option_id": kind_id or None,
                "price": float(price_value) if price_value is not None else None,
                "stock": stock_value,
                "in_stock": in_stock,
                "display_name": display_name,
                "sort_order": sort_order,
                "category": classification["category"],
                "confidence_score": classification["confidence"],
                "attributes": attributes,
            }

            if sku:
                variation["sku"] = sku
            if kind_id:
                variation["variant_id"] = kind_id
            if currency:
                variation["currency"] = currency.upper()

            validated = self.validate_variation_data(variation)
            if validated:
                variations.append(validated)

        if variations:
            logger.debug("Extracted %d variations from Shop2 HTML", len(variations))

        return self._deduplicate_variations(variations)

    def _safe_json_loads(self, payload: str) -> Optional[Any]:
        """Attempt to parse JSON payload with fallbacks for single-quoted Bitrix blobs."""
        if not payload:
            return None

        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            pass

        sanitized = payload.strip()
        sanitized = re.sub(r",(\s*[}\]])", r"\1", sanitized)
        try:
            return json.loads(sanitized)
        except json.JSONDecodeError:
            allow_literal = bool(sanitized) and sanitized[0] in "[{"
            if allow_literal and not re.search(r"\b(function|new)\b", sanitized, re.IGNORECASE) and "=>" not in sanitized:
                try:
                    return ast.literal_eval(sanitized)
                except (ValueError, SyntaxError):
                    logging.getLogger(__name__).debug("Failed to decode JSON payload")
                    return None
            logging.getLogger(__name__).debug("Failed to decode JSON payload")
            return None

    def _extract_json_blocks(self, text: str, anchors: List[str]) -> List[str]:
        blocks: List[str] = []
        if not text:
            return blocks

        if not anchors:
            return blocks

        escaped = [re.escape(anchor) for anchor in anchors if anchor]
        if not escaped:
            return blocks

        pattern = re.compile(
            r"(?:^|[;,{]\s*|\s+)(?:var|let|const)?\s*(?P<anchor>" + "|".join(escaped) + r")\b\s*=\s*",
            re.IGNORECASE | re.MULTILINE,
        )

        for match in pattern.finditer(text):
            open_idx = self._find_first_bracket(text, match.end())
            if open_idx == -1:
                continue
            close_idx = self._find_closing_bracket(text, open_idx)
            if close_idx == -1:
                continue
            blocks.append(text[open_idx : close_idx + 1])
        return blocks

    def _extract_jccatalogelement_blocks(self, text: str) -> List[str]:
        """Extract JSON payloads passed into Bitrix JCCatalogElement constructors."""
        if not text:
            return []

        blocks: List[str] = []
        pattern = re.compile(r"new\s+JCCatalog(?:Element|Section)\s*\(", re.IGNORECASE)

        for match in pattern.finditer(text):
            start = match.end()
            open_idx = self._find_first_bracket(text, start)
            if open_idx == -1:
                continue
            close_idx = self._find_closing_bracket(text, open_idx)
            if close_idx == -1:
                continue
            blocks.append(text[open_idx : close_idx + 1])

        return blocks

    def _find_first_bracket(self, text: str, idx: int) -> int:
        while idx < len(text):
            ch = text[idx]
            if ch in "[{":
                return idx
            if not ch.isspace():
                return -1
            idx += 1
        return -1

    def _extract_bitrix_price(self, offer: Dict[str, Any]) -> Tuple[Optional[float], Optional[str]]:
        """Extract normalized price and currency information from Bitrix offer payloads."""

        price_candidates: List[Any] = []
        currency: Optional[str] = None

        for key in (
            "PRICE",
            "MIN_PRICE",
            "BASE_PRICE",
            "price",
            "ITEM_PRICE",
        ):
            value = offer.get(key)
            if value is not None:
                price_candidates.append(value)

        item_prices = offer.get("ITEM_PRICES") or offer.get("ITEM_PRICE_DATA")
        if isinstance(item_prices, list):
            price_candidates.extend(item_prices)
        elif isinstance(item_prices, dict):
            price_candidates.append(item_prices)

        product_info = offer.get("PRODUCT")
        if isinstance(product_info, dict):
            for nested_key in ("MIN_PRICE", "PRICE", "PRICE_VALUE"):
                nested = product_info.get(nested_key)
                if nested is not None:
                    price_candidates.append(nested)

        def _try_normalize(candidate: Any) -> Tuple[Optional[float], Optional[str]]:
            local_currency: Optional[str] = None
            if isinstance(candidate, dict):
                local_currency = candidate.get("CURRENCY") or candidate.get("currency")
                for price_key in (
                    "DISCOUNT_VALUE",
                    "DISCOUNT_PRICE",
                    "RATIO_DISCOUNT_PRICE",
                    "VALUE",
                    "PRICE",
                    "BASE_PRICE",
                    "RATIO_PRICE",
                    "PRICE_WITHOUT_DISCOUNT",
                ):
                    raw = candidate.get(price_key)
                    if raw is None:
                        continue
                    cleaned = clean_price(str(raw))
                    if cleaned is not None:
                        return float(cleaned), local_currency
            else:
                cleaned = clean_price(str(candidate))
                if cleaned is not None:
                    return float(cleaned), local_currency
            return None, local_currency

        for candidate in price_candidates:
            price_value, local_currency = _try_normalize(candidate)
            if price_value is not None:
                if local_currency:
                    currency = local_currency
                return price_value, currency
            if local_currency and not currency:
                currency = local_currency

        return None, currency

    def _extract_bitrix_stock(self, offer: Dict[str, Any]) -> Optional[int]:
        """Derive stock level from Bitrix offer structures, handling nested payloads."""

        quantity_fields = (
            "QUANTITY",
            "STOCK",
            "QUANTITY_RESERVED",
            "IN_STOCK",
            "AVAILABLE",
            "TOTAL_QUANTITY",
        )
        for key in quantity_fields:
            raw = offer.get(key)
            if raw is None:
                continue
            if isinstance(raw, str) and raw.strip().upper() == "Y":
                return 1
            if isinstance(raw, (int, float)):
                return max(int(raw), 0)
            parsed = parse_stock(str(raw))
            if parsed is not None:
                return max(parsed, 0)

        product_info = offer.get("PRODUCT")
        if isinstance(product_info, dict):
            for key in ("QUANTITY", "AVAILABLE", "QUANTITY_AVAILABLE", "STOCK"):
                raw = product_info.get(key)
                if raw is None:
                    continue
                if isinstance(raw, str) and raw.strip().upper() == "Y":
                    return 1
                if isinstance(raw, (int, float)):
                    return max(int(raw), 0)
                parsed = parse_stock(str(raw))
                if parsed is not None:
                    return max(parsed, 0)

            can_buy = product_info.get("CAN_BUY")
            if isinstance(can_buy, str):
                if can_buy.upper() == "N":
                    return 0
            elif can_buy is False:
                return 0

        can_buy_flag = offer.get("CAN_BUY")
        if isinstance(can_buy_flag, str) and can_buy_flag.upper() == "N":
            return 0
        if can_buy_flag is False:
            return 0

        return None

    def _extract_bitrix_attributes(self, offer: Dict[str, Any]) -> Dict[str, str]:
        """Collect variation attributes from Bitrix offer payloads."""

        attributes: Dict[str, str] = {}

        def _register(label: Any, value: Any) -> None:
            if not label or not value:
                return
            label_text = sanitize_text(str(label))
            value_text = sanitize_text(str(value))
            if label_text and value_text:
                attributes[label_text] = value_text

        display_props = offer.get("DISPLAY_PROPERTIES") or {}
        if isinstance(display_props, dict):
            for prop in display_props.values():
                if not isinstance(prop, dict):
                    continue
                name = prop.get("NAME") or prop.get("title") or prop.get("CODE")
                value = prop.get("DISPLAY_VALUE") or prop.get("VALUE") or prop.get("NAME")
                if isinstance(value, list):
                    value = value[0]
                _register(name, value)

        if not attributes:
            tree = offer.get("TREE") or offer.get("tree")
            if isinstance(tree, dict):
                for key, val in tree.items():
                    if isinstance(val, dict):
                        label = val.get("NAME") or key
                        value = val.get("VALUE") or val.get("VALUE_ID") or val.get("NAME")
                    else:
                        label = key
                        value = val
                    _register(label, value)

        if not attributes:
            properties = offer.get("PROPERTIES") or offer.get("PROPS")
            if isinstance(properties, dict):
                for prop in properties.values():
                    if isinstance(prop, dict):
                        _register(prop.get("NAME") or prop.get("CODE"), prop.get("VALUE"))

        if not attributes:
            sku_props = offer.get("SKU_PROPS") or offer.get("SKU_TREE")
            if isinstance(sku_props, dict):
                for key, value in sku_props.items():
                    if isinstance(value, dict):
                        _register(value.get("NAME") or key, value.get("VALUE"))
                    else:
                        _register(key, value)

        return attributes

    def _find_closing_bracket(self, text: str, start_idx: int) -> int:
        if start_idx >= len(text):
            return -1

        opening = text[start_idx]
        closing = "}" if opening == "{" else "]"
        depth = 0
        in_string = False
        string_char = ""
        escape = False

        for pos in range(start_idx, len(text)):
            ch = text[pos]

            if in_string:
                if escape:
                    escape = False
                    continue
                if ch == "\\":
                    escape = True
                    continue
                if ch == string_char:
                    in_string = False
                continue

            if ch in ('"', "'"):
                in_string = True
                string_char = ch
                continue

            if ch == opening:
                depth += 1
            elif ch == closing:
                depth -= 1
                if depth == 0:
                    return pos

        return -1

    def _parse_bitrix_json(self, script_content: str) -> List[Dict]:
        variations: List[Dict] = []
        logger = logging.getLogger(__name__)

        discovery_start = time.perf_counter()
        anchors = [
            "offers",
            "offersData",
            "OFFER_DATA",
            "offersJson",
            "OFFERS_DATA",
            "OFFERS",
        ]

        json_blocks = list(self._extract_json_blocks(script_content, anchors))
        json_blocks.extend(self._extract_jccatalogelement_blocks(script_content))

        MAX_BLOCKS = 12
        MAX_BLOCK_SIZE = 1_500_000  # ~1.4 MB
        cache_hits = 0
        skipped_large = 0
        selected_blocks: List[Tuple[str, str]] = []
        seen_hashes: Set[str] = set()

        for block in json_blocks:
            if len(selected_blocks) >= MAX_BLOCKS:
                break
            if not isinstance(block, str):
                continue
            if len(block) > MAX_BLOCK_SIZE:
                skipped_large += 1
                logger.debug(
                    "Skipping Bitrix JSON block (%.1f KB) exceeding limit",
                    len(block) / 1024,
                )
                continue
            digest = hashlib.sha1(block.encode("utf-8", "ignore")).hexdigest()
            if digest in seen_hashes:
                continue
            seen_hashes.add(digest)
            cached = self._bitrix_json_cache.get(digest)
            if cached is not None:
                variations.extend(copy.deepcopy(cached))
                cache_hits += 1
                continue
            selected_blocks.append((block, digest))

        if len(json_blocks) > MAX_BLOCKS:
            logger.debug(
                "Truncated Bitrix JSON blocks from %d to %d for performance",
                len(json_blocks),
                len(selected_blocks),
            )

        if skipped_large:
            logger.info(
                "Skipped %d oversized Bitrix JSON blocks (limit %.1f KB)",
                skipped_large,
                MAX_BLOCK_SIZE / 1024,
            )

        discovery_elapsed = time.perf_counter() - discovery_start

        parse_timings: List[float] = []
        build_timings: List[float] = []

        for block, digest in selected_blocks:
            parse_start = time.perf_counter()
            data = self._safe_json_loads(block)
            parse_timings.append(time.perf_counter() - parse_start)
            if data is None:
                build_timings.append(0.0)
                self._bitrix_json_cache[digest] = []
                continue

            offers: List[Dict[str, Any]] = []
            if isinstance(data, list):
                offers = [item for item in data if isinstance(item, dict)]
            elif isinstance(data, dict):
                for key in (
                    "offers",
                    "OFFERS",
                    "ITEMS",
                    "items",
                    "RESULT",
                    "OFFER",
                    "OFFERS_DATA",
                ):
                    maybe = data.get(key)
                    if isinstance(maybe, list):
                        offers = [item for item in maybe if isinstance(item, dict)]
                        break
                    if isinstance(maybe, dict):
                        dict_candidates = [item for item in maybe.values() if isinstance(item, dict)]
                        if dict_candidates:
                            offers = dict_candidates
                            break
                if not offers and "PRODUCT" in data and isinstance(data.get("PRODUCT"), dict):
                    offers = [value for value in data.get("PRODUCT", {}).get("OFFERS", []) if isinstance(value, dict)]
                if not offers and all(isinstance(value, dict) for value in data.values()):
                    flattened: List[Dict[str, Any]] = []
                    for value in data.values():
                        if isinstance(value, dict):
                            nested_values = [item for item in value.values() if isinstance(item, dict)]
                            if nested_values:
                                flattened.extend(nested_values)
                            else:
                                flattened.append(value)
                    if flattened:
                        offers = flattened

            build_start = time.perf_counter()
            block_variations: List[Dict[str, Any]] = []
            for offer in offers:
                variation = self._build_bitrix_variation(offer)
                if variation:
                    variations.append(variation)
                    block_variations.append(variation)
            build_timings.append(time.perf_counter() - build_start)
            self._bitrix_json_cache[digest] = [copy.deepcopy(v) for v in block_variations]

        max_parse = max(parse_timings, default=0.0)
        max_build = max(build_timings, default=0.0)
        if any(duration > 0.2 for duration in (discovery_elapsed, max_parse, max_build)):
            logger.info(
                (
                    "Bitrix parsing slow: blocks=%d processed=%d cache_hits=%d "
                    "discovery=%.1fms max_parse=%.1fms max_build=%.1fms variations=%d"
                ),
                len(json_blocks),
                len(selected_blocks),
                cache_hits,
                discovery_elapsed * 1000,
                max_parse * 1000,
                max_build * 1000,
                len(variations),
            )
        else:
            logger.debug(
                "Bitrix parser extracted %d variations from %d JSON blocks",
                len(variations),
                len(json_blocks),
            )
        return variations

    def _build_bitrix_variation(self, offer: Dict[str, Any]) -> Optional[Dict]:
        if not isinstance(offer, dict):
            return None

        logger = logging.getLogger(__name__)

        price_value, currency = self._extract_bitrix_price(offer)
        if price_value is None:
            logger.debug(
                "Skipping Bitrix offer %s due to missing price",
                offer.get("ID") or offer.get("NAME"),
            )
            return None

        stock_value = self._extract_bitrix_stock(offer)
        attributes = self._extract_bitrix_attributes(offer)

        value_components = [value for value in attributes.values() if value]
        value_text = (
            " / ".join(value_components)
            if value_components
            else sanitize_text(str(offer.get("NAME", "")))
        )
        label_text = (
            " / ".join(attributes.keys())
            if attributes
            else offer.get("PROPERTY_NAME", "Offer")
        )
        classification = self.classify_variation_type(str(label_text or "Offer"), str(value_text or ""))
        display_name = self.format_variation_display_name(classification["type"], value_text or "")

        variation = {
            "type": classification["type"],
            "value": value_text or (offer.get("NAME") or ""),
            "price": price_value,
            "stock": stock_value,
            "sku": sanitize_text(str(offer.get("SKU") or offer.get("ARTNUMBER") or offer.get("CODE") or offer.get("XML_ID") or offer.get("ID") or "")),
            "variant_id": str(offer.get("ID") or offer.get("OFFER_ID") or offer.get("ID") or ""),
            "display_name": display_name,
            "sort_order": offer.get("SORT") or offer.get("SORT_ORDER") or 999,
            "category": classification["category"],
            "confidence_score": classification["confidence"],
            "attributes": attributes,
        }

        if currency:
            variation["currency"] = sanitize_text(currency)
        else:
            override_currency = self._resolve_currency_override(
                offer.get("DETAIL_PAGE_URL")
                or offer.get("URL")
                or self._current_url
            )
            if override_currency:
                variation["currency"] = sanitize_text(override_currency)

        url = offer.get("DETAIL_PAGE_URL") or offer.get("URL") or self._current_url
        if url:
            variation["url"] = url

        return self.validate_variation_data(variation)

    def _parse_insales_json(self, script_content: str) -> List[Dict]:
        variations: List[Dict] = []

        direct = self._safe_json_loads(script_content)
        if direct:
            variations.extend(self._parse_insales_variants(direct))

        data_candidates = self._extract_json_blocks(
            script_content, ["product", "variants", "InSales"]
        )

        for block in data_candidates:
            parsed = self._safe_json_loads(block)
            if not parsed:
                continue
            variations.extend(self._parse_insales_variants(parsed))

        return variations

    def _parse_insales_variants(self, data: Any) -> List[Dict]:
        variations: List[Dict] = []
        if isinstance(data, dict):
            variants = None
            for key in ("variants", "VARIANTS", "items"):
                maybe = data.get(key)
                if isinstance(maybe, list):
                    variants = maybe
                    break
            if variants is None and "product" in data and isinstance(data["product"], dict):
                product_variants = data["product"].get("variants")
                if isinstance(product_variants, list):
                    variants = product_variants
            if variants:
                for variant in variants:
                    built = self._build_insales_variation(variant)
                    if built:
                        variations.append(built)
        elif isinstance(data, list):
            for item in data:
                built = self._build_insales_variation(item)
                if built:
                    variations.append(built)
        return variations

    def _build_insales_variation(self, variant: Any) -> Optional[Dict]:
        if not isinstance(variant, dict):
            return None

        raw_price = variant.get("price") or variant.get("price_in_currency") or variant.get("selling_price")
        price_value = clean_price(str(raw_price)) if raw_price is not None else None
        if price_value is None:
            return None

        stock_fields = ("inventory_quantity", "quantity", "available", "stock", "remains")
        stock_value: Optional[int] = None
        in_stock = None
        for key in stock_fields:
            raw = variant.get(key)
            if raw is None:
                continue
            if isinstance(raw, bool):
                in_stock = raw
                stock_value = 1 if raw else 0
                break
            if isinstance(raw, (int, float)):
                stock_value = int(raw)
                in_stock = stock_value > 0
                break
            parsed = parse_stock(str(raw))
            if parsed is not None:
                stock_value = parsed
                in_stock = parsed > 0
                break

        attributes: Dict[str, str] = {}
        for attr_key in ("option1", "option2", "option3"):
            value = variant.get(attr_key)
            if value:
                label = attr_key.replace("option", "Option ")
                attributes[label] = sanitize_text(str(value))

        title = variant.get("title") or " ".join(v for v in attributes.values() if v)
        classification = self.classify_variation_type(" / ".join(attributes.keys()), title)
        display_name = self.format_variation_display_name(classification["type"], title or "")

        variation = {
            "type": classification["type"],
            "value": title,
            "price": float(price_value),
            "stock": stock_value,
            "sku": variant.get("sku") or variant.get("barcode"),
            "variant_id": str(variant.get("id") or variant.get("ID") or ""),
            "display_name": display_name,
            "sort_order": variant.get("position", 999),
            "category": classification["category"],
            "confidence_score": classification["confidence"],
            "attributes": attributes,
        }

        if in_stock is not None:
            variation["in_stock"] = bool(in_stock)

        return self.validate_variation_data(variation)

    def _parse_shopify_json(self, script_content: str) -> List[Dict]:
        """Parse Shopify product JSON for variations."""
        variations = []
        try:
            # Look for product JSON in Shopify format
            if "variants" in script_content:
                import json
                # Try to find JSON object
                json_match = re.search(r'\{.*?"variants".*?\}', script_content, re.DOTALL)
                if json_match:
                    product_data = json.loads(json_match.group())
                    variants = product_data.get("variants", [])

                    for variant in variants:
                        variation = {
                            "type": "variant",
                            "value": variant.get("title", ""),
                            "price": float(variant.get("price", 0)) / 100,  # Shopify uses cents
                            "stock": variant.get("inventory_quantity", 0),
                            "sku": variant.get("sku", ""),
                            "display_name": variant.get("title", ""),
                            "sort_order": variant.get("position", 999),
                            "category": "variant",
                            "confidence_score": 0.9
                        }
                        validated = self.validate_variation_data(variation)
                        if validated:
                            variations.append(validated)
        except Exception as e:
            logging.getLogger(__name__).debug(f"Failed to parse Shopify JSON: {e}")
        return self._deduplicate_variations(variations)

    def _parse_woocommerce_json(self, script_content: str) -> List[Dict]:
        """Parse WooCommerce variation JSON."""
        variations = []
        try:
            # WooCommerce often embeds variation data
            if "product_variations" in script_content or "wc_product_variations" in script_content:
                import json
                # Try to extract variations array
                json_match = re.search(r'"product_variations":\s*(\[.*?\])', script_content)
                if json_match:
                    variations_data = json.loads(json_match.group(1))

                    for var_data in variations_data:
                        attributes = var_data.get("attributes", {})
                        variation = {
                            "type": "attribute",
                            "value": " ".join(attributes.values()) if attributes else "",
                            "price": float(var_data.get("display_price", 0)),
                            "stock": var_data.get("max_qty", 0),
                            "sku": var_data.get("sku", ""),
                            "display_name": " ".join(attributes.values()) if attributes else "",
                            "sort_order": var_data.get("menu_order", 999),
                            "category": "attribute",
                            "confidence_score": 0.9
                        }
                        validated = self.validate_variation_data(variation)
                        if validated:
                            variations.append(validated)
        except Exception as e:
            logging.getLogger(__name__).debug(f"Failed to parse WooCommerce JSON: {e}")
        return variations

    def _parse_magento_json(self, script_content: str) -> List[Dict]:
        """Parse Magento configurable product JSON."""
        variations = []
        try:
            # Magento uses spConfig for configurable products
            if "spConfig" in script_content:
                import json
                json_match = re.search(r'"spConfig":\s*(\{.*?\})', script_content)
                if json_match:
                    config_data = json.loads(json_match.group(1))
                    attributes = config_data.get("attributes", {})

                    for attr_id, attr_data in attributes.items():
                        options = attr_data.get("options", [])
                        for option in options:
                            variation = {
                                "type": attr_data.get("label", "attribute").lower(),
                                "value": option.get("label", ""),
                                "price": float(option.get("price", 0)),
                                "stock": 1,  # Magento doesn't always expose stock in JSON
                                "display_name": option.get("label", ""),
                                "sort_order": option.get("position", 999),
                                "category": "configurable",
                                "confidence_score": 0.8
                            }
                            validated = self.validate_variation_data(variation)
                            if validated:
                                variations.append(validated)
        except Exception as e:
            logging.getLogger(__name__).debug(f"Failed to parse Magento JSON: {e}")
        return variations

    def _parse_generic_json(self, script_content: str) -> List[Dict]:
        """Generic JSON parsing for unknown CMS platforms."""
        variations = []
        try:
            import json
            # Look for common variation keywords
            variation_keywords = ["variant", "option", "attribute", "choice", "selection"]

            for keyword in variation_keywords:
                if keyword in script_content.lower():
                    # Try to find JSON objects containing variations
                    json_matches = re.findall(r'\{[^{}]*"' + keyword + r'"[^{}]*\}', script_content, re.IGNORECASE)
                    for match in json_matches:
                        try:
                            data = json.loads(match)
                            # Extract basic variation info
                            variation = {
                                "type": keyword,
                                "value": str(data.get("name", data.get("title", data.get("label", "")))),
                                "price": float(data.get("price", 0)),
                                "stock": int(data.get("stock", data.get("quantity", 0))),
                                "display_name": str(data.get("name", data.get("title", ""))),
                                "sort_order": int(data.get("order", data.get("position", 999))),
                                "category": "generic",
                                "confidence_score": 0.5
                            }
                            validated = self.validate_variation_data(variation)
                            if validated:
                                variations.append(validated)
                        except Exception:
                            continue
                    break
        except Exception as e:
            logging.getLogger(__name__).debug(f"Failed to parse generic JSON: {e}")
        return variations

    def _merge_selector_lists(self, primary: List[str], secondary: List[str]) -> List[str]:
        merged: List[str] = []
        for selector in primary + secondary:
            if selector and selector not in merged:
                merged.append(selector)
        return merged

    def _deduplicate_variations(self, variations: List[Dict]) -> List[Dict]:
        unique: List[Dict] = []
        seen = set()
        for variation in variations:
            key = (variation.get("option_id") or variation.get("value"), variation.get("type"))
            if key not in seen:
                unique.append(variation)
                seen.add(key)
        return unique

    def extract_price(
        self, html: Optional[str] = None, cms_selectors: Optional[Dict[str, List[str]]] = None
    ) -> Optional[float]:
        """Extract current price using OpenCart selectors - works with or without page."""
        logger = logging.getLogger(__name__)
        if self.page is None:
            logger.debug("No page available, using static price extraction")
            return self.extract_price_static(html, cms_selectors)

        """Extract current price using CMS-specific selectors."""
        if cms_selectors:
            selectors = cms_selectors.get(
                "price_update",
                [
                    ".price-new",
                    ".product-price",
                    ".price",
                    ".current-price",
                    ".autocalc-product-price",
                    'span[itemprop="price"]',
                ],
            )
        else:
            selectors = [
                ".price-new",
                ".product-price",
                ".price",
                ".current-price",
                ".autocalc-product-price",
                'span[itemprop="price"]',
            ]
        for selector in selectors:
            try:
                element = self.page.locator(selector).first
                if element.is_visible():
                    text = element.inner_text() or "0"
                    text = text.strip()
                    if not text:
                        continue
                    try:
                        cleaned = clean_price(text)
                        if cleaned is not None:
                            return float(cleaned)
                    except Exception:
                        logger.debug("Failed to clean price from selector %s", selector)
                        continue
            except Exception:
                continue
        return None

    def extract_price_static(self, html: Optional[str] = None, cms_selectors: Optional[Dict[str, List[str]]] = None) -> Optional[float]:
        """Extract price from static HTML without Playwright."""
        if html is None:
            return None
        try:
            soup = _bs()(html, "lxml")
            if cms_selectors:
                selectors = cms_selectors.get(
                    "price_update",
                    [
                        ".price-new",
                        ".product-price",
                        ".price",
                        ".current-price",
                        ".autocalc-product-price",
                        'span[itemprop="price"]',
                    ],
                )
            else:
                selectors = [
                    ".price-new",
                    ".product-price",
                    ".price",
                    ".current-price",
                    ".autocalc-product-price",
                    'span[itemprop="price"]',
                ]
            for selector in selectors:
                elements = soup.select(selector)
                for elem in elements:
                    text = elem.get_text(strip=True)
                    if text:
                        cleaned = clean_price(text)
                        if cleaned is not None:
                            return float(cleaned)
            return None
        except Exception:
            return None

    def extract_stock(self, html: Optional[str] = None, cms_selectors: Optional[Dict[str, List[str]]] = None) -> Optional[int]:
        """Extract current stock using OpenCart selectors - works with or without page."""
        logger = logging.getLogger(__name__)
        if self.page is None:
            logger.debug("No page available, using static stock extraction")
            return self.extract_stock_static(html, cms_selectors)

        """Extract current stock using CMS-specific selectors."""
        if cms_selectors:
            selectors = cms_selectors.get("stock_update", [".stock-status", ".stock", ".availability", ".in-stock"])
        else:
            selectors = [".stock-status", ".stock", ".availability", ".in-stock"]
        for selector in selectors:
            try:
                element = self.page.locator(selector).first
                if element.is_visible():
                    text = element.inner_text() or "0"
                    text = text.strip()
                    if not text:
                        continue
                    try:
                        parsed = parse_stock(text)
                        if parsed is not None:
                            return int(parsed)
                    except Exception:
                        logger.debug("Failed to parse stock from selector %s", selector)
                        continue
            except Exception:
                continue
        return None

    def extract_stock_static(self, html: Optional[str] = None, cms_selectors: Optional[Dict[str, List[str]]] = None) -> Optional[int]:
        """Extract stock from static HTML without Playwright."""
        if html is None:
            return None
        try:
            soup = _bs()(html, "lxml")
            if cms_selectors:
                selectors = cms_selectors.get("stock_update", [".stock-status", ".stock", ".availability", ".in-stock"])
            else:
                selectors = [".stock-status", ".stock", ".availability", ".in-stock"]
            for selector in selectors:
                elements = soup.select(selector)
                for elem in elements:
                    text = elem.get_text(strip=True)
                    if text:
                        parsed = parse_stock(text)
                        if parsed is not None:
                            return int(parsed)
            return None
        except Exception as exc:
            logging.getLogger(__name__).debug(
                "Static stock extraction failed for selectors %s: %s",
                selectors,
                exc,
            )
            return None

    def classify_variation_type(
        self, label_text: str, value_text: str
    ) -> Dict[str, Any]:
        """Enhanced variation type classification with comprehensive keyword detection and confidence scoring."""
        logger = logging.getLogger(__name__)

        # Get base classification from helper
        base_result = get_variation_type_details(label_text)
        var_type = str(base_result.get("type", "unknown"))
        confidence = float(base_result.get("confidence", 0.0))

        # Enhanced keyword detection with value_text context
        value_lower = value_text.lower().strip() if value_text else ""

        # Size patterns (numbers, letters, dimensions)
        size_patterns = [
            r"\b\d+(?:\.\d+)?\s*(?:x|×|by|\*)\s*\d+(?:\.\d+)?",  # dimensions like 10x10
            r"\b\d+(?:\.\d+)?\s*(?:ml|l|kg|g|oz|lb|cm|mm|inch)",  # units
            r"\b(?:xs|s|m|l|xl|xxl|xxxl|2xl|3xl|4xl)\b",  # size letters
            r"\b\d+(?:\.\d+)?(?:\s*-\s*\d+(?:\.\d+)?)?\b",  # numbers/ranges
        ]

        # Color patterns
        color_patterns = [
            r"\b(?:red|blue|green|black|white|yellow|orange|purple|pink|brown|gray|grey|silver|gold)\b",
            r"\b(?:красный|синий|зеленый|черный|белый|желтый|оранжевый|фиолетовый|розовый|коричневый|серый|серебряный|золотой)\b",
        ]

        # Model/style patterns
        model_patterns = [
            r"\b(?:model|style|type|variant|series|edition|version)\b",
            r"\b(?:модель|стиль|тип|вариант|серия|издание|версия)\b",
        ]

        # Check value_text for additional context
        size_matches = sum(
            1 for pattern in size_patterns if re.search(pattern, value_lower)
        )
        color_matches = sum(
            1 for pattern in color_patterns if re.search(pattern, value_lower)
        )
        model_matches = sum(
            1 for pattern in model_patterns if re.search(pattern, value_lower)
        )

        # Adjust confidence based on value_text
        if size_matches > 0 and var_type == "size":
            confidence = min(confidence + 0.2, 1.0)
        elif color_matches > 0 and var_type == "color":
            confidence = min(confidence + 0.2, 1.0)
        elif model_matches > 0 and var_type == "model":
            confidence = min(confidence + 0.2, 1.0)

        # Fallback assignment for low confidence
        if confidence < 0.3:
            if size_matches > color_matches and size_matches > model_matches:
                var_type = "size"
                confidence = 0.4
            elif color_matches > model_matches:
                var_type = "color"
                confidence = 0.4
            elif model_matches > 0:
                var_type = "model"
                confidence = 0.4
            else:
                var_type = "unknown"
                confidence = 0.0

        # Determine category
        category_map = {
            "size": "dimension",
            "color": "appearance",
            "model": "variant",
            "unknown": "other",
        }
        category = category_map.get(var_type, "other")

        logger.debug(
            f"Classified variation type: {var_type} (confidence: {confidence:.2f}) for label: '{label_text}', value: '{value_text}'"
        )

        return {"type": var_type, "confidence": confidence, "category": category}

    def format_variation_display_name(self, var_type: str, value: str) -> str:
        """Create user-friendly display name for variation."""
        if not value:
            return "Unknown"

        # Type-specific formatting
        if var_type == "size":
            # Standardize size format
            value = re.sub(r"\s+", "", value.upper())
            if re.match(r"^\d+(?:\.\d+)?(?:X\d+(?:\.\d+)?)?$", value):
                return value.replace("X", " × ")
        elif var_type == "color":
            # Capitalize color names
            return value.strip().title()
        elif var_type == "model":
            # Keep model names as-is but clean
            return value.strip()

        return value.strip()

    def sort_variations_for_display(self, variations: List[Dict]) -> List[Dict]:
        """Sort variations logically for consistent display."""
        if not variations:
            return []

        def sort_key(variation):
            var_type = variation.get("type", "unknown")
            value = variation.get("value", "")
            sort_order = variation.get("sort_order", 999)

            # Primary sort by type order
            type_order = {"size": 1, "color": 2, "model": 3, "unknown": 4}
            type_priority = type_order.get(var_type, 4)

            # Secondary sort by custom sort_order
            # Tertiary sort by value (natural sort for sizes)
            if var_type == "size":
                # Extract numbers for natural sorting
                numbers = re.findall(r"\d+(?:\.\d+)?", value)
                if numbers:
                    return (type_priority, sort_order, float(numbers[0]))
                else:
                    # Letter sizes
                    size_map = {
                        "XS": 1,
                        "S": 2,
                        "M": 3,
                        "L": 4,
                        "XL": 5,
                        "XXL": 6,
                        "XXXL": 7,
                    }
                    size_key = size_map.get(value.upper(), 99)
                    return (type_priority, sort_order, size_key)
            else:
                return (type_priority, sort_order, value.lower())

        return sorted(variations, key=sort_key)

    def group_variations_by_type(self, variations: List[Dict]) -> Dict[str, List[Dict]]:
        """Group variations by type for hierarchical display."""
        groups = {}
        for variation in variations:
            var_type = variation.get("type", "unknown")
            if var_type not in groups:
                groups[var_type] = []
            groups[var_type].append(variation)

        # Sort within each group
        for var_type in groups:
            groups[var_type] = self.sort_variations_for_display(groups[var_type])

        return groups

    def validate_variation_data(self, variation: Dict) -> Optional[Dict]:
        """Validate and enhance variation data with fallbacks."""
        logger = logging.getLogger(__name__)

        price = variation.get("price")
        stock = variation.get("stock")

        if price is None:
            price_value = 0.0
        else:
            try:
                price_value = float(price)
            except (TypeError, ValueError):
                logger.debug(
                    "Variation %s price is not numeric: %s",
                    variation.get("value", ""),
                    price,
                )
                price_value = 0.0

        if price_value < 0:
            logger.debug(
                "Variation %s price negative (%s); dropping",
                variation.get("value", ""),
                price_value,
            )
            return None

        if stock is None:
            stock_value = None
        else:
            try:
                stock_value = int(stock)
            except (TypeError, ValueError):
                parsed_stock = parse_stock(str(stock))
                stock_value = parsed_stock if parsed_stock is not None else 0

        validated = {
            "type": variation.get("type", "unknown"),
            "value": variation.get("value", ""),
            "option_id": variation.get("option_id"),
            "price": price_value,
            "stock": stock_value,
            "in_stock": (
                variation.get("in_stock")
                if variation.get("in_stock") is not None
                else (stock_value is None or stock_value > 0)
            ),
            "display_name": variation.get("display_name", ""),
            "sort_order": variation.get("sort_order", 999),
            "category": variation.get("category", "other"),
            "confidence_score": variation.get("confidence_score", 0.0),
        }

        if not validated["display_name"]:
            validated["display_name"] = self.format_variation_display_name(
                validated["type"], validated["value"]
            )

        for key in ("sku", "variant_id", "url", "currency"):
            value = variation.get(key)
            if value:
                validated[key] = value

        if "attributes" in variation and isinstance(variation["attributes"], dict):
            validated["attributes"] = variation["attributes"]
        else:
            validated.setdefault("attributes", {})

        if "in_stock" in variation:
            validated["in_stock"] = bool(variation["in_stock"])

        return validated

    def _extract_option_price(
        self, option: Any, fallback_price: Optional[float]
    ) -> Optional[float]:
        """Extract price from option attributes or text."""
        attribute_names = [
            "data-price",
            "data-price-value",
            "data-option-price",
            "data-price-modifier",
            "data-price-plus",
            "data-price-diff",
            "data-special",
            "data-value",
        ]

        delta_attributes = {
            "data-price",
            "data-price-modifier",
            "data-price-plus",
            "data-price-diff",
        }

        for attr in attribute_names:
            raw = option.get(attr)
            if raw is None:
                continue
            price = clean_price(str(raw))
            if price is None:
                continue

            if attr in delta_attributes and fallback_price is not None:
                if price == 0:
                    return float(fallback_price)
                return float(fallback_price) + float(price)

            return float(price)

        option_text = option.get_text(strip=True)
        if option_text:
            price = clean_price(option_text)
            if price is not None:
                if fallback_price is not None and price == 0:
                    return float(fallback_price)
                return float(price)

        return fallback_price

    def _extract_option_stock(
        self, option: Any, fallback_stock: Optional[int]
    ) -> Optional[int]:
        """Extract stock from option attributes or text."""
        candidates: List[str] = []
        attribute_names = [
            "data-quantity",
            "data-stock",
            "data-option-stock",
            "data-qty",
            "data-remains",
            "data-count",
            "data-opt-quantity",
        ]

        for attr in attribute_names:
            value = option.get(attr)
            if value is not None:
                candidates.append(str(value))

        option_text = option.get_text(strip=True)
        if option_text:
            candidates.append(option_text)

        for candidate in candidates:
            stock = parse_stock(candidate)
            if stock is not None:
                return stock

        return fallback_stock

    def generate_variation_summary(self, variations: List[Dict]) -> Dict:
        """Generate summary statistics for variations."""
        if not variations:
            return {}

        total_count = len(variations)
        in_stock_count = sum(
            1
            for v in variations
            if isinstance(v.get("stock"), int) and v["stock"] > 0
        )
        price_values = [v["price"] for v in variations if isinstance(v.get("price"), (int, float))]
        avg_price = (sum(price_values) / len(price_values)) if price_values else 0.0
        min_price = min(price_values, default=0.0)
        max_price = max(price_values, default=0.0)

        type_counts = {}
        for v in variations:
            var_type = v.get("type", "unknown")
            type_counts[var_type] = type_counts.get(var_type, 0) + 1

        return {
            "total_variations": total_count,
            "in_stock_count": in_stock_count,
            "out_of_stock_count": total_count - in_stock_count,
            "average_price": round(avg_price, 2),
            "min_price": min_price,
            "max_price": max_price,
            "type_distribution": type_counts,
        }
