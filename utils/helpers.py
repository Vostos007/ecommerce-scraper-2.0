import re
import time
import logging
from importlib import import_module
from typing import Optional, List, Dict, Union, Tuple, Any, TYPE_CHECKING
from urllib.parse import urlparse, urlsplit
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select
from selenium.webdriver.common.action_chains import ActionChains
from selenium.common.exceptions import NoSuchElementException
import json

if TYPE_CHECKING:  # pragma: no cover - typing only
    from bs4 import BeautifulSoup

# Configure logging
logger = logging.getLogger(__name__)

# Cache for extract_with_bs4
_bs4_cache: Dict[str, Any] = {}
_BS4_MODULE: Any | None = None


def _load_bs4() -> Any:
    """Lazy loader for BeautifulSoup to avoid cold-start penalty (ARCH-008)."""

    global _BS4_MODULE
    if _BS4_MODULE is None:
        _BS4_MODULE = import_module("bs4")
    return _BS4_MODULE


def _build_soup(html: str) -> "BeautifulSoup":
    return _load_bs4().BeautifulSoup(html, "html.parser")


GUARD_MARKERS = (
    "ddos-guard",
    "checking your browser",
    "attention required",
    "cloudflare",
    "please enable javascript",
)

GUARD_SNIPPETS = (
    'meta name="robots" content="noindex"',
    "adblock-blocker",
    "/protect",
    "var adb = 1",
)


def looks_like_guard_html(html: Optional[str]) -> bool:
    """Detect common anti-bot interstitial responses."""

    if not isinstance(html, str) or not html:
        return False

    lowered = html.lower()
    if any(marker in lowered for marker in GUARD_MARKERS):
        return True
    return any(snippet in lowered for snippet in GUARD_SNIPPETS)


def clean_price(price_text: str) -> Optional[float]:
    """Clean and parse price text to float with enhanced currency and decimal support."""
    if not isinstance(price_text, str) or not price_text.strip():
        logger.warning("Invalid price_text input: %s", price_text)
        return None

    try:
        # Enhanced regex to handle various formats
        # Remove currency symbols: €, $, £, ¥, ₽, and others
        cleaned = re.sub(r"[€$£¥₽\s]", "", price_text.strip())

        # Handle decimal separators: replace comma with dot if it's a decimal separator
        # Assume comma is decimal if there's only one comma and no dot, or if comma is followed by 2 digits
        if "," in cleaned and "." not in cleaned:
            # Check if comma is likely decimal (e.g., 123,45)
            parts = cleaned.split(",")
            if len(parts) == 2 and len(parts[1]) <= 2 and parts[1].isdigit():
                cleaned = cleaned.replace(",", ".")
            else:
                # Remove comma if it's thousands separator
                cleaned = cleaned.replace(",", "")

        # Extract numeric value with optional decimal
        match = re.search(r"(\d+(?:\.\d{1,2})?)", cleaned)
        if not match:
            logger.warning("No numeric value found in price_text: %s", price_text)
            return None

        price = float(match.group(1))

        # Basic validation: reasonable price range
        if price < 0 or price > 1000000:  # Adjust range as needed
            logger.warning("Price out of reasonable range: %f", price)
            return None

        return price

    except (ValueError, AttributeError) as e:
        logger.error("Error parsing price '%s': %s", price_text, e)
        return None


def parse_stock(stock_text: str) -> Optional[int]:
    """Parse stock text to integer with enhanced patterns and validation."""
    if not isinstance(stock_text, str) or not stock_text.strip():
        logger.warning("Invalid stock_text input: %s", stock_text)
        return None

    try:
        stock_lower = stock_text.lower().strip()

        # First, try to extract quantity if present
        match = re.search(r"(\d+)", stock_lower)
        if match:
            quantity = int(match.group(1))
            # Validate reasonable quantity
            if 0 <= quantity <= 10000:
                return quantity

        # Enhanced stock status patterns (fallback when no quantity found)
        if any(
            phrase in stock_lower
            for phrase in ["в наличии", "in stock", "available", "есть"]
        ):
            return -1  # Available without explicit quantity
        elif any(
            phrase in stock_lower
            for phrase in [
                "нет в наличии",
                "out of stock",
                "unavailable",
                "отсутствует",
            ]
        ):
            return 0  # Out of stock
        elif any(
            phrase in stock_lower
            for phrase in ["unlimited", "много", "большое количество", "неограничено"]
        ):
            return -1  # Treat unlimited stock as open-ended

        logger.warning("Unable to parse stock_text: %s", stock_text)
        return None

    except Exception as e:
        logger.error("Error parsing stock '%s': %s", stock_text, e)
        return None


