"""
Advanced proxy health checking and management system.
Provides comprehensive proxy validation, performance monitoring, and automatic replacement.
"""

import asyncio
import aiohttp
import time
import statistics
from collections import defaultdict
from typing import Dict, List, Optional, Any, Set
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ProxyStats:
    """Track comprehensive proxy statistics."""

    proxy_url: str
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    response_times: List[float] = field(default_factory=list)
    last_check: Optional[datetime] = None
    last_failure: Optional[datetime] = None
    failure_reasons: List[str] = field(default_factory=list)
    consecutive_failures: int = 0
    is_burned: bool = False
    burn_reason: Optional[str] = None
    geographic_location: Optional[str] = None
    anonymity_level: Optional[str] = None
    bandwidth_score: float = 1.0
    created_at: datetime = field(default_factory=datetime.now)

    @property
    def success_rate(self) -> float:
        """Calculate success rate percentage."""
        if self.total_requests == 0:
            return 0.0
        return (self.successful_requests / self.total_requests) * 100

    @property
    def avg_response_time(self) -> float:
        """Calculate average response time."""
        if not self.response_times:
            return float("inf")
        return statistics.mean(self.response_times[-50:])  # Last 50 requests

    @property
    def uptime_percentage(self) -> float:
        """Calculate uptime based on successful vs failed requests."""
        if self.total_requests == 0:
            return 100.0
        return (self.successful_requests / self.total_requests) * 100

    @property
    def health_score(self) -> float:
        """Calculate overall health score (0-1)."""
        if self.total_requests == 0:
            return 0.0

        if self.is_burned:
            return 0.0

        success_weight = 0.5
        response_time_weight = 0.3
        uptime_weight = 0.2

        success_score = self.success_rate / 100
        if not self.response_times:
            response_time_score = 1.0
        else:
            response_time_score = min(1.0, 5.0 / max(1.0, self.avg_response_time))
        uptime_score = self.uptime_percentage / 100

        return (
            success_score * success_weight
            + response_time_score * response_time_weight
            + uptime_score * uptime_weight
        )


