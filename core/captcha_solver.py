"""
Comprehensive 2captcha service integration for automatic CAPTCHA solving.

This module provides production-grade CAPTCHA solving capabilities with support for:
- reCAPTCHA v2 and v3
- hCaptcha
- Image-based CAPTCHAs
- Automatic CAPTCHA detection
- Cost tracking and balance monitoring
- Performance metrics and retry logic
"""

import asyncio
import os
import aiohttp
import base64
import re
import time
from typing import Dict, Any, Optional
from urllib.parse import urljoin, urlparse
import logging

logger = logging.getLogger(__name__)


class TwoCaptchaManager:
    """
    Comprehensive 2captcha service integration for automatic CAPTCHA solving.

    Features:
    - Support for multiple CAPTCHA types
    - Automatic detection and solving
    - Cost tracking and balance monitoring
    - Performance metrics and optimization
    - Proxy integration for solving
    """

    def __init__(self, config: Dict[str, Any]):
        """Initialize TwoCaptchaManager with configuration."""
        self.api_key_env = config.get("api_key_env", "CAPTCHA_API_KEY")
        self.api_key = config.get("api_key") or os.getenv(self.api_key_env, "")
        self.api_url = config.get("api_url", "http://2captcha.com")
        self.default_timeout = config.get("timeout_seconds", config.get("timeout", 120))
        self.polling_interval = config.get(
            "polling_interval_seconds", config.get("polling_interval", 5)
        )
        self.max_retries = config.get("max_retries", 3)
        self.proxy_format = config.get("proxy_format", "http")
        self.enabled = config.get("enabled", True) and bool(self.api_key)

        if config.get("enabled", True) and not self.api_key:
            logger.warning(
                "Captcha solving enabled but API key not configured (env %s)",
                self.api_key_env,
            )

        # Cost tracking configuration
        self.cost_tracking = config.get("cost_tracking", {})
        self.daily_limit_usd = self.cost_tracking.get("daily_limit_usd", 10.0)
        self.min_balance_usd = self.cost_tracking.get("min_balance_usd", 5.0)
        self.alert_on_low_balance = self.cost_tracking.get("alert_on_low_balance", True)

        # Performance settings
        self.performance_settings = config.get("performance_settings", {})
        self.prefer_fast_workers = self.performance_settings.get(
            "prefer_fast_workers", True
        )
        self.max_solve_time = self.performance_settings.get(
            "max_solve_time_seconds", 60
        )
        self.retry_on_timeout = self.performance_settings.get("retry_on_timeout", True)

        # Statistics tracking
        self.solve_stats = {
            "total_attempts": 0,
            "successful_solves": 0,
            "failed_solves": 0,
            "timeout_errors": 0,
            "balance_errors": 0,
            "avg_solve_time": 0.0,
            "total_cost_usd": 0.0,
            "daily_cost_usd": 0.0,
            "last_reset_date": time.strftime("%Y-%m-%d"),
        }

        # CAPTCHA type detection patterns
        self.captcha_patterns = {
            "recaptcha_v2": [
                r"www\.google\.com/recaptcha/api\.js",
                r"www\.google\.com/recaptcha/api/challenge",
                r'data-sitekey="([^"]+)"',
                r"grecaptcha\.render",
                r'<div[^>]*class="g-recaptcha"',
            ],
            "recaptcha_v3": [
                r'www\.google\.com/recaptcha/api\.js\?render=([^&\s"]+)',
                r"grecaptcha\.execute",
                r'data-action="([^"]+)"',
            ],
            "hcaptcha": [
                r"hcaptcha\.com/1/api\.js",
                r'data-sitekey="([^"]+)"',
                r'<div[^>]*class="h-captcha"',
                r"hcaptcha\.render",
            ],
            "image_captcha": [
                r"<img[^>]*captcha[^>]*>",
                r"captcha\.jpg|captcha\.png|captcha\.gif",
                r"verification.*image",
                r"security.*code",
            ],
        }

        # Cost per CAPTCHA type (approximate USD)
        self.captcha_costs = {
            "recaptcha_v2": 0.002,
            "recaptcha_v3": 0.002,
            "hcaptcha": 0.002,
            "image_captcha": 0.001,
        }

    async def solve_recaptcha_v2(
        self,
        site_key: str,
        page_url: str,
        proxy: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> Optional[str]:
        """
        Solve reCAPTCHA v2 with comprehensive error handling and retry logic.

        Args:
            site_key: The site key for the reCAPTCHA
            page_url: The URL where the CAPTCHA is located
            proxy: Optional proxy to use for solving
            user_agent: Optional user agent for solving

        Returns:
            CAPTCHA solution token or None if failed
        """
        if not self.enabled:
            logger.warning("2captcha is disabled or no API key provided")
            return None

        logger.info(f"Solving reCAPTCHA v2 for site_key: {site_key[:20]}...")

        start_time = time.time()
        self.solve_stats["total_attempts"] += 1

        try:
            # Check balance before attempting solve
            if not await self._check_sufficient_balance("recaptcha_v2"):
                return None

            # Submit CAPTCHA for solving
            submit_data = {
                "key": self.api_key,
                "method": "userrecaptcha",
                "googlekey": site_key,
                "pageurl": page_url,
                "json": 1,
            }

            # Add proxy if provided
            if proxy:
                proxy_formatted = self._format_proxy_for_2captcha(proxy)
                if proxy_formatted:
                    submit_data.update(proxy_formatted)

            # Add user agent if provided
            if user_agent:
                submit_data["userAgent"] = user_agent

            # Prefer fast workers if enabled
            if self.prefer_fast_workers:
                submit_data["fast"] = 1

            # Submit the CAPTCHA
            captcha_id = await self._submit_captcha(submit_data)
            if not captcha_id:
                self.solve_stats["failed_solves"] += 1
                return None

            # Poll for result
            solution = await self._poll_captcha_result(captcha_id, self.max_solve_time)

            solve_time = time.time() - start_time

            if solution:
                self.solve_stats["successful_solves"] += 1
                self._update_solve_time_stats(solve_time)
                self._track_cost("recaptcha_v2")
                logger.info(f"reCAPTCHA v2 solved successfully in {solve_time:.2f}s")
                return solution
            else:
                self.solve_stats["failed_solves"] += 1
                logger.warning(f"reCAPTCHA v2 solving failed after {solve_time:.2f}s")
                return None

        except Exception as e:
            self.solve_stats["failed_solves"] += 1
            logger.error(f"Error solving reCAPTCHA v2: {e}")
            return None

    async def solve_recaptcha_v3(
        self,
        site_key: str,
        page_url: str,
        action: str = "submit",
        proxy: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> Optional[str]:
        """
        Solve reCAPTCHA v3 with action parameter support.

        Args:
            site_key: The site key for the reCAPTCHA
            page_url: The URL where the CAPTCHA is located
            action: The action parameter for reCAPTCHA v3
            proxy: Optional proxy to use for solving
            user_agent: Optional user agent for solving

        Returns:
            CAPTCHA solution token or None if failed
        """
        if not self.enabled:
            logger.warning("2captcha is disabled or no API key provided")
            return None

        logger.info(
            f"Solving reCAPTCHA v3 for site_key: {site_key[:20]}... action: {action}"
        )

        start_time = time.time()
        self.solve_stats["total_attempts"] += 1

        try:
            # Check balance before attempting solve
            if not await self._check_sufficient_balance("recaptcha_v3"):
                return None

            # Submit CAPTCHA for solving
            submit_data = {
                "key": self.api_key,
                "method": "userrecaptcha",
                "version": "v3",
                "googlekey": site_key,
                "pageurl": page_url,
                "action": action,
                "json": 1,
            }

            # Add proxy if provided
            if proxy:
                proxy_formatted = self._format_proxy_for_2captcha(proxy)
                if proxy_formatted:
                    submit_data.update(proxy_formatted)

            # Add user agent if provided
            if user_agent:
                submit_data["userAgent"] = user_agent

            # Submit the CAPTCHA
            captcha_id = await self._submit_captcha(submit_data)
            if not captcha_id:
                self.solve_stats["failed_solves"] += 1
                return None

            # Poll for result
            solution = await self._poll_captcha_result(captcha_id, self.max_solve_time)

            solve_time = time.time() - start_time

            if solution:
                self.solve_stats["successful_solves"] += 1
                self._update_solve_time_stats(solve_time)
                self._track_cost("recaptcha_v3")
                logger.info(f"reCAPTCHA v3 solved successfully in {solve_time:.2f}s")
                return solution
            else:
                self.solve_stats["failed_solves"] += 1
                logger.warning(f"reCAPTCHA v3 solving failed after {solve_time:.2f}s")
                return None

        except Exception as e:
            self.solve_stats["failed_solves"] += 1
            logger.error(f"Error solving reCAPTCHA v3: {e}")
            return None

    async def solve_hcaptcha(
        self,
        site_key: str,
        page_url: str,
        proxy: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> Optional[str]:
        """
        Solve hCaptcha with comprehensive error handling.

        Args:
            site_key: The site key for the hCaptcha
            page_url: The URL where the CAPTCHA is located
            proxy: Optional proxy to use for solving
            user_agent: Optional user agent for solving

        Returns:
            CAPTCHA solution token or None if failed
        """
        if not self.enabled:
            logger.warning("2captcha is disabled or no API key provided")
            return None

        logger.info(f"Solving hCaptcha for site_key: {site_key[:20]}...")

        start_time = time.time()
        self.solve_stats["total_attempts"] += 1

        try:
            # Check balance before attempting solve
            if not await self._check_sufficient_balance("hcaptcha"):
                return None

            # Submit CAPTCHA for solving
            submit_data = {
                "key": self.api_key,
                "method": "hcaptcha",
                "sitekey": site_key,
                "pageurl": page_url,
                "json": 1,
            }

            # Add proxy if provided
            if proxy:
                proxy_formatted = self._format_proxy_for_2captcha(proxy)
                if proxy_formatted:
                    submit_data.update(proxy_formatted)

            # Add user agent if provided
            if user_agent:
                submit_data["userAgent"] = user_agent

            # Submit the CAPTCHA
            captcha_id = await self._submit_captcha(submit_data)
            if not captcha_id:
                self.solve_stats["failed_solves"] += 1
                return None

            # Poll for result
            solution = await self._poll_captcha_result(captcha_id, self.max_solve_time)

            solve_time = time.time() - start_time

            if solution:
                self.solve_stats["successful_solves"] += 1
                self._update_solve_time_stats(solve_time)
                self._track_cost("hcaptcha")
                logger.info(f"hCaptcha solved successfully in {solve_time:.2f}s")
                return solution
            else:
                self.solve_stats["failed_solves"] += 1
                logger.warning(f"hCaptcha solving failed after {solve_time:.2f}s")
                return None

        except Exception as e:
            self.solve_stats["failed_solves"] += 1
            logger.error(f"Error solving hCaptcha: {e}")
            return None

    async def solve_image_captcha(self, image_data: bytes) -> Optional[str]:
        """
        Solve image-based CAPTCHA from raw image data.

        Args:
            image_data: Raw image bytes (PNG, JPG, GIF)

        Returns:
            CAPTCHA solution text or None if failed
        """
        if not self.enabled:
            logger.warning("2captcha is disabled or no API key provided")
            return None

        logger.info("Solving image CAPTCHA...")

        start_time = time.time()
        self.solve_stats["total_attempts"] += 1

        try:
            # Check balance before attempting solve
            if not await self._check_sufficient_balance("image_captcha"):
                return None

            # Encode image to base64
            image_base64 = base64.b64encode(image_data).decode("utf-8")

            # Submit CAPTCHA for solving
            submit_data = {
                "key": self.api_key,
                "method": "base64",
                "body": image_base64,
                "json": 1,
            }

            # Submit the CAPTCHA
            captcha_id = await self._submit_captcha(submit_data)
            if not captcha_id:
                self.solve_stats["failed_solves"] += 1
                return None

            # Poll for result
            solution = await self._poll_captcha_result(captcha_id, self.max_solve_time)

            solve_time = time.time() - start_time

            if solution:
                self.solve_stats["successful_solves"] += 1
                self._update_solve_time_stats(solve_time)
                self._track_cost("image_captcha")
                logger.info(
                    f"Image CAPTCHA solved successfully in {solve_time:.2f}s: {solution}"
                )
                return solution
            else:
                self.solve_stats["failed_solves"] += 1
                logger.warning(f"Image CAPTCHA solving failed after {solve_time:.2f}s")
                return None

        except Exception as e:
            self.solve_stats["failed_solves"] += 1
            logger.error(f"Error solving image CAPTCHA: {e}")
            return None

    async def detect_captcha_type(self, html: str, url: str) -> Dict[str, Any]:
        """
        Automatically detect CAPTCHA type and extract necessary parameters from HTML.

        Args:
            html: HTML content to analyze
            url: URL of the page (for context)

        Returns:
            Dictionary with CAPTCHA type and parameters
        """
        detection_result = {
            "detected": False,
            "type": None,
            "site_key": None,
            "action": None,
            "image_url": None,
            "confidence": 0.0,
        }

        try:
            # Check for reCAPTCHA v3 first (more specific patterns)
            for pattern in self.captcha_patterns["recaptcha_v3"]:
                matches = re.findall(pattern, html, re.IGNORECASE)
                if matches:
                    detection_result.update(
                        {"detected": True, "type": "recaptcha_v3", "confidence": 0.9}
                    )

                    # Extract site key from render parameter
                    render_match = re.search(r'render=([^&\s"]+)', html, re.IGNORECASE)
                    if render_match:
                        detection_result["site_key"] = render_match.group(1)

                    # Extract action if present
                    action_match = re.search(
                        r'data-action="([^"]+)"', html, re.IGNORECASE
                    )
                    if action_match:
                        detection_result["action"] = action_match.group(1)

                    logger.info(
                        f"Detected reCAPTCHA v3 with site_key: {detection_result['site_key']}"
                    )
                    return detection_result

            # Check for reCAPTCHA v2
            for pattern in self.captcha_patterns["recaptcha_v2"]:
                matches = re.findall(pattern, html, re.IGNORECASE)
                if matches:
                    detection_result.update(
                        {"detected": True, "type": "recaptcha_v2", "confidence": 0.8}
                    )

                    # Extract site key
                    sitekey_match = re.search(
                        r'data-sitekey="([^"]+)"', html, re.IGNORECASE
                    )
                    if sitekey_match:
                        detection_result["site_key"] = sitekey_match.group(1)

                    logger.info(
                        f"Detected reCAPTCHA v2 with site_key: {detection_result['site_key']}"
                    )
                    return detection_result

            # Check for hCaptcha
            for pattern in self.captcha_patterns["hcaptcha"]:
                matches = re.findall(pattern, html, re.IGNORECASE)
                if matches:
                    detection_result.update(
                        {"detected": True, "type": "hcaptcha", "confidence": 0.8}
                    )

                    # Extract site key
                    sitekey_match = re.search(
                        r'data-sitekey="([^"]+)"', html, re.IGNORECASE
                    )
                    if sitekey_match:
                        detection_result["site_key"] = sitekey_match.group(1)

                    logger.info(
                        f"Detected hCaptcha with site_key: {detection_result['site_key']}"
                    )
                    return detection_result

            # Check for image CAPTCHA
            for pattern in self.captcha_patterns["image_captcha"]:
                matches = re.findall(pattern, html, re.IGNORECASE)
                if matches:
                    detection_result.update(
                        {"detected": True, "type": "image_captcha", "confidence": 0.6}
                    )

                    # Try to extract image URL
                    img_match = re.search(
                        r'<img[^>]*src="([^"]*captcha[^"]*)"', html, re.IGNORECASE
                    )
                    if img_match:
                        detection_result["image_url"] = urljoin(url, img_match.group(1))

                    logger.info(
                        f"Detected image CAPTCHA with URL: {detection_result['image_url']}"
                    )
                    return detection_result

            logger.debug("No CAPTCHA detected in HTML content")
            return detection_result

        except Exception as e:
            logger.error(f"Error detecting CAPTCHA type: {e}")
            return detection_result

    async def get_balance(self) -> Optional[float]:
        """
        Get current 2captcha account balance.

        Returns:
            Account balance in USD or None if failed
        """
        if not self.enabled:
            return None

        try:
            async with aiohttp.ClientSession() as session:
                url = f"{self.api_url}/res.php"
                params = {"key": self.api_key, "action": "getbalance", "json": 1}

                async with session.get(url, params=params, timeout=10) as response:
                    data = await response.json()

                    if data.get("status") == 1:
                        balance = float(data.get("request", 0))
                        logger.info(f"2captcha balance: ${balance:.4f} USD")
                        return balance
                    else:
                        logger.error(
                            f"Failed to get balance: {data.get('error_text', 'Unknown error')}"
                        )
                        return None

        except Exception as e:
            logger.error(f"Error getting 2captcha balance: {e}")
            return None

    def get_statistics(self) -> Dict[str, Any]:
        """
        Get comprehensive CAPTCHA solving statistics.

        Returns:
            Dictionary with detailed statistics
        """
        success_rate = 0.0
        if self.solve_stats["total_attempts"] > 0:
            success_rate = (
                self.solve_stats["successful_solves"]
                / self.solve_stats["total_attempts"]
            ) * 100

        return {
            "enabled": self.enabled,
            "total_attempts": self.solve_stats["total_attempts"],
            "successful_solves": self.solve_stats["successful_solves"],
            "failed_solves": self.solve_stats["failed_solves"],
            "success_rate_percent": round(success_rate, 2),
            "average_solve_time_seconds": round(self.solve_stats["avg_solve_time"], 2),
            "total_cost_usd": round(self.solve_stats["total_cost_usd"], 4),
            "daily_cost_usd": round(self.solve_stats["daily_cost_usd"], 4),
            "daily_limit_usd": self.daily_limit_usd,
            "last_reset_date": self.solve_stats["last_reset_date"],
        }

    # Private helper methods

    async def _submit_captcha(self, submit_data: Dict[str, Any]) -> Optional[str]:
        """Submit CAPTCHA to 2captcha service."""
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{self.api_url}/in.php"

                async with session.post(url, data=submit_data, timeout=30) as response:
                    data = await response.json()

                    if data.get("status") == 1:
                        captcha_id = data.get("request")
                        logger.debug(
                            f"CAPTCHA submitted successfully, ID: {captcha_id}"
                        )
                        return captcha_id
                    else:
                        error_text = data.get("error_text", "Unknown error")
                        logger.error(f"Failed to submit CAPTCHA: {error_text}")
                        return None

        except Exception as e:
            logger.error(f"Error submitting CAPTCHA: {e}")
            return None

    async def _poll_captcha_result(
        self, captcha_id: str, max_time: int
    ) -> Optional[str]:
        """Poll for CAPTCHA solution result."""
        start_time = time.time()

        try:
            async with aiohttp.ClientSession() as session:
                url = f"{self.api_url}/res.php"

                while time.time() - start_time < max_time:
                    params = {
                        "key": self.api_key,
                        "action": "get",
                        "id": captcha_id,
                        "json": 1,
                    }

                    async with session.get(url, params=params, timeout=10) as response:
                        data = await response.json()

                        if data.get("status") == 1:
                            return data.get("request")
                        elif data.get("request") == "CAPCHA_NOT_READY":
                            await asyncio.sleep(self.polling_interval)
                            continue
                        else:
                            logger.error(
                                f"CAPTCHA solving failed: {data.get('request') or data.get('error_text')}"
                            )
                            return None

                logger.warning(f"CAPTCHA solving timed out after {max_time}s")
                self.solve_stats["timeout_errors"] += 1
                return None

        except Exception as e:
            logger.error(f"Error polling CAPTCHA result: {e}")
            return None

    def _format_proxy_for_2captcha(self, proxy: str) -> Optional[Dict[str, str]]:
        """Format proxy URL for 2captcha API requirements."""
        try:
            if "://" in proxy:
                # Parse full proxy URL
                parsed = urlparse(proxy)
                proxy_type = parsed.scheme
                proxy_host = parsed.hostname
                proxy_port = parsed.port
                proxy_user = parsed.username
                proxy_pass = parsed.password
            else:
                # Assume host:port format
                parts = proxy.split(":")
                if len(parts) >= 2:
                    proxy_type = self.proxy_format
                    proxy_host = parts[0]
                    proxy_port = int(parts[1])
                    proxy_user = parts[2] if len(parts) > 2 else None
                    proxy_pass = parts[3] if len(parts) > 3 else None
                else:
                    return None

            proxy_data = {
                "proxy": f"{proxy_host}:{proxy_port}",
                "proxytype": proxy_type.upper(),
            }

            if proxy_user and proxy_pass:
                proxy_data["proxy"] = (
                    f"{proxy_user}:{proxy_pass}@{proxy_host}:{proxy_port}"
                )

            return proxy_data

        except Exception as e:
            logger.error(f"Error formatting proxy for 2captcha: {e}")
            return None

    async def _check_sufficient_balance(self, captcha_type: str) -> bool:
        """Check if there's sufficient balance for solving."""
        try:
            current_balance = await self.get_balance()
            if current_balance is None:
                self.solve_stats["balance_errors"] += 1
                return False

            required_cost = self.captcha_costs.get(captcha_type, 0.002)

            if current_balance < required_cost:
                logger.error(
                    f"Insufficient balance: ${current_balance:.4f} < ${required_cost:.4f}"
                )
                self.solve_stats["balance_errors"] += 1
                return False

            if current_balance < self.min_balance_usd and self.alert_on_low_balance:
                logger.warning(
                    f"Low balance alert: ${current_balance:.4f} < ${self.min_balance_usd:.4f}"
                )

            return True

        except Exception as e:
            logger.error(f"Error checking balance: {e}")
            return False

    def _update_solve_time_stats(self, solve_time: float) -> None:
        """Update average solve time statistics."""
        prev_success = max(self.solve_stats["successful_solves"] - 1, 0)
        prev_avg = self.solve_stats["avg_solve_time"]
        total_time = prev_avg * prev_success + solve_time
        new_count = prev_success + 1
        self.solve_stats["avg_solve_time"] = (
            total_time / new_count if new_count else 0.0
        )

    def _track_cost(self, captcha_type: str) -> None:
        """Track cost for solved CAPTCHA."""
        cost = self.captcha_costs.get(captcha_type, 0.002)
        self.solve_stats["total_cost_usd"] += cost

        # Reset daily cost if new day
        current_date = time.strftime("%Y-%m-%d")
        if current_date != self.solve_stats["last_reset_date"]:
            self.solve_stats["daily_cost_usd"] = 0.0
            self.solve_stats["last_reset_date"] = current_date

        self.solve_stats["daily_cost_usd"] += cost

        # Check daily limit
        if self.solve_stats["daily_cost_usd"] > self.daily_limit_usd:
            logger.warning(
                f"Daily cost limit exceeded: ${self.solve_stats['daily_cost_usd']:.4f} > ${self.daily_limit_usd:.4f}"
            )