def get_variation_type(label_text: str) -> str:
    """Return the primary variation type label for compatibility with legacy code."""
    details = get_variation_type_details(label_text)
    return details.get("type", "unknown")


def get_variation_type_details(label_text: str) -> Dict[str, Union[str, float]]:
    """Determine variation type from label text with confidence scoring and structured results."""
    if not isinstance(label_text, str) or not label_text.strip():
        logger.warning("Invalid label_text input: %s", label_text)
        return {"type": "unknown", "confidence": 0.0}

    try:
        label_lower = label_text.lower().strip()
        tokens = set(re.findall(r"[a-z0-9а-яё]+", label_lower))

        # Comprehensive keyword lists with multilingual support
        size_keywords = [
            "размер",
            "size",
            "разм",
            "s/m/l",
            "xs",
            "s",
            "m",
            "l",
            "xl",
            "xxl",
            "размеры",
            "sizes",
            "объем",
            "volume",
            "вес",
            "weight",
        ]
        color_keywords = [
            "цвет",
            "color",
            "colour",
            "красный",
            "red",
            "синий",
            "blue",
            "зеленый",
            "green",
            "черный",
            "black",
            "белый",
            "white",
            "желтый",
            "yellow",
            "оранжевый",
            "orange",
            "фиолетовый",
            "purple",
        ]
        model_keywords = [
            "модель",
            "model",
            "стиль",
            "style",
            "тип",
            "type",
            "вариант",
            "variant",
            "серия",
            "series",
            "линейка",
            "line",
        ]

        # Count matches for confidence scoring
        def keyword_matches(keyword: str) -> bool:
            kw = keyword.lower()
            if kw in tokens:
                return True
            return len(kw) > 2 and kw in label_lower

        size_matches = sum(1 for keyword in size_keywords if keyword_matches(keyword))
        color_matches = sum(1 for keyword in color_keywords if keyword_matches(keyword))
        model_matches = sum(1 for keyword in model_keywords if keyword_matches(keyword))

        # Determine type with confidence
        max_matches = max(size_matches, color_matches, model_matches)
        if max_matches == 0:
            return {"type": "unknown", "confidence": 0.0}

        confidence = min(
            max_matches / len(label_lower.split()), 1.0
        )  # Simple confidence metric

        if size_matches == max_matches:
            return {"type": "size", "confidence": confidence}
        elif color_matches == max_matches:
            return {"type": "color", "confidence": confidence}
        else:
            return {"type": "model", "confidence": confidence}

    except Exception as e:
        logger.error("Error determining variation type for '%s': %s", label_text, e)
        return {"type": "unknown", "confidence": 0.0}


def select_variation(driver: webdriver.Chrome, selector: str, value: str) -> bool:
    """Select variation by clicking or using Select with enhanced error handling."""
    if not isinstance(selector, str) or not isinstance(value, str):
        logger.error(
            "Invalid selector or value: selector=%s, value=%s", selector, value
        )
        return False

    try:
        element = driver.find_element(By.CSS_SELECTOR, selector)

        # Check if it's a select element
        if element.tag_name == "select":
            select = Select(element)
            select.select_by_visible_text(value)
            return True

        # Otherwise, try to click the button/swatch
        else:
            # Try to find the specific option that matches the value
            # Prefer CSS attribute selectors first
            try:
                option_element = element.find_element(
                    By.CSS_SELECTOR, f"[data-value='{value}']"
                )
            except NoSuchElementException:
                try:
                    option_element = element.find_element(
                        By.CSS_SELECTOR, f"[title='{value}']"
                    )
                except NoSuchElementException:
                    try:
                        # Use XPath for text matching within the same container
                        option_element = element.find_element(
                            By.XPATH, f".//*[contains(text(), '{value}')]"
                        )
                    except NoSuchElementException:
                        # Fallback: click the main element if it represents the value
                        option_element = element
            ActionChains(driver).move_to_element(option_element).click().perform()
            time.sleep(0.5)
            return True

    except Exception as e:
        logger.error(
            "Error selecting variation %s with selector %s: %s", value, selector, e
        )
        return False