class ProxyHealthChecker:
    """Advanced proxy health checker with automatic validation and rotation."""

    def __init__(self, config: Dict):
        self.config = config
        self.test_urls = config.get(
            "test_urls",
            [
                "https://httpbin.org/ip",
                "https://icanhazip.com",
                "https://api.ipify.org",
            ],
        )
        self.health_threshold = config.get("health_threshold", 0.8)
        self.check_interval = config.get("check_interval_seconds", 300)
        self.max_failures = config.get("max_failures_before_replacement", 3)
        self.timeout = config.get("timeout_seconds", 10)
        self.concurrent_checks = config.get("concurrent_checks", 5)

        # SSL verification configuration
        self.verify_ssl = config.get("verify_ssl", True)

        # Statistics and tracking
        self.proxy_stats: Dict[str, ProxyStats] = {}
        self.burned_proxies: Set[str] = set()
        self.last_health_check: Dict[str, datetime] = {}
        self._check_lock = asyncio.Lock()

        # Performance tracking config
        perf_config = config.get("performance_tracking", {})
        self.track_response_time = perf_config.get("track_response_time", True)
        self.track_success_rate = perf_config.get("track_success_rate", True)
        self.track_bandwidth = perf_config.get("track_bandwidth", True)
        self.history_retention_hours = perf_config.get("history_retention_hours", 24)

        logger.info(
            f"ProxyHealthChecker initialized with {len(self.test_urls)} test URLs, SSL verification: {self.verify_ssl}"
        )

    async def check_proxy_health(
        self, proxy: str, test_urls: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        Comprehensive proxy health check with multiple test URLs.

        Args:
            proxy: Proxy URL to test
            test_urls: Optional list of URLs to test against

        Returns:
            Dictionary with health check results
        """
        if proxy in self.burned_proxies:
            return {
                "proxy": proxy,
                "is_healthy": False,
                "health_score": 0.0,
                "reason": "Proxy marked as burned",
                "details": {},
            }

        test_urls = test_urls or self.test_urls
        results = []

        # Configure SSL verification based on configuration
        ssl_context = None if self.verify_ssl else False

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.timeout),
            connector=aiohttp.TCPConnector(ssl=ssl_context),
        ) as session:

            for url in test_urls:
                start_time = time.time()
                response_ctx = None
                response_obj = None
                try:
                    response_ctx = session.get(
                        url, proxy=proxy, allow_redirects=True
                    )

                    async def _process_response(response: aiohttp.ClientResponse) -> None:
                        response_time = time.time() - start_time
                        content = await response.text()

                        result = {
                            "url": url,
                            "status_code": response.status,
                            "response_time": response_time,
                            "success": 200 <= response.status < 300,
                            "content_length": len(content),
                            "error": None,
                        }

                        if result["success"]:
                            result["ip_detected"] = self._extract_ip_from_response(
                                content, url
                            )
                            result["content_valid"] = len(content) > 10

                        results.append(result)
                        await self._update_proxy_stats(
                            proxy, result["success"], response_time
                        )

                    if hasattr(response_ctx, "__aenter__"):
                        async with response_ctx as response:
                            response_obj = response
                            await _process_response(response)
                    else:
                        response_obj = await response_ctx
                        if hasattr(response_obj, "__aenter__"):
                            async with response_obj as nested_response:
                                response_obj = nested_response
                                await _process_response(nested_response)
                        else:
                            await _process_response(response_obj)

                except Exception as e:
                    response_time = time.time() - start_time
                    results.append(
                        {
                            "url": url,
                            "status_code": 0,
                            "response_time": response_time,
                            "success": False,
                            "content_length": 0,
                            "error": str(e),
                        }
                    )
                    await self._update_proxy_stats(proxy, False, response_time, str(e))
                finally:
                    if response_obj and not hasattr(response_ctx, "__aenter__"):
                        release = getattr(response_obj, "release", None)
                        if callable(release):
                            await release()

        # Calculate overall health
        successful_tests = sum(1 for r in results if r["success"])
        health_score = successful_tests / len(results) if results else 0.0
        avg_response_time = (
            statistics.mean([r["response_time"] for r in results])
            if results
            else float("inf")
        )
        is_healthy = health_score >= self.health_threshold

        health_result = {
            "proxy": proxy,
            "is_healthy": is_healthy,
            "health_score": health_score,
            "avg_response_time": avg_response_time,
            "successful_tests": successful_tests,
            "total_tests": len(results),
            "test_results": results,
            "checked_at": datetime.now().isoformat(),
        }

        # Update last check time
        self.last_health_check[proxy] = datetime.now()

        # Check if proxy should be marked as burned
        if not is_healthy:
            await self._check_burn_condition(proxy, health_result)

        logger.debug(
            f"Health check for {proxy}: score={health_score:.2f}, healthy={is_healthy}"
        )
        return health_result

    async def validate_proxy_batch(
        self, proxies: List[str]
    ) -> Dict[str, Dict[str, Any]]:
        """
        Validate multiple proxies concurrently for efficiency.

        Args:
            proxies: List of proxy URLs to validate

        Returns:
            Dictionary mapping proxy URLs to health check results
        """
        semaphore = asyncio.Semaphore(self.concurrent_checks)

        async def check_with_semaphore(proxy: str) -> tuple:
            async with semaphore:
                result = await self.check_proxy_health(proxy)
                return proxy, result

        logger.info(f"Starting batch validation of {len(proxies)} proxies")
        tasks = [check_with_semaphore(proxy) for proxy in proxies]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        validated_results: Dict[str, Dict[str, Any]] = {}
        occurrence_index: Dict[str, int] = defaultdict(int)

        for idx, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"Error in batch validation: {result}")
                continue
            proxy, health_data = result
            occurrence = occurrence_index[proxy]
            occurrence_index[proxy] += 1

            key = proxy if occurrence == 0 else f"{proxy}#{occurrence}"
            validated_results[key] = health_data

        healthy_count = sum(
            1 for r in validated_results.values() if r.get("is_healthy", False)
        )
        logger.info(
            f"Batch validation completed: {healthy_count}/{len(validated_results)} proxies healthy"
        )

        return validated_results

    async def monitor_proxy_performance(self, proxy: str) -> Dict[str, Any]:
        """
        Continuous performance monitoring for a specific proxy.

        Args:
            proxy: Proxy URL to monitor

        Returns:
            Performance monitoring data
        """
        stats = self.proxy_stats.get(proxy)
        if not stats:
            return {"error": "No statistics available for proxy"}

        performance_data = {
            "proxy": proxy,
            "success_rate": stats.success_rate,
            "avg_response_time": stats.avg_response_time,
            "uptime_percentage": stats.uptime_percentage,
            "health_score": stats.health_score,
            "total_requests": stats.total_requests,
            "consecutive_failures": stats.consecutive_failures,
            "last_check": stats.last_check.isoformat() if stats.last_check else None,
            "is_burned": stats.is_burned,
            "burn_reason": stats.burn_reason,
            "created_at": stats.created_at.isoformat(),
            "recent_failures": stats.failure_reasons[-10:],  # Last 10 failures
        }

        # Add geographic and anonymity info if available
        if stats.geographic_location:
            performance_data["geographic_location"] = stats.geographic_location
        if stats.anonymity_level:
            performance_data["anonymity_level"] = stats.anonymity_level

        return performance_data

    def get_proxy_statistics(self, proxy: str) -> Optional[Dict[str, Any]]:
        """
        Get detailed proxy performance metrics.

        Args:
            proxy: Proxy URL

        Returns:
            Detailed statistics or None if proxy not found
        """
        stats = self.proxy_stats.get(proxy)
        if not stats:
            return None

        return {
            "proxy_url": stats.proxy_url,
            "total_requests": stats.total_requests,
            "successful_requests": stats.successful_requests,
            "failed_requests": stats.failed_requests,
            "success_rate": stats.success_rate,
            "avg_response_time": stats.avg_response_time,
            "uptime_percentage": stats.uptime_percentage,
            "health_score": stats.health_score,
            "consecutive_failures": stats.consecutive_failures,
            "is_burned": stats.is_burned,
            "burn_reason": stats.burn_reason,
            "last_check": stats.last_check.isoformat() if stats.last_check else None,
            "last_failure": (
                stats.last_failure.isoformat() if stats.last_failure else None
            ),
            "failure_reasons": stats.failure_reasons,
            "geographic_location": stats.geographic_location,
            "anonymity_level": stats.anonymity_level,
            "bandwidth_score": stats.bandwidth_score,
            "created_at": stats.created_at.isoformat(),
        }

    def mark_proxy_burned(self, proxy: str, reason: str) -> None:
        """
        Mark proxy as burned and track the reason.

        Args:
            proxy: Proxy URL to mark as burned
            reason: Reason for burning the proxy
        """
        self.burned_proxies.add(proxy)

        if proxy in self.proxy_stats:
            self.proxy_stats[proxy].is_burned = True
            self.proxy_stats[proxy].burn_reason = reason
        else:
            # Create new stats entry for burned proxy
            stats = ProxyStats(proxy_url=proxy)
            stats.is_burned = True
            stats.burn_reason = reason
            self.proxy_stats[proxy] = stats

        logger.warning(f"Proxy {proxy} marked as burned: {reason}")

    def is_proxy_healthy(self, proxy: str) -> bool:
        """
        Check if proxy meets health requirements.

        Args:
            proxy: Proxy URL to check

        Returns:
            True if proxy is healthy, False otherwise
        """
        if proxy in self.burned_proxies:
            return False

        stats = self.proxy_stats.get(proxy)
        if not stats:
            return True  # Unknown proxy assumed healthy until tested

        return (
            stats.health_score >= self.health_threshold
            and stats.consecutive_failures < self.max_failures
            and not stats.is_burned
        )

    def get_healthy_proxies(self, proxies: List[str]) -> List[str]:
        """
        Return list of currently healthy proxies from given list.

        Args:
            proxies: List of proxy URLs to filter

        Returns:
            List of healthy proxy URLs
        """
        healthy = []
        for proxy in proxies:
            if self.is_proxy_healthy(proxy):
                healthy.append(proxy)

        # Sort by health score (best first)
        healthy.sort(
            key=lambda p: self.proxy_stats.get(p, ProxyStats(p)).health_score,
            reverse=True,
        )
        return healthy

    async def cleanup_old_statistics(self) -> None:
        """Remove old statistics beyond retention period."""
        cutoff_time = datetime.now() - timedelta(hours=self.history_retention_hours)

        proxies_to_remove = []
        for proxy, stats in self.proxy_stats.items():
            if (
                stats.last_check
                and stats.last_check < cutoff_time
                and not stats.is_burned
            ):
                proxies_to_remove.append(proxy)

        for proxy in proxies_to_remove:
            del self.proxy_stats[proxy]
            if proxy in self.last_health_check:
                del self.last_health_check[proxy]

        if proxies_to_remove:
            logger.info(
                f"Cleaned up statistics for {len(proxies_to_remove)} old proxies"
            )

    async def _update_proxy_stats(
        self, proxy: str, success: bool, response_time: float, error: str = None
    ) -> None:
        """Update proxy statistics after a request."""
        if proxy not in self.proxy_stats:
            self.proxy_stats[proxy] = ProxyStats(proxy_url=proxy)

        stats = self.proxy_stats[proxy]
        stats.total_requests += 1

        if success:
            stats.successful_requests += 1
            stats.consecutive_failures = 0
            if self.track_response_time:
                stats.response_times.append(response_time)
                # Keep only recent response times
                stats.response_times = stats.response_times[-100:]
        else:
            stats.failed_requests += 1
            stats.consecutive_failures += 1
            stats.last_failure = datetime.now()
            if error:
                stats.failure_reasons.append(f"{datetime.now().isoformat()}: {error}")
                # Keep only recent failures
                stats.failure_reasons = stats.failure_reasons[-20:]

        stats.last_check = datetime.now()

    async def _check_burn_condition(self, proxy: str, health_result: Dict) -> None:
        """Check if proxy should be marked as burned based on health result."""
        stats = self.proxy_stats.get(proxy)
        if not stats:
            return

        # Burn conditions
        if stats.consecutive_failures >= self.max_failures:
            self.mark_proxy_burned(
                proxy, f"Too many consecutive failures ({stats.consecutive_failures})"
            )
        elif stats.success_rate < 20 and stats.total_requests >= 10:
            self.mark_proxy_burned(
                proxy, f"Low success rate ({stats.success_rate:.1f}%)"
            )
        elif health_result["health_score"] == 0 and stats.total_requests >= 5:
            self.mark_proxy_burned(proxy, "Zero health score with sufficient tests")

    def _extract_ip_from_response(self, content: str, url: str) -> Optional[str]:
        """Extract IP address from response content for validation."""
        import re

        # Common IP extraction patterns
        ip_patterns = [
            r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b",  # Basic IP pattern
            r'"origin":\s*"([^"]+)"',  # httpbin.org format
            r'ip":\s*"([^"]+)"',  # Alternative JSON format
        ]

        for pattern in ip_patterns:
            match = re.search(pattern, content)
            if match:
                ip = match.group(1) if len(match.groups()) > 0 else match.group(0)
                # Basic IP validation
                if re.match(r"^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$", ip):
                    return ip

        return None

    def get_burned_proxies(self) -> Set[str]:
        """Get set of all burned proxy URLs."""
        return self.burned_proxies.copy()

    def reset_proxy_stats(self, proxy: str) -> bool:
        """
        Reset statistics for a proxy (useful when proxy is refreshed/replaced).

        Args:
            proxy: Proxy URL to reset

        Returns:
            True if proxy was reset, False if not found
        """
        if proxy in self.proxy_stats:
            del self.proxy_stats[proxy]

        if proxy in self.last_health_check:
            del self.last_health_check[proxy]

        self.burned_proxies.discard(proxy)

        logger.info(f"Reset statistics for proxy {proxy}")
        return True
