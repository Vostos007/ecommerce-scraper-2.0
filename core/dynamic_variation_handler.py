"""
Dynamic Variation Handler with MutationObserver support.

This module handles dynamic product variations that update via JavaScript/AJAX
when users interact with variation selectors (dropdowns, swatches, buttons).
"""

import asyncio
import logging
import time
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from playwright.async_api import Page, Response

from utils.cms_detection import CMSDetection


@dataclass
class VariationState:
    """Represents the current state of a product variation."""

    attributes: Dict[str, str]  # e.g., {"color": "red", "size": "M"}
    price: Optional[float]
    stock: Optional[int]
    sku: Optional[str]
    image_url: Optional[str]
    available: bool = True


@dataclass
class VariationInteraction:
    """Represents an interaction with a variation selector."""

    selector: str
    action: str  # 'click', 'select', 'change'
    value: str
    attribute_type: str  # 'color', 'size', 'model', etc.


class DynamicVariationHandler:
    """
    Handles dynamic variation detection and interaction using MutationObserver
    and AJAX monitoring for modern e-commerce platforms.
    """

    def __init__(self, page: Page, cms_type: Optional[str] = None):
        self.page = page
        self.cms_type = cms_type
        self.cms_detector = CMSDetection()
        self.logger = logging.getLogger(__name__)

        # State tracking
        self.current_state = VariationState(
            attributes={}, price=None, stock=None, sku=None, image_url=None
        )
        self.variation_states: List[VariationState] = []
        self.mutation_observer_active = False
        self.ajax_responses: List[Dict] = []

        # Configuration
        self.interaction_delay = 1000  # ms between interactions
        self.mutation_timeout = 5000  # ms to wait for DOM changes
        self.max_combinations = 100  # limit variation combinations

    async def initialize_monitoring(self) -> bool:
        """Initialize MutationObserver and AJAX monitoring."""
        try:
            # Set up MutationObserver for DOM changes
            await self._setup_mutation_observer()

            # Set up AJAX response monitoring
            await self._setup_ajax_monitoring()

            self.logger.info("Dynamic variation monitoring initialized")
            return True

        except Exception as e:
            self.logger.error(f"Failed to initialize monitoring: {e}")
            return False

    async def _setup_mutation_observer(self) -> None:
        """Set up MutationObserver to watch for DOM changes."""
        mutation_script = """
        () => {
            window.variationMutations = [];

            const observer = new MutationObserver((mutations) => {
                mutations.forEach((mutation) => {
                    if (mutation.type === 'childList' || mutation.type === 'attributes') {
                        // Track changes to price, stock, and image elements
                        const target = mutation.target;
                        if (target.matches && (
                            target.matches('.price, .price-box, .current-price, .product-price') ||
                            target.matches('.stock, .availability, .inventory') ||
                            target.matches('.product-image, .gallery-image') ||
                            target.closest('.price, .price-box, .current-price, .product-price') ||
                            target.closest('.stock, .availability, .inventory') ||
                            target.closest('.product-image, .gallery-image')
                        )) {
                            window.variationMutations.push({
                                type: mutation.type,
                                target: target.tagName + (target.className ? '.' + target.className.replace(/\\s+/g, '.') : ''),
                                timestamp: Date.now(),
                                content: target.textContent?.trim() || '',
                                src: target.src || '',
                                dataset: target.dataset ? Object.assign({}, target.dataset) : {}
                            });
                        }
                    }
                });
            });

            observer.observe(document.body, {
                childList: true,
                subtree: true,
                attributes: true,
                attributeFilter: ['class', 'data-price', 'data-stock', 'src']
            });

            window.variationObserver = observer;
        }
        """

        await self.page.evaluate(mutation_script)
        self.mutation_observer_active = True
        self.logger.debug("MutationObserver setup complete")

    async def _setup_ajax_monitoring(self) -> None:
        """Set up monitoring for AJAX requests related to variations."""

        async def handle_response(response: Response):
            try:
                url = response.url

                # Check if this might be a variation-related request
                if any(
                    keyword in url.lower()
                    for keyword in [
                        "variant",
                        "option",
                        "product",
                        "price",
                        "stock",
                        "cart",
                        "ajax",
                    ]
                ):

                    if response.status == 200:
                        try:
                            # Try to get JSON response
                            response_data = await response.json()
                            self.ajax_responses.append(
                                {
                                    "url": url,
                                    "data": response_data,
                                    "timestamp": time.time(),
                                    "type": "json",
                                }
                            )
                        except (ValueError, TypeError, UnicodeDecodeError) as e:
                            self.logger.debug(
                                f"Failed to parse JSON response from {url}: {e}"
                            )
                            try:
                                # Try to get text response
                                response_text = await response.text()
                                if response_text.strip():
                                    self.ajax_responses.append(
                                        {
                                            "url": url,
                                            "data": response_text,
                                            "timestamp": time.time(),
                                            "type": "text",
                                        }
                                    )
                            except (UnicodeDecodeError, RuntimeError) as text_e:
                                self.logger.debug(
                                    f"Failed to get text response from {url}: {text_e}"
                                )

            except Exception as e:
                self.logger.debug(f"Error handling response: {e}")

        self.page.on("response", handle_response)
        self.logger.debug("AJAX monitoring setup complete")

    async def detect_variation_selectors(self) -> List[VariationInteraction]:
        """Detect all variation selectors on the page."""
        interactions = []

        try:
            # Get CMS-specific selectors
            cms_selectors = self.cms_detector.get_variation_selectors(
                "attributes", self.cms_type
            )
            swatch_selectors = self.cms_detector.get_variation_selectors(
                "swatches", self.cms_type
            )

            # Combine all selectors
            all_selectors = cms_selectors + swatch_selectors

            for selector in all_selectors:
                try:
                    elements = await self.page.query_selector_all(selector)

                    for element in elements:
                        if await element.is_visible():
                            # Determine interaction type and attribute
                            tag_name = await element.evaluate(
                                "el => el.tagName.toLowerCase()"
                            )

                            if tag_name == "select":
                                # Dropdown selector
                                options = await element.query_selector_all("option")
                                attribute_type = await self._detect_attribute_type(
                                    element
                                )

                                for option in options[1:]:  # Skip first empty option
                                    value = await option.get_attribute("value")
                                    if value:
                                        interactions.append(
                                            VariationInteraction(
                                                selector=selector,
                                                action="select",
                                                value=value,
                                                attribute_type=attribute_type,
                                            )
                                        )

                            elif tag_name in ["input", "button", "label"]:
                                # Swatch or button selector
                                input_type = await element.get_attribute("type")
                                value = (
                                    await element.get_attribute("value")
                                    or await element.text_content()
                                )

                                if input_type in ["radio", "checkbox"] or tag_name in [
                                    "button",
                                    "label",
                                ]:
                                    attribute_type = await self._detect_attribute_type(
                                        element
                                    )
                                    interactions.append(
                                        VariationInteraction(
                                            selector=selector,
                                            action="click",
                                            value=value or "",
                                            attribute_type=attribute_type,
                                        )
                                    )

                except Exception as e:
                    self.logger.debug(f"Error processing selector {selector}: {e}")
                    continue

            self.logger.info(f"Detected {len(interactions)} variation interactions")
            return interactions

        except Exception as e:
            self.logger.error(f"Error detecting variation selectors: {e}")
            return []

    async def _detect_attribute_type(self, element) -> str:
        """Detect the type of attribute (color, size, etc.) from element context."""
        try:
            # Check element attributes and text for clues
            name = await element.get_attribute("name") or ""
            id_attr = await element.get_attribute("id") or ""
            class_attr = await element.get_attribute("class") or ""

            # Look for labels
            label_text = ""
            try:
                # Try to find associated label
                if id_attr:
                    label = await self.page.query_selector(f'label[for="{id_attr}"]')
                    if label:
                        label_text = await label.text_content() or ""

                # Or check parent elements for context
                if not label_text:
                    parent = await element.evaluate(
                        "el => el.closest('.form-group, .field, .option, .attribute')"
                    )
                    if parent:
                        label_elem = await parent.query_selector(
                            "label, .label, .field-label"
                        )
                        if label_elem:
                            label_text = await label_elem.text_content() or ""
            except (TimeoutError, AttributeError, RuntimeError) as e:
                self.logger.debug(
                    f"Error detecting attribute type from element context: {e}"
                )

            # Combine all text for analysis
            context_text = f"{name} {id_attr} {class_attr} {label_text}".lower()

            # Classify based on keywords
            if any(keyword in context_text for keyword in ["color", "colour", "цвет"]):
                return "color"
            elif any(
                keyword in context_text for keyword in ["size", "размер", "dimension"]
            ):
                return "size"
            elif any(
                keyword in context_text
                for keyword in ["model", "style", "type", "модель"]
            ):
                return "model"
            elif any(
                keyword in context_text
                for keyword in ["material", "fabric", "материал"]
            ):
                return "material"
            else:
                return "attribute"

        except Exception as e:
            self.logger.debug(f"Error detecting attribute type: {e}")
            return "attribute"

    async def extract_all_variations(self) -> List[Dict[str, Any]]:
        """Extract all possible product variations by interacting with selectors."""
        variations = []

        try:
            # Initialize monitoring
            if not await self.initialize_monitoring():
                return []

            # Detect all variation selectors
            interactions = await self.detect_variation_selectors()

            if not interactions:
                self.logger.warning("No variation selectors detected")
                return []

            # Group interactions by attribute type
            grouped_interactions = {}
            for interaction in interactions:
                attr_type = interaction.attribute_type
                if attr_type not in grouped_interactions:
                    grouped_interactions[attr_type] = []
                grouped_interactions[attr_type].append(interaction)

            # Generate combinations (limit to prevent infinite loops)
            combinations = self._generate_combinations(grouped_interactions)
            combinations = combinations[: self.max_combinations]

            self.logger.info(f"Testing {len(combinations)} variation combinations")

            # Test each combination
            for i, combination in enumerate(combinations):
                try:
                    # Clear previous state
                    self.ajax_responses.clear()
                    await self.page.evaluate(
                        "() => { window.variationMutations = []; }"
                    )

                    # Apply the combination
                    state = await self._apply_variation_combination(combination)

                    if state and state.price is not None:
                        # Convert state to variation dict
                        variation = {
                            "type": (
                                "_".join(state.attributes.keys())
                                if state.attributes
                                else "variation"
                            ),
                            "value": (
                                " ".join(state.attributes.values())
                                if state.attributes
                                else f"Variation {i+1}"
                            ),
                            "price": state.price,
                            "stock": state.stock or 0,
                            "sku": state.sku or "",
                            "image_url": state.image_url or "",
                            "available": state.available,
                            "attributes": state.attributes,
                            "display_name": (
                                " ".join(state.attributes.values())
                                if state.attributes
                                else f"Variation {i+1}"
                            ),
                            "sort_order": i,
                            "category": "dynamic",
                            "confidence_score": 0.9,
                        }
                        variations.append(variation)

                        self.logger.debug(
                            f"Extracted variation {i+1}: {variation['value']}"
                        )

                    # Small delay between combinations
                    await asyncio.sleep(0.5)

                except Exception as e:
                    self.logger.debug(f"Error testing combination {i}: {e}")
                    continue

            self.logger.info(
                f"Successfully extracted {len(variations)} dynamic variations"
            )
            return variations

        except Exception as e:
            self.logger.error(f"Error extracting variations: {e}")
            return []

    def _generate_combinations(
        self, grouped_interactions: Dict[str, List[VariationInteraction]]
    ) -> List[List[VariationInteraction]]:
        """Generate all possible combinations of variation interactions."""
        import itertools

        combinations = []

        # Get all attribute types
        attribute_types = list(grouped_interactions.keys())

        if not attribute_types:
            return []

        # Generate combinations for each attribute type
        attribute_options = []
        for attr_type in attribute_types:
            interactions = grouped_interactions[attr_type]
            # Limit options per attribute to prevent explosion
            limited_interactions = interactions[:10]  # Max 10 options per attribute
            attribute_options.append(limited_interactions)

        # Generate cartesian product of all combinations
        for combination in itertools.product(*attribute_options):
            combinations.append(list(combination))

        return combinations

    async def _apply_variation_combination(
        self, combination: List[VariationInteraction]
    ) -> Optional[VariationState]:
        """Apply a specific combination of variation selections."""
        try:
            state = VariationState(
                attributes={}, price=None, stock=None, sku=None, image_url=None
            )

            # Apply each interaction in the combination
            for interaction in combination:
                try:
                    # Find the element
                    elements = await self.page.query_selector_all(interaction.selector)
                    target_element = None

                    # Find the specific element for this value
                    for element in elements:
                        if interaction.action == "select":
                            # For select elements, find the option
                            options = await element.query_selector_all("option")
                            for option in options:
                                value = await option.get_attribute("value")
                                if value == interaction.value:
                                    target_element = element
                                    break
                        else:
                            # For other elements, check value or text
                            element_value = await element.get_attribute("value")
                            element_text = await element.text_content()

                            if (
                                element_value == interaction.value
                                or element_text == interaction.value
                            ):
                                target_element = element
                                break

                    if target_element:
                        # Perform the interaction
                        if interaction.action == "select":
                            await target_element.select_option(value=interaction.value)
                        else:
                            await target_element.click()

                        # Update state attributes
                        state.attributes[interaction.attribute_type] = interaction.value

                        # Wait for changes
                        await asyncio.sleep(self.interaction_delay / 1000)

                except Exception as e:
                    self.logger.debug(
                        f"Error applying interaction {interaction.value}: {e}"
                    )
                    continue

            # Wait for all changes to complete
            await asyncio.sleep(1)

            # Extract current state
            state.price = await self._extract_current_price()
            state.stock = await self._extract_current_stock()
            state.sku = await self._extract_current_sku()
            state.image_url = await self._extract_current_image()
            state.available = state.stock is None or state.stock > 0

            return state

        except Exception as e:
            self.logger.debug(f"Error applying combination: {e}")
            return None

    async def _extract_current_price(self) -> Optional[float]:
        """Extract current price from the page."""
        try:
            price_selectors = self.cms_detector.get_variation_selectors(
                "price_update", self.cms_type
            )

            for selector in price_selectors:
                try:
                    element = await self.page.query_selector(selector)
                    if element and await element.is_visible():
                        text = await element.text_content()
                        if text:
                            # Extract numeric price
                            import re

                            price_match = re.search(r"[\d.,]+", text.replace(",", "."))
                            if price_match:
                                return float(price_match.group().replace(",", "."))
                except (TimeoutError, AttributeError, RuntimeError, ValueError) as e:
                    self.logger.debug(
                        f"Error extracting current price with selector {selector}: {e}"
                    )
                    continue

            # Check AJAX responses for price data
            for response in self.ajax_responses[-5:]:  # Check last 5 responses
                try:
                    if response["type"] == "json":
                        data = response["data"]
                        if isinstance(data, dict):
                            price = (
                                data.get("price")
                                or data.get("cost")
                                or data.get("amount")
                            )
                            if price is not None:
                                return float(price)
                except (ValueError, TypeError, KeyError) as e:
                    self.logger.debug(f"Error processing AJAX response for price: {e}")
                    continue

            return None

        except Exception as e:
            self.logger.debug(f"Error extracting price: {e}")
            return None

    async def _extract_current_stock(self) -> Optional[int]:
        """Extract current stock from the page."""
        try:
            stock_selectors = self.cms_detector.get_variation_selectors(
                "stock_update", self.cms_type
            )

            for selector in stock_selectors:
                try:
                    element = await self.page.query_selector(selector)
                    if element and await element.is_visible():
                        text = await element.text_content()
                        if text:
                            # Extract numeric stock
                            import re

                            stock_match = re.search(r"\d+", text)
                            if stock_match:
                                return int(stock_match.group())
                except (TimeoutError, AttributeError, RuntimeError, ValueError) as e:
                    self.logger.debug(
                        f"Error extracting current stock with selector {selector}: {e}"
                    )
                    continue

            # Check AJAX responses for stock data
            for response in self.ajax_responses[-5:]:
                try:
                    if response["type"] == "json":
                        data = response["data"]
                        if isinstance(data, dict):
                            stock = (
                                data.get("stock")
                                or data.get("quantity")
                                or data.get("inventory")
                            )
                            if stock is not None:
                                return int(stock)
                except (ValueError, TypeError, KeyError) as e:
                    self.logger.debug(f"Error processing AJAX response for stock: {e}")
                    continue

            return None

        except Exception as e:
            self.logger.debug(f"Error extracting stock: {e}")
            return None

    async def _extract_current_sku(self) -> Optional[str]:
        """Extract current SKU from the page."""
        try:
            # Check for SKU in various places
            sku_selectors = [".sku", ".product-sku", "[data-sku]", ".variant-sku"]

            for selector in sku_selectors:
                try:
                    element = await self.page.query_selector(selector)
                    if element:
                        sku = (
                            await element.get_attribute("data-sku")
                            or await element.text_content()
                        )
                        if sku and sku.strip():
                            return sku.strip()
                except (TimeoutError, AttributeError, RuntimeError) as e:
                    self.logger.debug(
                        f"Error extracting SKU with selector {selector}: {e}"
                    )
                    continue

            # Check AJAX responses for SKU
            for response in self.ajax_responses[-5:]:
                try:
                    if response["type"] == "json":
                        data = response["data"]
                        if isinstance(data, dict):
                            sku = data.get("sku") or data.get("product_code")
                            if sku:
                                return str(sku)
                except (ValueError, TypeError, KeyError) as e:
                    self.logger.debug(f"Error processing AJAX response for SKU: {e}")
                    continue

            return None

        except Exception as e:
            self.logger.debug(f"Error extracting SKU: {e}")
            return None

    async def _extract_current_image(self) -> Optional[str]:
        """Extract current product image URL."""
        try:
            # Check for main product image
            image_selectors = [
                ".product-image img",
                ".gallery-image img",
                ".main-image img",
                "[data-image]",
            ]

            for selector in image_selectors:
                try:
                    element = await self.page.query_selector(selector)
                    if element:
                        src = await element.get_attribute(
                            "src"
                        ) or await element.get_attribute("data-src")
                        if src:
                            return src
                except (TimeoutError, AttributeError, RuntimeError) as e:
                    self.logger.debug(
                        f"Error extracting image with selector {selector}: {e}"
                    )
                    continue

            return None

        except Exception as e:
            self.logger.debug(f"Error extracting image: {e}")
            return None

    async def cleanup(self) -> None:
        """Cleanup monitoring and observers."""
        try:
            if self.mutation_observer_active:
                await self.page.evaluate(
                    "() => { if (window.variationObserver) window.variationObserver.disconnect(); }"
                )
                self.mutation_observer_active = False

            self.logger.debug("Dynamic variation handler cleanup complete")

        except Exception as e:
            self.logger.debug(f"Error during cleanup: {e}")