def human_delay(
    driver: webdriver.Chrome, min_delay: float = 1.0, max_delay: float = 3.0
) -> None:
    """Apply human-like delay with random variation."""
    import random

    if not isinstance(min_delay, (int, float)) or not isinstance(
        max_delay, (int, float)
    ):
        logger.warning("Invalid delay parameters: min=%s, max=%s", min_delay, max_delay)
        return
    try:
        delay = random.uniform(min_delay, max_delay)
        time.sleep(delay)
    except Exception as e:
        logger.error("Error applying human delay: %s", e)


def is_product_url(url: str) -> bool:
    """Check if URL is likely a product page with input validation."""
    if not isinstance(url, str):
        logger.warning("Invalid URL input: %s", url)
        return False

    try:
        lowered = url.lower()

        if "/catalog/" in lowered:
            path = urlsplit(lowered).path.strip("/")
            segments = [segment for segment in path.split("/") if segment]
            if len(segments) >= 3:
                last_segment = segments[-1]
                if any(char.isdigit() for char in last_segment):
                    return True

        product_patterns = [
            "/product/",
            "/item/",
            "/goods/",
            "?id=",
            "/p/",
            "/shop/",
        ]

        return any(pattern in lowered for pattern in product_patterns)
    except Exception as e:
        logger.error("Error checking product URL '%s': %s", url, e)
        return False


def parse_robots_txt(robots_content: str) -> Dict[str, List[str]]:
    """Parse robots.txt content with error handling."""
    if not isinstance(robots_content, str):
        logger.warning("Invalid robots_content input")
        return {"allow": [], "disallow": [], "sitemap": []}

    try:
        rules = {"allow": [], "disallow": [], "sitemap": []}

        for line in robots_content.split("\n"):
            line = line.strip()
            if line.startswith("Allow:"):
                rules["allow"].append(line.split(":", 1)[1].strip())
            elif line.startswith("Disallow:"):
                rules["disallow"].append(line.split(":", 1)[1].strip())
            elif line.startswith("Sitemap:"):
                rules["sitemap"].append(line.split(":", 1)[1].strip())

        return rules
    except Exception as e:
        logger.error("Error parsing robots.txt: %s", e)
        return {"allow": [], "disallow": [], "sitemap": []}


def load_config(path: str = "config/settings.json") -> Dict[str, Any]:
    """Load configuration from JSON file with error handling."""
    if not isinstance(path, str):
        logger.error("Invalid config path: %s", path)
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.error("Error loading config from %s: %s", path, e)
        return {}


def extract_with_bs4(
    html: str,
    selectors: List[str],
    tag_attr: str = "text",
    timeout: Optional[float] = None,
) -> Optional[str]:
    """Extract text using BeautifulSoup with multiple selectors, caching, and enhanced error handling."""
    if not isinstance(html, str) or not isinstance(selectors, list):
        logger.error("Invalid html or selectors input")
        return None

    try:
        # Selector validation
        valid_selectors = [s for s in selectors if isinstance(s, str) and s.strip()]
        if not valid_selectors:
            logger.warning("No valid selectors provided")
            return None

        # Check cache
        cache_key = f"{hash(html)}:{','.join(valid_selectors)}:{tag_attr}"
        if cache_key in _bs4_cache:
            return _bs4_cache[cache_key]

        soup = _build_soup(html)

        for selector in valid_selectors:
            try:
                element = soup.select_one(selector)
                if element:
                    if tag_attr == "text":
                        result = element.get_text(strip=True)
                    else:
                        attr_value = element.get(tag_attr)
                        result = (
                            attr_value
                            if isinstance(attr_value, str)
                            else str(attr_value) if attr_value is not None else None
                        )

                    if result:
                        # Cache result
                        _bs4_cache[cache_key] = result
                        return result
            except Exception as e:
                logger.warning("Error with selector '%s': %s", selector, e)
                continue

        logger.warning("No element found with provided selectors")
        return None

    except Exception as e:
        logger.error("Error extracting with bs4: %s", e)
        return None


def validate_url(url: str) -> bool:
    """Validate if the given string is a valid URL."""
    if not isinstance(url, str):
        return False

    try:
        result = urlparse(url)
        return all([result.scheme, result.netloc])
    except Exception as e:
        logger.error("Error validating URL '%s': %s", url, e)
        return False


