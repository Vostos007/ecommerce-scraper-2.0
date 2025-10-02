import xml.etree.ElementTree as ET
from collections import deque
from typing import List, Optional, Set
from urllib.parse import urljoin, urlparse
from playwright.sync_api import Page
import httpx
from bs4 import BeautifulSoup
import json
import time
import warnings
from core.antibot_manager import AntibotManager
from utils.helpers import parse_robots_txt, is_product_url
from utils.cms_detection import CMSDetection, CMSConfig
import logging

logger = logging.getLogger("sitemap_analyzer")


class SitemapAnalyzer:
    def __init__(
        self, antibot_manager: AntibotManager, base_url: Optional[str] = ""
    ):
        self.antibot_manager = antibot_manager
        self.base_url = base_url.rstrip("/") if base_url else ""
        self.settings = self._load_settings()
        self.manual_categories = self.settings.get(
            "manual_categories", ["/instrumenty", "/yarn"]
        )

        # Initialize CMS detection with config
        cms_config_data = self.settings.get("cms_detection", {})
        cms_config = CMSConfig(
            enable_version_detection=cms_config_data.get("enabled", True),
            confidence_threshold=cms_config_data.get("confidence_threshold", 0.6),
            max_detection_time=cms_config_data.get("detection_cache_ttl", 30.0),
            detection_methods=cms_config_data.get("detection_methods", {}),
            method_weights=self._extract_method_weights(
                cms_config_data.get("detection_methods", {})
            ),
        )
        self.cms_detector = CMSDetection(cms_config)

    def _load_settings(self) -> dict:
        try:
            with open("config/settings.json", "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            logger.warning("Settings file not found, using defaults")
            return {}

    def _extract_method_weights(self, detection_methods: dict) -> dict:
        """Extract method weights from detection_methods settings."""
        method_weights = {}
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
        return method_weights

    def set_base_url(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def find_sitemap_url(self, antibot_manager: AntibotManager) -> Optional[str]:
        """Find sitemap URL by trying common paths and parsing robots.txt."""
        logger.info(f"Searching for sitemap at {self.base_url}")

        # First try curl_cffi fetch
        for pattern in self.settings.get(
            "sitemap_patterns", ["/sitemap.xml", "/sitemap_index.xml"]
        ):
            sitemap_url = urljoin(self.base_url, pattern)
            logger.debug(f"Trying sitemap URL with curl_cffi: {sitemap_url}")
            try:
                content = antibot_manager.fetch_sitemap(sitemap_url)
                logger.debug(f"Sitemap content preview: {content[:500]}")
                if content and content.startswith("<?xml"):
                    logger.info(f"Found sitemap at {sitemap_url} via curl_cffi")
                    return sitemap_url
            except Exception as e:
                logger.debug(f"Sitemap fetch failed for {sitemap_url}: {e}")
                continue

        # Fallback to browser if curl_cffi failed
        page = antibot_manager.get_page()
        antibot_manager.human_scroll(page)

        # Try common sitemap paths with browser
        for pattern in self.settings.get(
            "sitemap_patterns", ["/sitemap.xml", "/sitemap_index.xml"]
        ):
            sitemap_url = urljoin(self.base_url, pattern)
            logger.debug(f"Trying sitemap URL with browser: {sitemap_url}")
            try:
                page.goto(sitemap_url, wait_until="networkidle")
                antibot_manager.human_delay(3)
                content = page.content()
                logger.debug(f"Sitemap content preview: {content[:500]}")
                if content.startswith("<?xml"):
                    logger.info(f"Found sitemap at {sitemap_url}")
                    return sitemap_url
            except Exception as e:
                logger.debug(f"Sitemap goto failed for {sitemap_url}: {e}")
                continue

        # Parse robots.txt
        logger.debug("Trying to parse robots.txt")
        try:
            robots_url = urljoin(self.base_url, "/robots.txt")
            page.goto(robots_url)
            antibot_manager.human_delay()
            robots_content = page.content()
            robots_data = parse_robots_txt(robots_content)
            sitemaps = robots_data.get("sitemap", [])
            if sitemaps:
                first_sitemap = sitemaps[0]
                logger.info(f"Found sitemap in robots.txt: {first_sitemap}")
                return first_sitemap
        except Exception as e:
            logger.debug(f"Robots.txt goto failed: {e}")

        logger.warning("No sitemap found")
        return None

    def parse_sitemap(self, sitemap_url: str, page: Page) -> List[str]:
        """Parse sitemap XML and return filtered product URLs."""
        logger.info(f"Parsing sitemap: {sitemap_url}")
        product_urls = []
        urls = []

        # Try curl_cffi first
        try:
            xml_content = self.antibot_manager.fetch_sitemap(sitemap_url)
            logger.debug(f"Sitemap content length from curl_cffi: {len(xml_content)}")
        except Exception as e:
            logger.debug(f"curl_cffi failed, falling back to browser: {e}")
            page.goto(sitemap_url, wait_until="networkidle")
            self.antibot_manager.human_delay(3)
            xml_content = page.content()

        if not xml_content or not xml_content.startswith("<?xml"):
            logger.warning(
                f"No valid XML content from sitemap {sitemap_url}, using manual categories fallback"
            )
            product_urls = self.get_manual_category_products(page)
            return product_urls or []

        logger.debug(f"Sitemap content preview: {xml_content[:500]}")

        try:
            root = ET.fromstring(xml_content)
        except ET.ParseError as e:
            logger.error(f"XML parsing error in sitemap: {e}")
            logger.debug(f"Sitemap content for debugging: {xml_content[:1000]}")
            return self.get_manual_category_products(page)

        try:
            # Clean namespace tags
            for elem in root.iter():
                if "}" in elem.tag:
                    elem.tag = elem.tag.split("}", 1)[1]

            all_found_urls = []

            # Handle sitemapindex
            if root.tag == "sitemapindex":
                logger.debug("Parsing sitemap index")
                for sitemap in root.findall(".//loc"):
                    if sitemap.text:
                        logger.debug(f"Found sub-sitemap: {sitemap.text}")
                        sub_urls = self.parse_sitemap(sitemap.text, page)
                        product_urls.extend(sub_urls)
                        all_found_urls.extend(sub_urls)
            # Handle urlset
            elif root.tag == "urlset":
                logger.debug("Parsing URL set")
                for url_elem in root.findall(".//url"):
                    loc_elem = url_elem.find("loc")
                    changefreq_elem = url_elem.find("changefreq")
                    if loc_elem is not None and loc_elem.text:
                        url = loc_elem.text
                        all_found_urls.append(url)
                        logger.debug(f"Found URL: {url}")
                        # Filter by changefreq='weekly' or product patterns (changed to weekly for more results)
                        if (
                            changefreq_elem is not None
                            and changefreq_elem.text in ["daily", "weekly"]
                        ) or is_product_url(url):
                            urls.append(url)
                            product_urls.append(url)
                            logger.debug(f"Added product URL: {url}")
                        else:
                            logger.debug(f"Skipped non-product URL: {url}")
            else:
                logger.warning(f"Unknown sitemap format: {root.tag}")

            logger.info(f"Total URLs in sitemap: {len(all_found_urls)}")
            logger.info(f"Product URLs after filtering: {len(product_urls)}")
            if len(all_found_urls) > 0:
                logger.debug(f"Sample URLs: {all_found_urls[:5]}")
                logger.debug(f"Sample product URLs: {product_urls[:5]}")
        except Exception as e:
            logger.error(f"Error parsing sitemap {sitemap_url}: {e}", exc_info=True)

        return product_urls or []

    def detect_site_type(self, page: Page) -> str:
        """Detect site type by meta generator. Deprecated: use detect_cms_comprehensive instead."""
        warnings.warn(
            "detect_site_type is deprecated, use detect_cms_comprehensive instead",
            DeprecationWarning,
            stacklevel=2,
        )
        logger.debug("Detecting site type")
        try:
            gen_locator = page.locator('meta[name="generator"]')
            content = gen_locator.get_attribute("content") or ""
            content_lower = content.lower()
            if "woocommerce" in content_lower:
                logger.info("Detected WooCommerce site")
                return "woocommerce"
            if "opencart" in content_lower:
                logger.info("Detected OpenCart site")
                return "opencart"
        except Exception as e:
            logger.debug(f"Error detecting site type: {e}")

        logger.info("Detected custom site type")
        return "custom"

    def detect_by_meta_tags(self, page: Page) -> dict:
        """Detect CMS by meta tags. Deprecated: use detect_cms_comprehensive instead."""
        warnings.warn(
            "detect_by_meta_tags is deprecated, use detect_cms_comprehensive instead",
            DeprecationWarning,
            stacklevel=2,
        )
        detections = {}
        try:
            # Common meta tags
            meta_selectors = [
                'meta[name="generator"]',
                'meta[name="application-name"]',
                'meta[property="og:site_name"]',
                'meta[name="cms"]',
                'meta[name="platform"]',
            ]
            for selector in meta_selectors:
                loc = page.locator(selector)
                for i in range(loc.count()):
                    elem = loc.nth(i)
                    content = elem.get_attribute("content") or ""
                    content_lower = content.lower()
                    # WordPress
                    if "wordpress" in content_lower:
                        detections["wordpress"] = detections.get("wordpress", 0) + 1
                    # Joomla
                    if "joomla" in content_lower:
                        detections["joomla"] = detections.get("joomla", 0) + 1
                    # Magento
                    if "magento" in content_lower:
                        detections["magento"] = detections.get("magento", 0) + 1
                    # Shopify
                    if "shopify" in content_lower:
                        detections["shopify"] = detections.get("shopify", 0) + 1
                    # Bitrix
                    if "bitrix" in content_lower:
                        detections["bitrix"] = detections.get("bitrix", 0) + 1
                    # WooCommerce
                    if "woocommerce" in content_lower:
                        detections["woocommerce"] = detections.get("woocommerce", 0) + 1
                    # OpenCart
                    if "opencart" in content_lower:
                        detections["opencart"] = detections.get("opencart", 0) + 1
                    # Drupal
                    if "drupal" in content_lower:
                        detections["drupal"] = detections.get("drupal", 0) + 1
                    # PrestaShop
                    if "prestashop" in content_lower:
                        detections["prestashop"] = detections.get("prestashop", 0) + 1
                    # Squarespace
                    if "squarespace" in content_lower:
                        detections["squarespace"] = detections.get("squarespace", 0) + 1
                    # Wix
                    if "wix" in content_lower:
                        detections["wix"] = detections.get("wix", 0) + 1
        except Exception as e:
            logger.debug(f"Error in detect_by_meta_tags: {e}")
        # Calculate confidence
        if detections:
            max_count = max(detections.values())
            cms = max(detections.keys(), key=lambda k: detections[k])
            confidence = min(max_count / 3, 1.0)
            return {"cms": cms, "confidence": confidence}
        return {"cms": None, "confidence": 0.0}

    def detect_by_html_patterns(self, page: Page) -> dict:
        """Detect CMS by HTML patterns and structures. Deprecated: use detect_cms_comprehensive instead."""
        warnings.warn(
            "detect_by_html_patterns is deprecated, use detect_cms_comprehensive instead",
            DeprecationWarning,
            stacklevel=2,
        )
        detections = {}
        try:
            html_content = page.content().lower()
            # WordPress patterns
            if "wp-content" in html_content or "wp-includes" in html_content:
                detections["wordpress"] = detections.get("wordpress", 0) + 1
            if 'class="wp-' in html_content or 'id="wp-' in html_content:
                detections["wordpress"] = detections.get("wordpress", 0) + 1
            # Joomla patterns
            if "joomla" in html_content or "com_content" in html_content:
                detections["joomla"] = detections.get("joomla", 0) + 1
            if 'class="mod-' in html_content or 'id="mod-' in html_content:
                detections["joomla"] = detections.get("joomla", 0) + 1
            # Magento patterns
            if "magento" in html_content or "var formkey" in html_content:
                detections["magento"] = detections.get("magento", 0) + 1
            if 'class="product-' in html_content or "data-mage" in html_content:
                detections["magento"] = detections.get("magento", 0) + 1
            # Shopify patterns
            if "shopify" in html_content or "cdn.shopify.com" in html_content:
                detections["shopify"] = detections.get("shopify", 0) + 1
            if 'class="shopify-' in html_content or "data-shopify" in html_content:
                detections["shopify"] = detections.get("shopify", 0) + 1
            # Bitrix patterns
            if "bitrix" in html_content or "bx-" in html_content:
                detections["bitrix"] = detections.get("bitrix", 0) + 1
            # WooCommerce patterns
            if "woocommerce" in html_content or "wc-" in html_content:
                detections["woocommerce"] = detections.get("woocommerce", 0) + 1
            # OpenCart patterns
            if "opencart" in html_content or "route=product" in html_content:
                detections["opencart"] = detections.get("opencart", 0) + 1
            # Drupal patterns
            if "drupal" in html_content or "node-" in html_content:
                detections["drupal"] = detections.get("drupal", 0) + 1
            # PrestaShop patterns
            if "prestashop" in html_content or "id_product" in html_content:
                detections["prestashop"] = detections.get("prestashop", 0) + 1
            # Squarespace patterns
            if "squarespace" in html_content or "squarespace.com" in html_content:
                detections["squarespace"] = detections.get("squarespace", 0) + 1
            # Wix patterns
            if "wix" in html_content or "wix.com" in html_content:
                detections["wix"] = detections.get("wix", 0) + 1
        except Exception as e:
            logger.debug(f"Error in detect_by_html_patterns: {e}")
        # Calculate confidence
        if detections:
            max_count = max(detections.values())
            cms = max(detections.keys(), key=lambda k: detections[k])
            confidence = min(max_count / 5, 1.0)
            return {"cms": cms, "confidence": confidence}
        return {"cms": None, "confidence": 0.0}

    def detect_by_url_patterns(self, page: Page) -> dict:
        """Detect CMS by URL patterns. Deprecated: use detect_cms_comprehensive instead."""
        warnings.warn(
            "detect_by_url_patterns is deprecated, use detect_cms_comprehensive instead",
            DeprecationWarning,
            stacklevel=2,
        )
        detections = {}
        try:
            current_url = page.url.lower()
            # Get all links on the page
            loc = page.locator("a")
            urls = [current_url]
            for i in range(loc.count()):
                link = loc.nth(i)
                href = link.get_attribute("href")
                if href:
                    urls.append(href.lower())
            for url in urls:
                # WordPress
                if (
                    "/wp-admin/" in url
                    or "/wp-content/" in url
                    or "/wp-includes/" in url
                ):
                    detections["wordpress"] = detections.get("wordpress", 0) + 1
                # Joomla
                if (
                    "/administrator/" in url
                    or "/components/" in url
                    or "/modules/" in url
                ):
                    detections["joomla"] = detections.get("joomla", 0) + 1
                # Magento
                if "/media/" in url and "catalog" in url or "/skin/" in url:
                    detections["magento"] = detections.get("magento", 0) + 1
                # Shopify
                if "myshopify.com" in url or "/collections/" in url:
                    detections["shopify"] = detections.get("shopify", 0) + 1
                # Bitrix
                if "/bitrix/" in url or "/upload/" in url and "iblock" in url:
                    detections["bitrix"] = detections.get("bitrix", 0) + 1
                # WooCommerce
                if "/product/" in url or "/shop/" in url or "/cart/" in url:
                    detections["woocommerce"] = detections.get("woocommerce", 0) + 1
                # OpenCart
                if "route=product" in url or "/index.php?route=" in url:
                    detections["opencart"] = detections.get("opencart", 0) + 1
                # Drupal
                if "/node/" in url or "/user/" in url or "/admin/" in url:
                    detections["drupal"] = detections.get("drupal", 0) + 1
                # PrestaShop
                if "/product.php" in url or "/category.php" in url:
                    detections["prestashop"] = detections.get("prestashop", 0) + 1
        except Exception as e:
            logger.debug(f"Error in detect_by_url_patterns: {e}")
        # Calculate confidence
        if detections:
            max_count = max(detections.values())
            cms = max(detections.keys(), key=lambda k: detections[k])
            confidence = min(max_count / 10, 1.0)
            return {"cms": cms, "confidence": confidence}
        return {"cms": None, "confidence": 0.0}

    def detect_by_javascript_frameworks(self, page: Page) -> dict:
        """Detect CMS by JavaScript frameworks and variables. Deprecated: use detect_cms_comprehensive instead."""
        warnings.warn(
            "detect_by_javascript_frameworks is deprecated, use detect_cms_comprehensive instead",
            DeprecationWarning,
            stacklevel=2,
        )
        detections = {}
        try:
            # Evaluate JavaScript to check for global variables
            js_checks = [
                ("wordpress", 'typeof wp !== "undefined"'),
                ("joomla", 'typeof Joomla !== "undefined"'),
                ("magento", 'typeof Mage !== "undefined"'),
                ("shopify", 'typeof Shopify !== "undefined"'),
                ("bitrix", 'typeof BX !== "undefined"'),
                ("woocommerce", 'typeof woocommerce !== "undefined"'),
                ("opencart", 'typeof opencart !== "undefined"'),
                ("drupal", 'typeof Drupal !== "undefined"'),
                ("prestashop", 'typeof prestashop !== "undefined"'),
            ]
            for cms, check in js_checks:
                try:
                    result = page.evaluate(check)
                    if result:
                        detections[cms] = detections.get(cms, 0) + 1
                except (TimeoutError, RuntimeError) as e:
                    logger.debug(f"JavaScript evaluation failed for {cms} check: {e}")
            # Also check for script sources
            loc = page.locator("script")
            for i in range(loc.count()):
                script = loc.nth(i)
                src = script.get_attribute("src") or ""
                src_lower = src.lower()
                if "wp-includes" in src_lower:
                    detections["wordpress"] = detections.get("wordpress", 0) + 1
                if "joomla" in src_lower:
                    detections["joomla"] = detections.get("joomla", 0) + 1
                if "magento" in src_lower:
                    detections["magento"] = detections.get("magento", 0) + 1
                if "shopify" in src_lower:
                    detections["shopify"] = detections.get("shopify", 0) + 1
                if "bitrix" in src_lower:
                    detections["bitrix"] = detections.get("bitrix", 0) + 1
        except Exception as e:
            logger.debug(f"Error in detect_by_javascript_frameworks: {e}")
        # Calculate confidence
        if detections:
            max_count = max(detections.values())
            cms = max(detections.keys(), key=lambda k: detections[k])
            confidence = min(max_count / 3, 1.0)
            return {"cms": cms, "confidence": confidence}
        return {"cms": None, "confidence": 0.0}

    def detect_by_file_paths(self, page: Page) -> dict:
        """Detect CMS by checking file paths. Deprecated: use detect_cms_comprehensive instead."""
        warnings.warn(
            "detect_by_file_paths is deprecated, use detect_cms_comprehensive instead",
            DeprecationWarning,
            stacklevel=2,
        )
        detections = {}
        try:
            file_paths = {
                "wordpress": [
                    "/wp-admin/install.php",
                    "/wp-login.php",
                    "/wp-content/themes/",
                    "/wp-includes/js/jquery/jquery.js",
                ],
                "joomla": [
                    "/administrator/manifests/files/joomla.xml",
                    "/administrator/",
                    "/components/",
                    "/modules/",
                ],
                "magento": ["/app/etc/local.xml", "/skin/frontend/", "/media/catalog/"],
                "shopify": ["/admin/", "/collections/", "/products/"],
                "bitrix": [
                    "/bitrix/admin/",
                    "/upload/iblock/",
                    "/bitrix/php_interface/",
                ],
                "opencart": ["/admin/", "/catalog/", "/system/library/"],
                "drupal": ["/user/login", "/admin/", "/sites/default/"],
                "prestashop": ["/admin/", "/modules/", "/themes/"],
            }
            for cms, paths in file_paths.items():
                for path in paths:
                    try:
                        url = urljoin(self.base_url, path)
                        # Try to fetch with curl_cffi
                        content = self.antibot_manager.fetch_sitemap(url)
                        if content and len(content) > 0:
                            detections[cms] = detections.get(cms, 0) + 1
                    except (ConnectionError, TimeoutError, ValueError) as e:
                        logger.debug(f"Failed to fetch {url} for {cms} detection: {e}")
        except Exception as e:
            logger.debug(f"Error in detect_by_file_paths: {e}")
        # Calculate confidence
        if detections:
            max_count = max(detections.values())
            cms = max(detections.keys(), key=lambda k: detections[k])
            confidence = min(max_count / 4, 1.0)
            return {"cms": cms, "confidence": confidence}
        return {"cms": None, "confidence": 0.0}

    def detect_cms_comprehensive(self, page: Page) -> dict:
        """Comprehensive CMS detection using integrated CMSDetection module."""
        logger.info("Starting comprehensive CMS detection")
        try:
            result = self.cms_detector.detect_cms_by_patterns(
                url=page.url, html=page.content()
            )
            logger.info(
                f"Detected CMS: {result.cms_type} with confidence {result.confidence}"
            )
            return {"cms": result.cms_type, "confidence": result.confidence}
        except Exception as e:
            logger.error(f"CMS detection failed: {e}")
            return {"cms": None, "confidence": 0.0}

    def get_manual_category_products(self, page: Page) -> List[str]:
        """Fallback: extract products from manual categories when sitemap is blocked."""
        logger.info("Using manual categories fallback")
        all_products = []

        for category_path in self.manual_categories:
            category_url = urljoin(self.base_url, category_path)
            logger.info(f"Extracting products from manual category: {category_url}")

            try:
                page.goto(category_url, wait_until="networkidle")
                self.antibot_manager.human_delay(2)
                self.antibot_manager.human_scroll(page)

                # OpenCart specific product selector
                loc = page.locator(
                    '.product a[href*="/product/"], a[href*="/product"], a[href*="route=product/product"], a[href*="index.php?route=product/product"]'
                )

                category_products = []
                elements = []
                try:
                    elements = list(loc.all())
                except Exception:  # noqa: BLE001
                    pass

                if elements:
                    logger.debug(
                        f"Found {len(elements)} product elements in {category_url}"
                    )
                    iterable = elements
                else:
                    try:
                        count = loc.count()
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(
                            "Locator count unavailable for %s: %s", category_url, exc
                        )
                        count = 0
                    logger.debug(
                        f"Found {count} product elements in {category_url}"
                    )
                    iterable = [loc.nth(i) for i in range(count)] if count else []

                for elem in iterable:
                    try:
                        href = elem.get_attribute("href")
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(
                            "Failed to inspect manual category element in %s: %s",
                            category_url,
                            exc,
                        )
                        continue

                    if href and is_product_url(href) and href not in all_products:
                        category_products.append(href)
                        all_products.append(href)
                        logger.debug(f"Added product from manual category: {href}")

                logger.info(
                    f"Extracted {len(category_products)} products from {category_url}"
                )

            except Exception as e:
                logger.error(
                    f"Error extracting from manual category {category_url}: {e}"
                )
                continue

        logger.info(f"Total products from manual categories: {len(all_products)}")
        return all_products

    # ------------------------------------------------------------------
    # Lightweight HTTP-based discovery
    # ------------------------------------------------------------------

    def get_product_urls_from_sitemap(
        self, base_url: Optional[str] = None, max_products: int = 200
    ) -> List[str]:
        base = (base_url or self.base_url).rstrip("/")
        if not base:
            return []

        product_urls: List[str] = []
        seen: Set[str] = set()
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        }

        sitemap_patterns = self.settings.get(
            "sitemap_patterns", ["/sitemap.xml", "/sitemap_index.xml"]
        )

        with httpx.Client(timeout=15, headers=headers, follow_redirects=True) as client:
            for pattern in sitemap_patterns + ["/sitemap.xml", "/sitemap_index.xml"]:
                sitemap_url = urljoin(base + "/", pattern.lstrip("/"))
                try:
                    resp = client.get(sitemap_url)
                except httpx.HTTPError as exc:
                    logger.debug(f"Sitemap request failed for {sitemap_url}: {exc}")
                    continue

                if resp.status_code >= 400 or "xml" not in resp.headers.get(
                    "content-type", ""
                ) and not resp.text.strip().startswith("<?xml"):
                    continue

                logger.info(f"Parsing sitemap at {sitemap_url}")
                parsed = self._parse_sitemap_xml(resp.text, base, max_products - len(seen))
                for url in parsed:
                    if url not in seen:
                        seen.add(url)
                        product_urls.append(url)
                        if len(product_urls) >= max_products:
                            return product_urls[:max_products]

        if product_urls:
            return product_urls[:max_products]

        # HTTP crawling fallback when sitemap missing or insufficient
        return self._discover_products_via_http(base, max_products)

    def _parse_sitemap_xml(
        self, xml_content: str, base_url: str, remaining: int
    ) -> List[str]:
        results: List[str] = []
        if remaining <= 0:
            return results

        try:
            root = ET.fromstring(xml_content)
        except ET.ParseError as exc:
            logger.debug(f"Sitemap XML parse error: {exc}")
            return results

        # remove namespaces
        for elem in root.iter():
            if "}" in elem.tag:
                elem.tag = elem.tag.split("}", 1)[1]

        base_domain = urlparse(base_url).netloc

        if root.tag == "sitemapindex":
            for loc in root.findall(".//loc"):
                if not loc.text:
                    continue
                try:
                    resp = httpx.get(loc.text, timeout=15)
                except httpx.HTTPError:
                    continue
                if resp.status_code == 200 and resp.text.strip().startswith("<?xml"):
                    nested = self._parse_sitemap_xml(
                        resp.text, base_url, remaining - len(results)
                    )
                    for url in nested:
                        if url not in results:
                            results.append(url)
                        if len(results) >= remaining:
                            return results
        elif root.tag == "urlset":
            for url_elem in root.findall(".//url"):
                loc_elem = url_elem.find("loc")
                if loc_elem is None or not loc_elem.text:
                    continue
                absolute = loc_elem.text.strip()
                if urlparse(absolute).netloc and urlparse(absolute).netloc != base_domain:
                    continue
                if is_product_url(absolute) or "product" in absolute or "catalog" in absolute:
                    absolute = absolute.split("#")[0]
                    if absolute not in results:
                        results.append(absolute)
                if len(results) >= remaining:
                    break

        return results[:remaining]

    def _discover_products_via_http(
        self, base_url: str, max_products: int
    ) -> List[str]:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        }

        queue: deque[str] = deque([base_url])
        visited: Set[str] = set()
        product_urls: List[str] = []
        base_domain = urlparse(base_url).netloc

        product_selectors = [
            "li.product a[href]",
            "a.woocommerce-LoopProduct-link",
            "a.products-full-list__link",
            "a.catalogue__product-link",
            "a[href*='index.php?route=product/product']",
            "a[href*='/product/']",
            "a[href*='/pryaja/']",
        ]

        category_selectors = [
            "a[href*='/catalog']",
            "a[href*='/category']",
            "a[href*='/collection']",
            "a[href*='/pryaja']",
            "nav a[href]",
        ]

        with httpx.Client(timeout=15, headers=headers, follow_redirects=True) as client:
            while queue and len(product_urls) < max_products:
                current = queue.popleft()
                if current in visited:
                    continue
                visited.add(current)

                try:
                    response = client.get(current)
                except httpx.HTTPError as exc:
                    logger.debug(f"HTTP discovery failed for {current}: {exc}")
                    continue

                if response.status_code >= 400:
                    continue

                html = response.text
                soup = BeautifulSoup(html, "html.parser")

                if self._looks_like_product_page(soup):
                    if current not in product_urls:
                        product_urls.append(current)
                    continue

                for selector in product_selectors:
                    for link in soup.select(selector):
                        href = link.get("href")
                        if not href:
                            continue
                        absolute = urljoin(current, href).split("#")[0]
                        if urlparse(absolute).netloc != base_domain:
                            continue
                        if absolute in product_urls:
                            continue
                        if is_product_url(absolute) or any(
                            pattern in absolute for pattern in ["product", "catalog", "pryaja", "product_id="]
                        ):
                            product_urls.append(absolute)
                            if len(product_urls) >= max_products:
                                break
                    if len(product_urls) >= max_products:
                        break

                if len(product_urls) >= max_products:
                    break

                for selector in category_selectors:
                    for link in soup.select(selector):
                        href = link.get("href")
                        if not href:
                            continue
                        absolute = urljoin(current, href).split("#")[0]
                        if urlparse(absolute).netloc != base_domain:
                            continue
                        if absolute not in visited and absolute not in queue:
                            queue.append(absolute)

                # pagination links
                for link in soup.select("a[href*='page=']"):
                    href = link.get("href")
                    if not href:
                        continue
                    absolute = urljoin(current, href).split("#")[0]
                    if urlparse(absolute).netloc != base_domain:
                        continue
                    if absolute not in visited and absolute not in queue:
                        queue.append(absolute)

        return product_urls[:max_products]

    @staticmethod
    def _looks_like_product_page(soup: BeautifulSoup) -> bool:
        if soup.select_one('[itemprop="price"]'):
            return True
        if soup.select_one('form[id^="product"] select[name^="option"]'):
            return True
        if soup.select_one('.product-page__input-box select[name^="option"]'):
            return True
        if soup.find('meta', attrs={'property': 'og:type', 'content': 'product'}):
            return True
        return False

    def find_category_urls(self, page: Page, base_url: str) -> List[str]:
        """Find category URLs using CSS selectors."""
        logger.info("Finding category URLs")
        categories = []
        selectors = [
            'nav a[href*="/category/"]',
            ".menu-category a",
            ".category-menu a",
            'a[href*="/catalog/"]',
            ".navbar-nav a",
            ".main-menu a",
            'a[href*="/shop/"]',
            'a[href*="/collection/"]',
            'a:has-text("Каталог")',
            # OpenCart specific
            ".category-list a",
            'a[href*="/index.php?route=product/category"]',
            ".top-menu a",
            ".header-menu a",
            ".menu a",
            ".categories a",
            "li.level-0 a",
            ".menu-category > a",
            'a[title*="категория"]',
            'a[title*="category"]',
        ]

        page.goto(base_url, wait_until="networkidle")
        self.antibot_manager.human_delay(3)
        self.antibot_manager.human_scroll(page)

        # Log page content for debugging
        content_preview = page.content()[:1000]
        print(f"MAIN PAGE CONTENT PREVIEW:\n{content_preview}\nEND PREVIEW")
        logger.debug(f"Main page content preview: {content_preview}")

        # Wait for potential dynamic content
        try:
            page.wait_for_load_state("networkidle")
            time.sleep(2)
        except (TimeoutError, RuntimeError) as e:
            logger.debug(f"Error waiting for page load state: {e}")

        for selector in selectors:
            try:
                loc = page.locator(selector)
                logger.debug(f"Selector '{selector}' found {loc.count()} elements")
                for i in range(loc.count()):
                    elem = loc.nth(i)
                    href = elem.get_attribute("href")
                    if href and base_url in href and href not in categories:
                        categories.append(href)
                        logger.debug(f"Added category: {href}")
            except Exception as e:
                logger.debug(f"Selector error '{selector}': {e}")
                continue

        # Fallback: links with category-like text
        try:
            loc = page.locator("a")
            category_keywords = [
                "каталог",
                "category",
                "shop",
                "магазин",
                "коллекция",
                "collection",
            ]
            for i in range(loc.count()):
                link = loc.nth(i)
                text = link.inner_text().lower()
                href = link.get_attribute("href")
                if (
                    href
                    and any(keyword in text for keyword in category_keywords)
                    and base_url in href
                    and href not in categories
                ):
                    categories.append(href)
                    logger.debug(f"Added category by text: {href}")
        except Exception as e:
            logger.debug(f"Text-based category search error: {e}")

        logger.info(f"Found {len(categories)} category URLs")
        return list(set(categories))

    def extract_product_urls(
        self, category_url: str, page: Page, max_pages: int = 5
    ) -> List[str]:
        """Extract product URLs from category pages with pagination."""
        logger.info(
            f"Extracting products from category: {category_url}, max_pages: {max_pages}"
        )
        products = []
        self.antibot_manager.human_delay()

        for current_page_num in range(1, max_pages + 1):
            try:
                if current_page_num > 1:
                    page_url = f"{category_url}?page={current_page_num}"
                else:
                    page_url = category_url

                page.goto(page_url)
                self.antibot_manager.human_delay()
                self.antibot_manager.human_scroll(page)

                # Enhanced product selectors including OpenCart
                product_selectors = [
                    ".product-item a",
                    ".goods-card a",
                    '.product a[href*="/product/"]',
                    ".item a",
                    'a[href*="/product"]',
                    'a[href*="/item"]',
                    ".product-link",
                    ".product-title a",
                    ".catalog-item a",
                    "div.product a",
                    ".card a",
                    ".product-grid a",
                    # OpenCart specific
                    ".product-thumb a",
                    ".product-list a",
                    "a.product-name",
                ]

                found_products = False
                for selector in product_selectors:
                    try:
                        loc = page.locator(selector)
                        logger.debug(
                            f"Selector '{selector}' found {loc.count()} elements on {page_url}"
                        )
                        for i in range(loc.count()):
                            elem = loc.nth(i)
                            href = elem.get_attribute("href")
                            if href and is_product_url(href) and href not in products:
                                products.append(href)
                                logger.debug(f"Added product: {href}")
                        if loc.count() > 0:
                            found_products = True
                            logger.debug(f"Found products with selector: {selector}")
                            break
                    except Exception as e:
                        logger.debug(f"Selector error '{selector}': {e}")
                        continue

                if not found_products:
                    logger.debug("No products found on this page")
                    break

                # Check for next page
                next_selectors = [
                    ".next",
                    'a[rel="next"]',
                    ".pagination .next",
                    ".pager-next",
                    'a[aria-label="Next"]',
                    ".pagination-next",
                ]
                has_next = False
                for selector in next_selectors:
                    try:
                        next_locator = page.locator(selector).first
                        if next_locator.is_visible():
                            has_next = True
                            logger.debug(
                                f"Found next page indicator with selector: {selector}"
                            )
                            break
                    except Exception:
                        continue

                if not has_next:
                    logger.debug("No next page found")
                    break

            except Exception as e:
                logger.error(
                    f"Error extracting products from {page_url}: {e}", exc_info=True
                )
                break

        logger.info(
            f"Extracted {len(products)} product URLs from category {category_url}"
        )
        return list(set(products))
