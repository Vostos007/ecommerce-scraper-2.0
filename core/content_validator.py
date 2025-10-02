"""
Advanced content validation system for detecting blocked responses and silent blocks.
Provides comprehensive response analysis and quality scoring.
"""

import re
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from bs4 import BeautifulSoup
from urllib.parse import urlparse
import hashlib
import difflib
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ValidationResult:
    """Result of content validation."""

    is_valid: bool
    confidence_score: float  # 0.0 to 1.0
    quality_score: float  # 0.0 to 1.0
    block_detected: bool
    block_type: Optional[str] = None
    block_indicators: List[str] = None
    missing_elements: List[str] = None
    content_length: int = 0
    warnings: List[str] = None
    suggestions: List[str] = None

    def __post_init__(self):
        if self.block_indicators is None:
            self.block_indicators = []
        if self.missing_elements is None:
            self.missing_elements = []
        if self.warnings is None:
            self.warnings = []
        if self.suggestions is None:
            self.suggestions = []


class ContentValidator:
    """Advanced content validator for detecting blocks and quality issues."""

    def __init__(self, config: Dict):
        self.config = config
        self.min_content_length = config.get("min_content_length", 1000)
        self.quality_threshold = config.get("quality_threshold", 0.7)

        # Block detection patterns
        self.block_indicators = config.get(
            "block_indicators",
            [
                "access denied",
                "blocked",
                "captcha",
                "rate limit",
                "заблокирован",
                "доступ запрещен",
                "капча",
                "cloudflare",
                "ddos protection",
                "security check",
                "forbidden",
                "unauthorized",
                "too many requests",
                "service unavailable",
                "temporarily unavailable",
            ],
        )

        # CAPTCHA specific patterns
        self.captcha_patterns = [
            r"captcha",
            r"recaptcha",
            r"hcaptcha",
            r"prove you are human",
            r"robot verification",
            r"security verification",
            r"verify.*human",
            r"solve.*challenge",
            r"i.*not.*robot",
        ]

        # Rate limiting patterns
        self.rate_limit_patterns = [
            r"rate limit",
            r"too many requests",
            r"request limit",
            r"throttled",
            r"slow down",
            r"try again later",
            r"превышен лимит",
            r"слишком много запросов",
        ]

        # Bot detection patterns
        self.bot_detection_patterns = [
            r"bot detected",
            r"automated traffic",
            r"suspicious activity",
            r"bot.*block",
            r"anti.*bot",
            r"robot.*detect",
            r"обнаружен бот",
            r"автоматический трафик",
        ]

        # Required elements for content validation
        self.required_elements = config.get("required_elements", ["title", "h1"])

        # Silent block detection config
        silent_config = config.get("silent_block_detection", {})
        self.silent_detection_enabled = silent_config.get("enabled", True)
        self.min_content_ratio = silent_config.get("min_content_ratio", 0.3)
        self.check_element_count = silent_config.get("check_element_count", True)
        self.compare_with_previous = silent_config.get("compare_with_previous", True)

        # Content fingerprinting for comparison
        self.content_fingerprints: Dict[str, str] = {}
        self.element_counts: Dict[str, Dict[str, int]] = {}

        logger.info("ContentValidator initialized with block detection enabled")

    def validate_response(
        self, content: str, url: str, expected_indicators: Optional[List[str]] = None
    ) -> ValidationResult:
        """
        Comprehensive response validation.

        Args:
            content: Response content to validate
            url: URL of the response
            expected_indicators: Optional list of expected content indicators

        Returns:
            ValidationResult with detailed analysis
        """
        result = ValidationResult(
            is_valid=True,
            confidence_score=1.0,
            quality_score=1.0,
            block_detected=False,
            content_length=len(content),
        )

        try:
            # Basic content checks
            if not content or len(content.strip()) < 10:
                result.is_valid = False
                result.confidence_score = 0.0
                result.quality_score = 0.0
                result.warnings.append("Empty or minimal content")
                return result

            # Parse HTML for analysis
            soup = BeautifulSoup(content, "html.parser")

            # Check for blocking patterns
            block_result = self.detect_block_patterns(content)
            if block_result["blocked"]:
                result.block_detected = True
                result.block_type = block_result["block_type"]
                result.block_indicators = block_result["indicators"]
                result.is_valid = False
                result.confidence_score = block_result["confidence"]

            # Check for CAPTCHA
            if self.is_captcha_page(content):
                result.block_detected = True
                result.block_type = "captcha"
                result.is_valid = False
                result.confidence_score = 0.9
                result.warnings.append("CAPTCHA challenge detected")

            # Check content quality
            quality_result = self.analyze_response_quality(
                {"content": content, "url": url}
            )
            result.quality_score = quality_result["quality_score"]

            if result.quality_score < self.quality_threshold:
                result.warnings.append(f"Low quality score: {result.quality_score:.2f}")

            # Check for required elements
            missing = self._check_required_elements(soup)
            if missing:
                result.missing_elements = missing
                result.quality_score *= 0.8  # Reduce quality score
                result.warnings.append(
                    f"Missing required elements: {', '.join(missing)}"
                )

            # Silent block detection
            if self.silent_detection_enabled:
                silent_block = self.detect_silent_block(content, url)
                if silent_block:
                    result.block_detected = True
                    result.block_type = "silent_block"
                    result.is_valid = False
                    result.confidence_score = 0.7
                    result.warnings.append("Silent block detected")

            # Check expected indicators if provided
            if expected_indicators:
                missing_indicators = self._check_expected_indicators(
                    content, expected_indicators
                )
                if missing_indicators:
                    result.quality_score *= 0.6
                    result.warnings.append(
                        f"Missing expected content: {', '.join(missing_indicators)}"
                    )

            # Final validation decision
            if result.block_detected:
                result.is_valid = False
            else:
                hard_threshold = 0.5
                if result.quality_score < hard_threshold:
                    result.is_valid = False
                    result.confidence_score = result.quality_score
                elif result.quality_score < self.quality_threshold:
                    # Borderline quality – keep valid but lower confidence
                    result.confidence_score = result.quality_score

            # Generate suggestions
            result.suggestions = self._generate_suggestions(result)

            logger.debug(
                f"Content validation for {url}: valid={result.is_valid}, quality={result.quality_score:.2f}"
            )

        except Exception as e:
            logger.error(f"Error validating content for {url}: {e}")
            result.is_valid = False
            result.confidence_score = 0.0
            result.warnings.append(f"Validation error: {str(e)}")

        return result

    def detect_block_patterns(self, content: str) -> Dict[str, Any]:
        """
        Detect blocking patterns in content.

        Args:
            content: Content to analyze

        Returns:
            Dictionary with block detection results
        """
        content_lower = content.lower()
        found_indicators = []
        block_type = None
        confidence = 0.0

        # Check basic block indicators
        for indicator in self.block_indicators:
            if indicator.lower() in content_lower:
                found_indicators.append(indicator)

        # Check CAPTCHA patterns
        captcha_matches = []
        for pattern in self.captcha_patterns:
            matches = re.findall(pattern, content_lower, re.IGNORECASE)
            captcha_matches.extend(matches)

        if captcha_matches:
            block_type = "captcha"
            found_indicators.extend(captcha_matches)
            confidence = 0.95

        # Check rate limiting patterns
        rate_limit_matches = []
        for pattern in self.rate_limit_patterns:
            matches = re.findall(pattern, content_lower, re.IGNORECASE)
            rate_limit_matches.extend(matches)

        if rate_limit_matches:
            block_type = "rate_limit"
            found_indicators.extend(rate_limit_matches)
            confidence = max(confidence, 0.9)

        # Check bot detection patterns
        bot_matches = []
        for pattern in self.bot_detection_patterns:
            matches = re.findall(pattern, content_lower, re.IGNORECASE)
            bot_matches.extend(matches)

        if bot_matches:
            block_type = "bot_detection"
            found_indicators.extend(bot_matches)
            confidence = max(confidence, 0.85)

        # Check for common blocking HTTP status indicators in content
        status_indicators = [
            "403 forbidden",
            "429 too many requests",
            "503 service unavailable",
            "error 403",
            "error 429",
            "error 503",
            "status: 403",
            "status: 429",
        ]

        for indicator in status_indicators:
            if indicator in content_lower:
                found_indicators.append(indicator)
                block_type = "http_error"
                confidence = max(confidence, 0.8)

        # Calculate overall confidence
        if found_indicators and confidence == 0.0:
            confidence = min(0.8, len(found_indicators) * 0.2)

        return {
            "blocked": bool(found_indicators),
            "block_type": block_type,
            "indicators": found_indicators,
            "confidence": confidence,
        }

    def is_content_valid(
        self,
        content: str,
        min_length: Optional[int] = None,
        required_elements: Optional[List[str]] = None,
    ) -> bool:
        """
        Check if content meets basic validity requirements.

        Args:
            content: Content to validate
            min_length: Minimum content length (uses config default if None)
            required_elements: Required HTML elements (uses config default if None)

        Returns:
            True if content is valid
        """
        min_length = min_length or self.min_content_length
        required_elements = required_elements or self.required_elements

        # Basic length check
        if len(content.strip()) < min_length:
            return False

        # Check for blocking patterns
        block_result = self.detect_block_patterns(content)
        if block_result["blocked"]:
            return False

        # Check required elements
        try:
            soup = BeautifulSoup(content, "html.parser")
            missing = self._check_required_elements(soup, required_elements)
            if missing:
                return False
        except Exception:
            # If parsing fails, consider content potentially invalid
            return False

        return True

    def analyze_response_quality(self, response: Dict[str, Any]) -> Dict[str, Any]:
        """
        Analyze response quality and generate score.

        Args:
            response: Response data with 'content' and 'url' keys

        Returns:
            Dictionary with quality analysis
        """
        content = response.get("content", "")
        response.get("url", "")

        quality_metrics = {
            "content_length": len(content),
            "word_count": len(content.split()),
            "html_structure_score": 0.0,
            "text_content_ratio": 0.0,
            "element_diversity": 0.0,
            "has_navigation": False,
            "has_main_content": False,
            "error_indicators": [],
        }

        try:
            soup = BeautifulSoup(content, "html.parser")

            # Calculate HTML structure score
            quality_metrics["html_structure_score"] = (
                self._calculate_html_structure_score(soup)
            )

            # Calculate text to HTML ratio
            text_content = soup.get_text(strip=True)
            if content:
                quality_metrics["text_content_ratio"] = len(text_content) / len(content)

            # Element diversity (variety of HTML tags)
            all_tags = [tag.name for tag in soup.find_all()]
            unique_tags = set(all_tags)
            quality_metrics["element_diversity"] = len(unique_tags) / max(
                len(all_tags), 1
            )

            # Check for navigation elements
            nav_elements = soup.find_all(["nav", "header", "menu"])
            nav_classes = soup.find_all(class_=re.compile(r"nav|menu|header"))
            quality_metrics["has_navigation"] = bool(nav_elements or nav_classes)

            # Check for main content areas
            main_elements = soup.find_all(["main", "article", "section"])
            main_classes = soup.find_all(class_=re.compile(r"main|content|article"))
            main_ids = soup.find_all(id=re.compile(r"main|content|article"))
            quality_metrics["has_main_content"] = bool(
                main_elements or main_classes or main_ids
            )

            # Check for error indicators
            error_indicators = [
                "error",
                "exception",
                "failed",
                "not found",
                "unavailable",
                "ошибка",
                "исключение",
                "не найдено",
                "недоступно",
            ]

            for indicator in error_indicators:
                if indicator in content.lower():
                    quality_metrics["error_indicators"].append(indicator)

        except Exception as e:
            logger.warning(f"Error analyzing HTML structure: {e}")

        # Calculate overall quality score
        quality_score = self._calculate_quality_score(quality_metrics)

        return {
            "quality_score": quality_score,
            "metrics": quality_metrics,
            "content_length": quality_metrics["content_length"],
            "word_count": quality_metrics["word_count"],
        }

    def detect_silent_block(
        self,
        content: str,
        url: Optional[str] = None,
        previous_content: Optional[str] = None,
    ) -> bool:
        """
        Detect silent blocking (empty or minimal content).

        Args:
            content: Current content
            url: URL for fingerprinting
            previous_content: Previous content for comparison

        Returns:
            True if silent block is detected
        """
        if not self.silent_detection_enabled:
            return False

        try:
            signals = 0
            stripped = content.strip()
            content_length = len(stripped)
            length_threshold = max(200, int(self.min_content_length * self.min_content_ratio))

            if content_length < length_threshold:
                signals += 1

            soup = BeautifulSoup(content, "html.parser")

            if self.check_element_count:
                element_count = len(soup.find_all())

                if url:
                    domain = urlparse(url).netloc
                    if domain in self.element_counts:
                        expected_count = self.element_counts[domain].get(
                            "avg_count", 50
                        )
                        if element_count < expected_count * self.min_content_ratio:
                            signals += 1
                    else:
                        self.element_counts[domain] = {"avg_count": element_count}

            if self.compare_with_previous and previous_content:
                similarity = self._calculate_content_similarity(
                    content, previous_content
                )
                if similarity > 0.95:
                    signals += 1

            if not soup.find("body"):
                return True

            error_structures = [
                soup.find("div", class_=re.compile(r"error|404|not.?found")),
                soup.find(
                    "h1", string=re.compile(r"error|404|not found|blocked", re.I)
                ),
                soup.find(
                    "title", string=re.compile(r"error|404|not found|blocked", re.I)
                ),
            ]

            if any(error_structures):
                return True

            text_content = soup.get_text(strip=True)
            words = text_content.split()

            if len(words) < 20:
                signals += 1

            if words:
                word_frequency = {}
                for word in words:
                    word_lower = word.lower()
                    word_frequency[word_lower] = word_frequency.get(word_lower, 0) + 1

                most_common_freq = max(word_frequency.values())
                if most_common_freq / len(words) > 0.35:
                    signals += 1

            text_ratio = len(words) / max(len(content.split()), 1)
            if text_ratio < 0.2:
                signals += 1

            placeholder_patterns = [
                r"page not found",
                r"temporarily unavailable",
                r"maintenance",
                r"coming soon",
                r"under construction",
                r"please try again",
                r"service unavailable",
            ]

            for pattern in placeholder_patterns:
                if re.search(pattern, text_content, re.IGNORECASE):
                    signals += 1
                    break

            return signals >= 2

        except Exception as e:
            logger.warning(f"Error in silent block detection: {e}")

        return False

    def is_captcha_page(self, content: str) -> bool:
        """
        Detect CAPTCHA challenges.

        Args:
            content: Content to analyze

        Returns:
            True if CAPTCHA is detected
        """
        content_lower = content.lower()

        # Check for CAPTCHA patterns
        for pattern in self.captcha_patterns:
            if re.search(pattern, content_lower):
                return True

        # Check for CAPTCHA service indicators
        captcha_services = [
            "recaptcha",
            "hcaptcha",
            "funcaptcha",
            "geetest",
            "cloudflare",
            "turnstile",
        ]

        for service in captcha_services:
            if service in content_lower:
                return True

        # Check for CAPTCHA-related HTML elements
        try:
            soup = BeautifulSoup(content, "html.parser")

            # reCAPTCHA elements
            if soup.find("div", class_=re.compile(r"recaptcha|g-recaptcha")):
                return True

            # hCaptcha elements
            if soup.find("div", class_=re.compile(r"hcaptcha|h-captcha")):
                return True

            # Generic CAPTCHA form elements
            captcha_inputs = soup.find_all(
                "input", {"name": re.compile(r"captcha", re.I)}
            )
            if captcha_inputs:
                return True

            # Check for CAPTCHA images
            captcha_images = soup.find_all("img", {"src": re.compile(r"captcha", re.I)})
            if captcha_images:
                return True

        except Exception:
            pass

        return False

    def calculate_content_score(self, content: str) -> float:
        """
        Calculate content quality score.

        Args:
            content: Content to score

        Returns:
            Quality score between 0.0 and 1.0
        """
        if not content:
            return 0.0

        try:
            soup = BeautifulSoup(content, "html.parser")
            text_content = soup.get_text(strip=True)

            # Basic metrics
            content_length = len(content)
            text_length = len(text_content)
            word_count = len(text_content.split())

            effective_min_length = max(300, int(self.min_content_length * 0.3))
            word_baseline = max(80, int(effective_min_length / 4))

            # Score components
            scores = []

            # Length score (sigmoid function)
            length_score = min(1.0, content_length / effective_min_length)
            scores.append(length_score * 0.3)

            # Word count score
            word_score = min(1.0, word_count / word_baseline)
            scores.append(word_score * 0.2)

            # Structure score
            structure_score = self._calculate_html_structure_score(soup)
            scores.append(structure_score * 0.3)

            # Text density score
            if content_length > 0:
                text_density = text_length / content_length
                density_score = min(1.0, max(0.0, text_density * 1.5))
                scores.append(density_score * 0.2)
            else:
                scores.append(0.0)

            return sum(scores)

        except Exception:
            return 0.0

    def _check_required_elements(
        self, soup: BeautifulSoup, required_elements: Optional[List[str]] = None
    ) -> List[str]:
        """Check for required HTML elements."""
        required_elements = required_elements or self.required_elements
        missing = []

        for element in required_elements:
            if not soup.find(element):
                missing.append(element)

        return missing

    def _check_expected_indicators(
        self, content: str, expected_indicators: List[str]
    ) -> List[str]:
        """Check for expected content indicators."""
        missing = []
        content_lower = content.lower()

        for indicator in expected_indicators:
            if indicator.lower() not in content_lower:
                missing.append(indicator)

        return missing

    def _calculate_html_structure_score(self, soup: BeautifulSoup) -> float:
        """Calculate HTML structure quality score."""
        score_components = []

        # Basic structure elements
        basic_elements = ["html", "head", "body", "title"]
        basic_score = sum(1 for elem in basic_elements if soup.find(elem)) / len(
            basic_elements
        )
        score_components.append(basic_score * 0.3)

        # Semantic elements
        semantic_elements = [
            "header",
            "nav",
            "main",
            "article",
            "section",
            "aside",
            "footer",
        ]
        semantic_count = sum(1 for elem in semantic_elements if soup.find(elem))
        semantic_score = min(
            1.0, semantic_count / 3
        )  # 3 semantic elements as good baseline
        score_components.append(semantic_score * 0.4)

        # Content elements
        content_elements = ["h1", "h2", "h3", "p", "div", "span"]
        content_count = sum(len(soup.find_all(elem)) for elem in content_elements)
        content_score = min(1.0, content_count / 10)  # 10 content elements as baseline
        score_components.append(content_score * 0.3)

        return sum(score_components)

    def _calculate_quality_score(self, metrics: Dict[str, Any]) -> float:
        """Calculate overall quality score from metrics."""
        score_components = []

        effective_min_length = max(300, int(self.min_content_length * 0.3))
        word_baseline = max(80, int(effective_min_length / 4))

        # Content length score
        length_score = min(1.0, metrics["content_length"] / effective_min_length)
        score_components.append(length_score * 0.25)

        # Word count score
        word_score = min(1.0, metrics["word_count"] / word_baseline)
        score_components.append(word_score * 0.20)

        # Structure score
        score_components.append(metrics["html_structure_score"] * 0.25)

        # Text content ratio
        score_components.append(min(1.0, metrics["text_content_ratio"] * 2) * 0.15)

        # Element diversity
        score_components.append(min(1.0, metrics["element_diversity"] * 2) * 0.10)

        # Navigation bonus
        if metrics["has_navigation"]:
            score_components.append(0.025)

        # Main content bonus
        if metrics["has_main_content"]:
            score_components.append(0.025)

        # Error penalty
        error_penalty = min(0.2, len(metrics["error_indicators"]) * 0.05)

        final_score = max(0.0, sum(score_components) - error_penalty)
        return min(1.0, final_score)

    def _calculate_content_similarity(self, content1: str, content2: str) -> float:
        """Calculate similarity between two content strings."""
        try:
            # Use difflib for sequence similarity
            similarity = difflib.SequenceMatcher(None, content1, content2).ratio()
            return similarity
        except Exception:
            return 0.0

    def _generate_content_fingerprint(self, content: str) -> str:
        """Generate fingerprint for content comparison."""
        try:
            soup = BeautifulSoup(content, "html.parser")
            text_content = soup.get_text(strip=True)
            # Use first 500 characters for fingerprinting
            fingerprint_text = text_content[:500]
            return hashlib.md5(fingerprint_text.encode()).hexdigest()
        except Exception:
            return hashlib.md5(content[:500].encode()).hexdigest()

    def _generate_suggestions(self, result: ValidationResult) -> List[str]:
        """Generate improvement suggestions based on validation result."""
        suggestions = []

        if result.block_detected:
            if result.block_type == "captcha":
                suggestions.append(
                    "Consider using a different proxy or solving CAPTCHA challenge"
                )
            elif result.block_type == "rate_limit":
                suggestions.append(
                    "Implement rate limiting delays or use different proxy"
                )
            elif result.block_type == "bot_detection":
                suggestions.append("Update user agent or use residential proxies")
            elif result.block_type == "silent_block":
                suggestions.append("Verify proxy health and consider rotation")

        if result.quality_score < self.quality_threshold:
            suggestions.append(
                "Content quality is low, verify target page is loading correctly"
            )

        if result.missing_elements:
            suggestions.append(
                f"Page may not be fully loaded, missing: {', '.join(result.missing_elements)}"
            )

        if result.content_length < self.min_content_length:
            suggestions.append("Content length is below minimum threshold")

        return suggestions

    def update_content_baseline(self, url: str, content: str) -> None:
        """Update content baseline for future comparison."""
        try:
            domain = urlparse(url).netloc
            soup = BeautifulSoup(content, "html.parser")
            element_count = len(soup.find_all())

            if domain not in self.element_counts:
                self.element_counts[domain] = {"count_history": [], "avg_count": 0}

            self.element_counts[domain]["count_history"].append(element_count)
            # Keep only last 10 counts
            self.element_counts[domain]["count_history"] = self.element_counts[domain][
                "count_history"
            ][-10:]

            # Calculate average
            history = self.element_counts[domain]["count_history"]
            self.element_counts[domain]["avg_count"] = sum(history) / len(history)

        except Exception as e:
            logger.warning(f"Error updating content baseline for {url}: {e}")