def sanitize_text(text: str) -> str:
    """Sanitize text by removing unwanted characters and normalizing whitespace."""
    if not isinstance(text, str):
        logger.warning("Invalid text input for sanitization: %s", text)
        return ""

    try:
        # Remove control characters and normalize whitespace
        sanitized = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", text)
        sanitized = re.sub(r"\s+", " ", sanitized).strip()
        return sanitized
    except Exception as e:
        logger.error("Error sanitizing text: %s", e)
        return text  # Return original on error


def safe_float_conversion(value: Any) -> Optional[float]:
    """Safely convert value to float with error handling."""
    if value is None:
        return None

    try:
        if isinstance(value, (int, float)):
            return float(value)
        elif isinstance(value, str):
            cleaned = value.strip()
            if not cleaned:
                return None
            return float(cleaned.replace(",", "."))
        else:
            return float(value)
    except (ValueError, TypeError) as e:
        logger.warning("Error converting to float: %s (%s)", value, e)
        return None


def safe_int_conversion(value: Any) -> Optional[int]:
    """Safely convert value to int with error handling."""
    if value is None:
        return None

    try:
        if isinstance(value, (int, float)):
            return int(value)
        elif isinstance(value, str):
            cleaned = value.strip()
            if not cleaned:
                return None
            return int(float(cleaned.replace(",", ".")))
        else:
            return int(value)
    except (ValueError, TypeError) as e:
        logger.warning("Error converting to int: %s (%s)", value, e)
        return None


# ===== PROXY INFRASTRUCTURE UTILITY FUNCTIONS =====


def parse_proxy_url(proxy_url: str) -> Optional[Dict[str, Any]]:
    """
    Parse proxy URL into structured components with validation.

    Args:
        proxy_url: Proxy URL to parse (e.g., 'http://user:pass@host:port')

    Returns:
        Dictionary with proxy components or None if invalid
    """
    if not validate_proxy_format(proxy_url):
        return None

    try:
        parsed = urlparse(proxy_url)
        return {
            "scheme": parsed.scheme,
            "host": parsed.hostname,
            "port": parsed.port,
            "username": parsed.username,
            "password": parsed.password,
            "url": proxy_url,
            "has_auth": bool(parsed.username and parsed.password),
        }
    except Exception as e:
        logger.error(f"Error parsing proxy URL {proxy_url}: {e}")
        return None


def format_proxy_for_requests(proxy_data: Dict[str, Any]) -> Dict[str, str]:
    """
    Format proxy data for use with requests library.

    Args:
        proxy_data: Dictionary with proxy information

    Returns:
        Dictionary formatted for requests library
    """
    try:
        if isinstance(proxy_data, str):
            # If string provided, parse it first
            parsed = parse_proxy_url(proxy_data)
            if not parsed:
                return {}
            proxy_data = parsed

        scheme = proxy_data.get("scheme", "http")
        host = proxy_data.get("host")
        port = proxy_data.get("port")
        username = proxy_data.get("username")
        password = proxy_data.get("password")

        if not host or not port:
            logger.warning("Missing host or port in proxy data")
            return {}

        if username and password:
            proxy_url = f"{scheme}://{username}:{password}@{host}:{port}"
        else:
            proxy_url = f"{scheme}://{host}:{port}"

        return {"http": proxy_url, "https": proxy_url}

    except Exception as e:
        logger.error(f"Error formatting proxy for requests: {e}")
        return {}


def format_proxy_for_aiohttp(proxy_data: Dict[str, Any]) -> Optional[str]:
    """
    Format proxy data for use with aiohttp.

    Args:
        proxy_data: Dictionary with proxy information

    Returns:
        Proxy URL string for aiohttp or None if invalid
    """
    try:
        if isinstance(proxy_data, str):
            # If string provided, validate and return if valid
            if validate_proxy_format(proxy_data):
                return proxy_data
            return None

        scheme = proxy_data.get("scheme", "http")
        host = proxy_data.get("host")
        port = proxy_data.get("port")
        username = proxy_data.get("username")
        password = proxy_data.get("password")

        if not host or not port:
            logger.warning("Missing host or port in proxy data")
            return None

        if username and password:
            return f"{scheme}://{username}:{password}@{host}:{port}"
        else:
            return f"{scheme}://{host}:{port}"

    except Exception as e:
        logger.error(f"Error formatting proxy for aiohttp: {e}")
        return None


def validate_proxy_format(proxy_url: str) -> bool:
    """
    Validate proxy URL format.

    Args:
        proxy_url: Proxy URL to validate

    Returns:
        True if format is valid
    """
    if not isinstance(proxy_url, str) or not proxy_url.strip():
        return False

    try:
        parsed = urlparse(proxy_url)

        # Check required components
        if not parsed.scheme:
            return False
        if not parsed.hostname:
            return False
        if not parsed.port:
            return False

        # Validate scheme
        valid_schemes = ["http", "https", "socks4", "socks5"]
        if parsed.scheme.lower() not in valid_schemes:
            return False

        # Validate port range
        if not (1 <= parsed.port <= 65535):
            return False

        return True

    except Exception:
        return False


def extract_proxy_credentials(proxy_url: str) -> Optional[Tuple[str, str]]:
    """
    Extract authentication credentials from proxy URL.

    Args:
        proxy_url: Proxy URL containing credentials

    Returns:
        Tuple of (username, password) or None if no credentials
    """
    try:
        parsed = urlparse(proxy_url)
        if parsed.username and parsed.password:
            return (parsed.username, parsed.password)
        return None
    except Exception as e:
        logger.error(f"Error extracting proxy credentials: {e}")
        return None


# ===== SESSION UTILITY FUNCTIONS =====


def encrypt_session_data(data: Dict[str, Any], key: str) -> Optional[str]:
    """
    Encrypt session data for secure storage.

    Args:
        data: Session data to encrypt
        key: Encryption key

    Returns:
        Encrypted data as base64 string or None if failed
    """
    try:
        import base64
        from cryptography.fernet import Fernet

        # Ensure key is properly formatted
        if not key:
            logger.error("No encryption key provided")
            return None

        # Convert data to JSON string
        json_data = json.dumps(data, separators=(",", ":"))

        # Encrypt data
        fernet = Fernet(key.encode() if isinstance(key, str) else key)
        encrypted_data = fernet.encrypt(json_data.encode())

        # Return as base64 string
        return base64.urlsafe_b64encode(encrypted_data).decode()

    except Exception as e:
        logger.error(f"Error encrypting session data: {e}")
        return None


def decrypt_session_data(encrypted_data: str, key: str) -> Optional[Dict[str, Any]]:
    """
    Decrypt session data from secure storage.

    Args:
        encrypted_data: Base64 encoded encrypted data
        key: Decryption key

    Returns:
        Decrypted session data or None if failed
    """
    try:
        import base64
        from cryptography.fernet import Fernet

        if not encrypted_data or not key:
            return None

        # Decode from base64
        decoded_data = base64.urlsafe_b64decode(encrypted_data.encode())

        # Decrypt data
        fernet = Fernet(key.encode() if isinstance(key, str) else key)
        decrypted_data = fernet.decrypt(decoded_data)

        # Parse JSON
        return json.loads(decrypted_data.decode())

    except Exception as e:
        logger.error(f"Error decrypting session data: {e}")
        return None


def generate_session_key() -> str:
    """
    Generate encryption key for session data.

    Returns:
        Base64 encoded encryption key
    """
    try:
        from cryptography.fernet import Fernet

        return Fernet.generate_key().decode()
    except Exception as e:
        logger.error(f"Error generating session key: {e}")
        return ""


def validate_session_integrity(session_data: Dict[str, Any]) -> bool:
    """
    Validate session data integrity.

    Args:
        session_data: Session data to validate

    Returns:
        True if session data is valid
    """
    try:
        if not isinstance(session_data, dict):
            return False

        # Check required fields
        required_fields = ["domain", "created_at"]
        if not all(field in session_data for field in required_fields):
            return False

        # Validate domain
        domain = session_data.get("domain")
        if not domain or not isinstance(domain, str):
            return False

        # Validate timestamps
        created_at = session_data.get("created_at")
        if not created_at:
            return False

        # Check data types
        cookies = session_data.get("cookies", {})
        headers = session_data.get("headers", {})

        if not isinstance(cookies, dict) or not isinstance(headers, dict):
            return False

        return True

    except Exception as e:
        logger.error(f"Error validating session integrity: {e}")
        return False


# ===== CONTENT ANALYSIS FUNCTIONS =====


def analyze_html_structure(html: str) -> Dict[str, Any]:
    """
    Analyze HTML structure completeness.

    Args:
        html: HTML content to analyze

    Returns:
        Dictionary with structure analysis
    """
    try:
        if not html or not isinstance(html, str):
            return {"valid": False, "reason": "Empty or invalid HTML"}

        soup = _build_soup(html)

        # Basic structure checks
        has_html = bool(soup.find("html"))
        has_head = bool(soup.find("head"))
        has_body = bool(soup.find("body"))
        has_title = bool(soup.find("title"))

        # Content analysis
        text_content = soup.get_text(strip=True)
        text_length = len(text_content)

        # Element counts
        element_count = len(soup.find_all())
        link_count = len(soup.find_all("a"))
        image_count = len(soup.find_all("img"))
        script_count = len(soup.find_all("script"))

        # Calculate structure score
        structure_score = 0
        if has_html:
            structure_score += 25
        if has_head:
            structure_score += 25
        if has_body:
            structure_score += 25
        if has_title:
            structure_score += 25

        return {
            "valid": structure_score >= 50,
            "structure_score": structure_score,
            "has_html": has_html,
            "has_head": has_head,
            "has_body": has_body,
            "has_title": has_title,
            "text_length": text_length,
            "element_count": element_count,
            "link_count": link_count,
            "image_count": image_count,
            "script_count": script_count,
            "text_to_html_ratio": text_length / len(html) if html else 0,
        }

    except Exception as e:
        logger.error(f"Error analyzing HTML structure: {e}")
        return {"valid": False, "reason": f"Analysis error: {str(e)}"}


def detect_javascript_challenges(html: str) -> Dict[str, Any]:
    """
    Detect JavaScript challenges and anti-bot measures.

    Args:
        html: HTML content to analyze

    Returns:
        Dictionary with challenge detection results
    """
    try:
        if not html or not isinstance(html, str):
            return {"has_challenges": False, "challenges": []}

        html_lower = html.lower()
        challenges = []

        # Common JavaScript challenge patterns
        js_challenge_patterns = [
            ("cloudflare", ["cloudflare", "cf-browser-verification", "cf-ray"]),
            ("ddos_guard", ["ddos-guard", "ddosguard", "checking your browser"]),
            ("captcha", ["captcha", "recaptcha", "hcaptcha"]),
            (
                "bot_detection",
                ["bot detected", "automated traffic", "suspicious activity"],
            ),
            ("rate_limiting", ["rate limited", "too many requests", "slow down"]),
            ("js_redirect", ["window.location", "document.location", "location.href"]),
            ("loading_screen", ["loading...", "please wait", "redirecting"]),
        ]

        for challenge_type, patterns in js_challenge_patterns:
            if any(pattern in html_lower for pattern in patterns):
                challenges.append(challenge_type)

        # Check for common challenge indicators
        soup = _build_soup(html)

        # Check for meta refresh
        meta_refresh = soup.find("meta", {"http-equiv": "refresh"})
        if meta_refresh:
            challenges.append("meta_refresh")

        # Check for challenge forms
        challenge_forms = soup.find_all(
            "form", id=re.compile(r"challenge|captcha|verify", re.I)
        )
        if challenge_forms:
            challenges.append("challenge_form")

        # Check for noscript warnings
        noscript = soup.find_all("noscript")
        if noscript and any("javascript" in tag.get_text().lower() for tag in noscript):
            challenges.append("noscript_warning")

        return {
            "has_challenges": len(challenges) > 0,
            "challenges": list(set(challenges)),  # Remove duplicates
            "challenge_count": len(set(challenges)),
        }

    except Exception as e:
        logger.error(f"Error detecting JavaScript challenges: {e}")
        return {"has_challenges": False, "challenges": [], "error": str(e)}


def calculate_content_entropy(text: str) -> float:
    """
    Calculate content randomness/entropy to detect generated content.

    Args:
        text: Text content to analyze

    Returns:
        Entropy score (0.0 to 1.0, higher = more random)
    """
    try:
        if not text or not isinstance(text, str):
            return 0.0

        import math
        from collections import Counter

        # Clean text
        text = re.sub(r"\s+", " ", text.strip())
        if len(text) < 10:  # Too short to analyze
            return 0.0

        # Calculate character frequency
        char_counts = Counter(text.lower())
        text_length = len(text)

        # Calculate Shannon entropy
        entropy = 0.0
        for count in char_counts.values():
            probability = count / text_length
            if probability > 0:
                entropy -= probability * math.log2(probability)

        # Normalize to 0-1 range (max entropy for ASCII is ~6.6 bits)
        max_entropy = math.log2(min(256, len(char_counts)))
        normalized_entropy = entropy / max_entropy if max_entropy > 0 else 0.0

        return min(1.0, max(0.0, normalized_entropy))

    except Exception as e:
        logger.error(f"Error calculating content entropy: {e}")
        return 0.0


def extract_error_messages(html: str) -> List[str]:
    """
    Extract error and blocking messages from HTML content.

    Args:
        html: HTML content to analyze

    Returns:
        List of detected error/block messages
    """
    try:
        if not html or not isinstance(html, str):
            return []

        soup = _build_soup(html)
        error_messages = []

        # Common error/block message patterns
        error_patterns = [
            r"access denied",
            r"blocked",
            r"forbidden",
            r"rate limit",
            r"too many requests",
            r"captcha",
            r"verification required",
            r"bot detected",
            r"suspicious activity",
            r"service unavailable",
            r"temporarily unavailable",
            r"заблокирован",
            r"доступ запрещен",
            r"ошибка \d+",
            r"error \d+",
            r"http \d{3}",
        ]

        # Check page title
        title = soup.find("title")
        if title:
            title_text = title.get_text().strip()
            for pattern in error_patterns:
                if re.search(pattern, title_text, re.IGNORECASE):
                    error_messages.append(f"Title: {title_text}")
                    break

        # Check headings
        for heading in soup.find_all(["h1", "h2", "h3"]):
            heading_text = heading.get_text().strip()
            for pattern in error_patterns:
                if re.search(pattern, heading_text, re.IGNORECASE):
                    error_messages.append(f"Heading: {heading_text}")
                    break

        # Check error containers
        error_containers = soup.find_all(
            ["div", "p", "span"], class_=re.compile(r"error|warning|alert|block", re.I)
        )
        for container in error_containers:
            container_text = container.get_text().strip()
            if (
                len(container_text) > 10 and len(container_text) < 200
            ):  # Reasonable message length
                for pattern in error_patterns:
                    if re.search(pattern, container_text, re.IGNORECASE):
                        error_messages.append(f"Container: {container_text}")
                        break

        # Check meta tags
        meta_tags = soup.find_all("meta", attrs={"name": ["description", "keywords"]})
        for meta in meta_tags:
            content = meta.get("content", "")
            for pattern in error_patterns:
                if re.search(pattern, content, re.IGNORECASE):
                    error_messages.append(f"Meta: {content}")
                    break

        return list(set(error_messages))  # Remove duplicates

    except Exception as e:
        logger.error(f"Error extracting error messages: {e}")
        return []


def detect_rate_limiting(response_headers: Dict[str, str], content: str) -> bool:
    """
    Detect rate limiting from headers and content.

    Args:
        response_headers: HTTP response headers
        content: Response content

    Returns:
        True if rate limiting detected
    """
    try:
        # Check for rate limit headers
        rate_limit_headers = [
            "x-ratelimit-remaining",
            "retry-after",
            "x-rate-limit",
            "x-ratelimit-limit",
            "ratelimit-remaining",
            "rate-limit-remaining",
        ]

        headers_lower = {k.lower(): v for k, v in response_headers.items()}

        if any(header in headers_lower for header in rate_limit_headers):
            return True

        # Check content for rate limit indicators
        if content:
            content_lower = content.lower()
            rate_limit_patterns = [
                "rate limit",
                "too many requests",
                "слишком много запросов",
                "превышен лимит",
                "throttled",
                "slow down",
            ]

            if any(pattern in content_lower for pattern in rate_limit_patterns):
                return True

        return False

    except Exception as e:
        logger.error(f"Error detecting rate limiting: {e}")
        return False


def calculate_proxy_score(proxy_stats: Dict[str, Any]) -> float:
    """
    Calculate proxy performance score based on statistics.

    Args:
        proxy_stats: Dictionary with proxy statistics

    Returns:
        Performance score (0.0 to 1.0)
    """
    try:
        success_rate = proxy_stats.get("success_rate", 0.0)
        avg_response_time = proxy_stats.get("avg_response_time", float("inf"))
        uptime = proxy_stats.get("uptime", 0.0)

        # Normalize response time (consider 5s as baseline, 1s as excellent)
        response_time_score = min(1.0, 5.0 / max(1.0, avg_response_time))

        # Weighted scoring
        score = (success_rate * 0.5) + (uptime * 0.3) + (response_time_score * 0.2)

        return min(1.0, max(0.0, score))

    except Exception as e:
        logger.error(f"Error calculating proxy score: {e}")
        return 0.0
