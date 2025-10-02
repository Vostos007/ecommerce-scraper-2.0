"""
Enhanced proxy rotator with comprehensive proxy management infrastructure.
Integrates health checking, premium proxy services, and intelligent selection.
"""

import asyncio
import math
import random
from datetime import UTC, datetime, timedelta
from typing import List, Dict, Optional, Any

import requests

from utils.logger import get_logger

# Import new proxy infrastructure components
from .proxy_health_checker import ProxyHealthChecker
from .premium_proxy_manager import PremiumProxyManager
from .exponential_backoff import ExponentialBackoff
from .content_validator import ContentValidator

logger = get_logger(__name__)


class ProxyRotator:
    """Enhanced proxy rotator with advanced proxy management and automatic replacement."""

    class _ProxyAcquisition:
        """Adapter that behaves both as an iterator and an awaitable result."""

        def __init__(
            self,
            rotator: "ProxyRotator",
            requirements: Optional[Dict],
        ) -> None:
            self._rotator = rotator
            self._requirements = requirements
            self._consumed = False
            self._result: Optional[str] = None

        def __iter__(self) -> "ProxyRotator._ProxyAcquisition":
            return self

        def __next__(self) -> str:
            if self._consumed:
                raise StopIteration
            self._result = self._rotator._get_next_proxy_basic(self._requirements)
            self._consumed = True
            if self._result is None:
                raise StopIteration
            return self._result

        def __await__(self):
            return self._await_impl().__await__()

        async def _await_impl(self) -> Optional[str]:
            if not self._consumed:
                self._result = await self._rotator._get_next_proxy_async(
                    self._requirements
                )
                self._consumed = True
            return self._result

    def __init__(self, proxies: List[str], config: Dict = None):
        self.config = config or {}
        self.proxies = proxies[:]
        self.current_index = 0
        self.failed_proxies = set()
        self.burned_proxies = set()

        # Initialize new components
        self.health_checker = ProxyHealthChecker(
            self.config.get("health_checker", {})
        )
        self.premium_manager = PremiumProxyManager(
            self.config.get("premium_proxies", {})
        )
        self.backoff = ExponentialBackoff(self.config.get("backoff", {}))
        self.content_validator = ContentValidator(
            self.config.get("content_validator", {})
        )

        # Performance tracking
        self.proxy_stats = {}
        self.last_health_check = {}
        self.replacement_in_progress = set()

        # Configuration
        self.auto_replace_burned = self.config.get("auto_replace_burned", True)
        self.min_healthy_proxies = self.config.get("min_healthy_proxies", 3)
        self.health_check_interval = self.config.get(
            "health_check_interval_seconds", 300
        )
        self.intelligent_selection = self.config.get("intelligent_selection", True)
        self.load_balancing = self.config.get("load_balancing", True)

        autoscale_config = self.config.get("autoscale", {})
        self.autoscale_enabled = autoscale_config.get("enabled", True)
        self.autoscale_safety_factor = float(
            autoscale_config.get("safety_factor", 1.5)
        )
        self.autoscale_target_success_rate = float(
            autoscale_config.get("target_success_rate", 0.85)
        )
        self.autoscale_min_proxy_count = int(
            autoscale_config.get("min_proxy_count", 5)
        )
        self.autoscale_max_proxy_count = int(
            autoscale_config.get("max_proxy_count", 100)
        )
        self.autoscale_default_concurrency = int(
            autoscale_config.get("default_concurrency", 32)
        )
        self.autoscale_warning_threshold = float(
            autoscale_config.get("warning_threshold", 0.8)
        )
        self.autoscale_critical_threshold = float(
            autoscale_config.get("critical_threshold", 0.5)
        )
        self.autoscale_cooldown_seconds = int(
            autoscale_config.get("cooldown_seconds", 30 * 60)
        )
        self._last_autoscale_time: Optional[datetime] = None

        # Performance metrics
        self.total_requests = 0
        self.successful_requests = 0
        self.proxy_rotations = 0

        # Background monitoring task
        self._monitoring_task: Optional[asyncio.Task] = None
        self._autoscale_lock: Optional[asyncio.Lock] = None

        logger.info(
            f"Enhanced ProxyRotator initialized with {len(self.proxies)} proxies"
        )

    async def start(self) -> None:
        """Start background monitoring and other async tasks."""
        if self.config.get("enable_background_monitoring", True):
            try:
                self._monitoring_task = asyncio.create_task(
                    self._background_monitoring()
                )
                logger.info("ProxyRotator background monitoring started")
            except Exception as e:
                logger.warning(f"Failed to start background monitoring: {e}")

        # Start premium proxy manager if available
        if hasattr(self.premium_manager, "start_auto_refresh"):
            await self.premium_manager.start_auto_refresh()

    def get_next_proxy(self, requirements: Optional[Dict] = None) -> "ProxyRotator._ProxyAcquisition":
        """Return adapter that supports both sync iteration and awaiting."""

        return ProxyRotator._ProxyAcquisition(self, requirements)

    async def _get_next_proxy_async(
        self, requirements: Optional[Dict] = None
    ) -> Optional[str]:
        """Full async proxy selection with health checks and intelligent routing."""

        try:
            healthy_proxies = await self._get_healthy_proxies()

            if not healthy_proxies:
                logger.warning(
                    "No healthy proxies available, attempting to refresh pool"
                )
                await self._refresh_proxy_pool()
                healthy_proxies = await self._get_healthy_proxies()

                if not healthy_proxies:
                    logger.error("No proxies available after refresh attempt")
                    return None

            if requirements:
                filtered_proxies = self._filter_proxies_by_requirements(
                    healthy_proxies, requirements
                )
                if filtered_proxies:
                    healthy_proxies = filtered_proxies

            selected_proxy = await self._select_best_proxy(healthy_proxies)

            if selected_proxy:
                self.proxy_rotations += 1
                logger.debug(f"Selected proxy: {selected_proxy[:50]}...")

            return selected_proxy

        except Exception as exc:  # noqa: BLE001
            logger.error(f"Error selecting proxy: {exc}")
            return None

    def _get_next_proxy_basic(
        self, requirements: Optional[Dict] = None
    ) -> Optional[str]:
        """Lightweight round-robin selection for synchronous code paths."""

        if not self.proxies:
            return None

        start_index = self.current_index
        seen = 0
        proxies_count = len(self.proxies)

        while seen < proxies_count:
            proxy = self.proxies[self.current_index]
            self.current_index = (self.current_index + 1) % proxies_count
            seen += 1

            if proxy in self.failed_proxies or proxy in self.burned_proxies:
                continue

            if requirements:
                healthy = self._filter_proxies_by_requirements([proxy], requirements)
                if not healthy:
                    continue

            self.proxy_rotations += 1
            logger.debug(f"Selected proxy (sync path): {proxy[:50]}...")
            return proxy

        self.current_index = start_index
        return None

    def get_next_proxy_sync(self, requirements: Optional[Dict] = None) -> Optional[str]:
        """Return next proxy using synchronous round-robin semantics."""

        handle = self.get_next_proxy(requirements)
        try:
            return next(handle)
        except StopIteration:
            return None

    async def mark_proxy_success(
        self, proxy: str, response_time: float, content: str = None
    ) -> None:
        """
        Mark proxy as successful and update statistics.

        Args:
            proxy: Proxy URL that was successful
            response_time: Response time in seconds
            content: Optional response content for validation
        """
        try:
            self.total_requests += 1  # Increment total requests
            self.successful_requests += 1

            # Update health checker stats
            await self.health_checker._update_proxy_stats(proxy, True, response_time)

            # Update premium proxy manager if applicable
            if hasattr(self.premium_manager, "mark_proxy_used"):
                self.premium_manager.mark_proxy_used(proxy, response_time, True)

            # Update exponential backoff
            self.backoff.track_success(proxy, response_time)

            # Validate content if provided
            if content and self.content_validator:
                validation_result = self.content_validator.validate_response(
                    content, ""
                )
                if not validation_result.is_valid:
                    logger.warning(f"Proxy {proxy[:50]} returned invalid content")
                    await self.mark_proxy_failure(
                        proxy, "invalid_content", response_time
                    )
                    return

            # Remove from failed proxies if it was there
            self.failed_proxies.discard(proxy)

            logger.debug(f"Marked proxy success: {proxy[:50]}")

        except Exception as e:
            logger.error(f"Error marking proxy success: {e}")

    async def mark_proxy_failure(
        self, proxy: str, error_type: str, response_time: float = 0.0
    ) -> None:
        """
        Mark proxy as failed and handle potential burning.

        Args:
            proxy: Proxy URL that failed
            error_type: Type of error encountered
            response_time: Response time in seconds
        """
        try:
            self.total_requests += 1  # Increment total requests

            # Update health checker stats
            await self.health_checker._update_proxy_stats(
                proxy, False, response_time, error_type
            )

            # Update premium proxy manager if applicable
            if hasattr(self.premium_manager, "mark_proxy_used"):
                self.premium_manager.mark_proxy_used(proxy, response_time, False)

            # Update exponential backoff
            self.backoff.track_failure(proxy, error_type, response_time)

            # Add to failed proxies
            self.failed_proxies.add(proxy)

            # Check if proxy should be burned
            if await self._should_burn_proxy(proxy, error_type):
                await self.mark_proxy_burned(proxy, error_type)

            logger.debug(f"Marked proxy failure: {proxy[:50]} - {error_type}")

        except Exception as e:
            logger.error(f"Error marking proxy failure: {e}")

    async def mark_proxy_burned(self, proxy: str, reason: str) -> None:
        """
        Mark proxy as burned and trigger replacement.

        Args:
            proxy: Proxy URL to burn
            reason: Reason for burning the proxy
        """
        try:
            self.burned_proxies.add(proxy)
            self.failed_proxies.add(proxy)

            # Mark in health checker
            self.health_checker.mark_proxy_burned(proxy, reason)

            logger.warning(f"Proxy burned: {proxy[:50]} - {reason}")

            # Attempt automatic replacement if enabled
            if self.auto_replace_burned and proxy not in self.replacement_in_progress:
                await self._replace_burned_proxy(proxy, reason)

            # Check if we need more proxies
            healthy_count = await self._get_healthy_proxy_count()
            if healthy_count < self.min_healthy_proxies:
                logger.warning(
                    f"Low healthy proxy count: {healthy_count}/{self.min_healthy_proxies}"
                )
                await self._emergency_proxy_refresh()

        except Exception as e:
            logger.error(f"Error burning proxy: {e}")

    async def validate_and_refresh_pool(self) -> int:
        """
        Validate entire proxy pool and refresh if needed.

        Returns:
            Number of healthy proxies after validation
        """
        try:
            logger.info("Starting proxy pool validation and refresh")

            # Validate existing proxies
            validation_results = await self.health_checker.validate_proxy_batch(
                self.proxies
            )

            healthy_proxies = []
            burned_count = 0

            for proxy_key, result in validation_results.items():
                result_proxy = result.get("proxy", proxy_key)
                if result.get("is_healthy", False):
                    healthy_proxies.append(result_proxy)
                    # Remove from failed/burned if it's healthy again
                    self.failed_proxies.discard(result_proxy)
                else:
                    burned_count += 1
                    await self.mark_proxy_burned(result_proxy, "failed_validation")

            # Update proxy list (preserve order, drop duplicates)
            original_count = len(self.proxies)
            healthy_unique = list(dict.fromkeys(healthy_proxies))
            self.proxies = healthy_unique

            logger.info(
                f"Proxy validation completed: {len(healthy_unique)}/{original_count} healthy"
            )

            # Refresh from premium service if needed
            if len(healthy_unique) < self.min_healthy_proxies:
                await self._refresh_from_premium_service()

            return len(self.proxies)

        except Exception as e:
            logger.error(f"Error validating proxy pool: {e}")
            return 0

    async def get_proxy_statistics(self) -> Dict[str, Any]:
        """
        Get comprehensive proxy statistics.

        Returns:
            Dictionary with detailed proxy statistics
        """
        try:
            healthy_count = await self._get_healthy_proxy_count()

            stats = {
                "total_proxies": len(self.proxies),
                "healthy_proxies": healthy_count,
                "failed_proxies": len(self.failed_proxies),
                "burned_proxies": len(self.burned_proxies),
                "total_requests": self.total_requests,
                "successful_requests": self.successful_requests,
                "success_rate": (self.successful_requests / max(1, self.total_requests))
                * 100,
                "proxy_rotations": self.proxy_rotations,
                "health_checker_stats": len(self.health_checker.proxy_stats),
                "circuit_breakers_open": len(
                    [p for p in self.backoff.retry_states.values() if p.is_circuit_open]
                ),
            }

            # Add premium proxy manager stats if available
            premium_stats = None
            if hasattr(self.premium_manager, "monitor_proxy_usage"):
                try:
                    premium_stats = await self.premium_manager.monitor_proxy_usage()
                except Exception as premium_error:
                    logger.warning(
                        "Failed to collect premium proxy stats: %s",
                        premium_error,
                    )

            stats["premium_proxy_stats"] = premium_stats or {}

            if self.autoscale_enabled:
                autoscale_data = await self.get_autoscale_recommendations(
                    self.autoscale_default_concurrency
                )
                stats["autoscale"] = autoscale_data
                stats["optimal_proxy_count"] = autoscale_data.get(
                    "optimal_proxy_count"
                )
                stats["recommended_purchase"] = autoscale_data.get(
                    "recommended_purchase"
                )
                stats["autoscale_status"] = autoscale_data.get("status")
                stats["purchase_estimate"] = autoscale_data.get("estimated_cost")

            stats.setdefault("active_proxies", stats["healthy_proxies"])

            # Add top performing proxies
            stats["top_performing_proxies"] = await self._get_top_performing_proxies(5)

            return stats

        except Exception as e:
            logger.error(f"Error getting proxy statistics: {e}")
            return {"error": str(e)}

    async def _get_healthy_proxies(self) -> List[str]:
        """Get list of currently healthy proxies."""
        healthy = []

        for proxy in self.proxies:
            if (
                proxy not in self.failed_proxies
                and proxy not in self.burned_proxies
                and self.health_checker.is_proxy_healthy(proxy)
                and self.backoff.is_identifier_healthy(proxy)
            ):
                healthy.append(proxy)

        return healthy

    async def _get_healthy_proxy_count(self) -> int:
        """Get count of healthy proxies."""
        healthy_proxies = await self._get_healthy_proxies()
        return len(healthy_proxies)

    async def _select_best_proxy(self, available_proxies: List[str]) -> Optional[str]:
        """
        Select best proxy from available list using intelligent selection.

        Args:
            available_proxies: List of available proxy URLs

        Returns:
            Best proxy URL or None
        """
        if not available_proxies:
            return None

        if not self.intelligent_selection:
            # Simple round-robin selection
            proxy = available_proxies[self.current_index % len(available_proxies)]
            self.current_index += 1
            return proxy

        # Intelligent selection based on performance
        proxy_scores = []

        for proxy in available_proxies:
            score = 0.0

            # Health checker score
            if proxy in self.health_checker.proxy_stats:
                stats = self.health_checker.proxy_stats[proxy]
                score += stats.health_score * 0.4

            # Exponential backoff score (inverse of failure rate)
            if proxy in self.backoff.retry_states:
                retry_state = self.backoff.retry_states[proxy]
                score += retry_state.success_rate * 0.3

            # Usage balancing (prefer less used proxies)
            usage_count = self.health_checker.proxy_stats.get(
                proxy, type("obj", (object,), {"total_requests": 0})
            ).total_requests
            max_usage = max(
                (s.total_requests for s in self.health_checker.proxy_stats.values()),
                default=1,
            )
            usage_score = 1.0 - (usage_count / max(max_usage, 1))
            score += usage_score * 0.2

            # Random factor for load balancing
            score += random.random() * 0.1

            proxy_scores.append((proxy, score))

        # Sort by score and select best
        proxy_scores.sort(key=lambda x: x[1], reverse=True)
        selected_proxy = proxy_scores[0][0]

        # Update index for round-robin fallback
        try:
            self.current_index = available_proxies.index(selected_proxy) + 1
        except ValueError:
            self.current_index += 1

        return selected_proxy

    async def _should_burn_proxy(self, proxy: str, error_type: str) -> bool:
        """
        Determine if proxy should be burned based on error type and history.

        Args:
            proxy: Proxy URL to check
            error_type: Type of error encountered

        Returns:
            True if proxy should be burned
        """
        # Immediate burn conditions
        immediate_burn_errors = ["blocked", "captcha", "authentication"]
        if any(
            burn_error in error_type.lower() for burn_error in immediate_burn_errors
        ):
            return True

        # Check health checker recommendation
        if proxy in self.health_checker.proxy_stats:
            stats = self.health_checker.proxy_stats[proxy]
            if stats.consecutive_failures >= 5 or stats.health_score < 0.2:
                return True

        # Check exponential backoff state
        if proxy in self.backoff.retry_states:
            retry_state = self.backoff.retry_states[proxy]
            if retry_state.consecutive_failures >= 3 or retry_state.success_rate < 0.1:
                return True

        return False

    async def _replace_burned_proxy(self, burned_proxy: str, reason: str) -> None:
        """
        Attempt to replace a burned proxy with a new one.

        Args:
            burned_proxy: URL of burned proxy
            reason: Reason why proxy was burned
        """
        try:
            self.replacement_in_progress.add(burned_proxy)

            # Try to get replacement from premium service
            if hasattr(self.premium_manager, "get_best_proxies"):
                replacements = self.premium_manager.get_best_proxies(count=1)
                if replacements:
                    new_proxy = replacements[0].formatted_url
                    if new_proxy not in self.proxies:
                        self.proxies.append(new_proxy)
                        logger.info(
                            f"Replaced burned proxy with premium proxy: {new_proxy[:50]}"
                        )
                        return

            # If premium replacement failed, try refreshing pool
            await self._refresh_from_premium_service()

        except Exception as e:
            logger.error(f"Error replacing burned proxy: {e}")
        finally:
            self.replacement_in_progress.discard(burned_proxy)

    async def _refresh_proxy_pool(self) -> None:
        """Refresh proxy pool from premium service."""
        try:
            if hasattr(self.premium_manager, "refresh_proxy_pool"):
                success = await self.premium_manager.refresh_proxy_pool()
                if success:
                    # Add new proxies from premium service
                    new_proxies = [
                        p.formatted_url for p in self.premium_manager.proxy_pool
                    ]
                    for proxy in new_proxies:
                        if proxy not in self.proxies:
                            self.proxies.append(proxy)

                    logger.info(
                        f"Refreshed proxy pool: {len(new_proxies)} new proxies added"
                    )

        except Exception as e:
            logger.error(f"Error refreshing proxy pool: {e}")

    async def _refresh_from_premium_service(self) -> None:
        """Refresh proxies specifically from premium service."""
        try:
            if hasattr(self.premium_manager, "fetch_proxy_list"):
                new_proxies = await self.premium_manager.fetch_proxy_list()
                added_count = 0

                for proxy_info in new_proxies:
                    proxy_url = proxy_info.formatted_url
                    if proxy_url not in self.proxies:
                        self.proxies.append(proxy_url)
                        added_count += 1

                if added_count > 0:
                    logger.info(f"Added {added_count} new proxies from premium service")

        except Exception as e:
            logger.error(f"Error refreshing from premium service: {e}")

    async def _emergency_proxy_refresh(self) -> None:
        """Emergency proxy refresh when running low on healthy proxies."""
        try:
            logger.warning("Emergency proxy refresh triggered")

            # Try premium service first
            await self._refresh_from_premium_service()

            # Reset some failed proxies if still low
            healthy_count = await self._get_healthy_proxy_count()
            if healthy_count < self.min_healthy_proxies:
                # Reset half of failed proxies
                failed_list = list(self.failed_proxies)
                reset_count = len(failed_list) // 2
                for proxy in failed_list[:reset_count]:
                    self.failed_proxies.discard(proxy)
                    self.backoff.reset_backoff(proxy)

                logger.info(f"Reset {reset_count} failed proxies due to emergency")

        except Exception as e:
            logger.error(f"Error in emergency proxy refresh: {e}")

    def _filter_proxies_by_requirements(
        self, proxies: List[str], requirements: Dict
    ) -> List[str]:
        """Filter proxies based on requirements."""
        if not requirements:
            return proxies

        required_country = requirements.get("country")
        required_protocol = requirements.get("protocol")

        if not required_country and not required_protocol:
            return proxies

        # Get proxy metadata from premium proxy manager if available
        if not hasattr(self.premium_manager, "active_proxies"):
            logger.debug(
                "Premium proxy manager not available for filtering, returning all proxies"
            )
            return proxies

        filtered_proxies = []

        for proxy in proxies:
            try:
                # Check if proxy has metadata in premium manager
                proxy_info = None
                for url, info in self.premium_manager.active_proxies.items():
                    if url == proxy or (
                        hasattr(info, "proxy_url") and info.proxy_url == proxy
                    ):
                        proxy_info = info
                        break

                if not proxy_info:
                    # No metadata available, include proxy by default
                    filtered_proxies.append(proxy)
                    continue

                # Filter by country if specified
                if required_country:
                    proxy_country = getattr(proxy_info, "country", None)
                    if (
                        proxy_country
                        and proxy_country.upper() != required_country.upper()
                    ):
                        logger.debug(
                            f"Proxy {proxy[:50]} excluded: country {proxy_country} != {required_country}"
                        )
                        continue

                # Filter by protocol if specified
                if required_protocol:
                    proxy_protocol = getattr(proxy_info, "protocol", None)
                    if (
                        proxy_protocol
                        and proxy_protocol.lower() != required_protocol.lower()
                    ):
                        logger.debug(
                            f"Proxy {proxy[:50]} excluded: protocol {proxy_protocol} != {required_protocol}"
                        )
                        continue

                # Proxy meets requirements
                filtered_proxies.append(proxy)

            except Exception as e:
                logger.warning(f"Error filtering proxy {proxy[:50]}: {e}")
                # Include proxy on error to avoid breaking functionality
                filtered_proxies.append(proxy)

        logger.debug(
            f"Filtered {len(proxies)} proxies to {len(filtered_proxies)} based on requirements: {requirements}"
        )
        return filtered_proxies

    async def _get_top_performing_proxies(self, count: int) -> List[Dict[str, Any]]:
        """Get top performing proxies."""
        top_proxies = []

        for proxy in self.proxies:
            if proxy in self.health_checker.proxy_stats:
                stats = self.health_checker.proxy_stats[proxy]
                top_proxies.append(
                    {
                        "proxy": proxy[:50] + "...",
                        "health_score": stats.health_score,
                        "success_rate": stats.success_rate,
                        "avg_response_time": stats.avg_response_time,
                        "total_requests": stats.total_requests,
                    }
                )

        top_proxies.sort(key=lambda x: x["health_score"], reverse=True)
        return top_proxies[:count]

    def compute_optimal_proxy_count(
        self, concurrency: int, overrides: Optional[Dict[str, Any]] = None
    ) -> int:
        """Estimate optimal number of proxies for given concurrency."""

        if concurrency <= 0:
            return self.autoscale_min_proxy_count

        params = {
            "safety_factor": self.autoscale_safety_factor,
            "target_success_rate": self.autoscale_target_success_rate,
            "min_proxy_count": self.autoscale_min_proxy_count,
            "max_proxy_count": self.autoscale_max_proxy_count,
        }
        if overrides:
            params.update(overrides)

        safety_factor = max(1.0, float(params.get("safety_factor", 1.5)))
        target_success_rate = max(
            0.1, min(0.99, float(params.get("target_success_rate", 0.85)))
        )
        min_count = max(1, int(params.get("min_proxy_count", 5)))
        max_count = max(min_count, int(params.get("max_proxy_count", 100)))

        optimal = math.ceil(concurrency * safety_factor / target_success_rate)
        optimal = max(min_count, optimal)
        optimal = min(max_count, optimal)
        return int(optimal)

    async def get_autoscale_recommendations(
        self, concurrency: int
    ) -> Dict[str, Any]:
        """Provide autoscale recommendations based on current proxy health."""

        optimal = self.compute_optimal_proxy_count(concurrency)
        healthy = await self._get_healthy_proxy_count()
        deficit = max(0, optimal - healthy)

        status = "sufficient"
        if optimal > 0:
            ratio = healthy / optimal
            if ratio < self.autoscale_critical_threshold:
                status = "critical"
            elif ratio < self.autoscale_warning_threshold:
                status = "warning"

        recommendations: Dict[str, Any] = {
            "optimal_proxy_count": optimal,
            "current_healthy": healthy,
            "deficit": deficit,
            "status": status,
            "recommended_purchase": 0,
            "estimated_cost": 0.0,
        }

        if deficit > 0 and hasattr(self.premium_manager, "get_purchase_recommendations"):
            purchase = self.premium_manager.get_purchase_recommendations(deficit)
            if isinstance(purchase, dict):
                recommendations["recommended_purchase"] = int(
                    purchase.get("recommended_count", 0)
                )
                recommendations["estimated_cost"] = float(
                    purchase.get("estimated_cost", 0.0)
                )
                recommendations["can_purchase"] = bool(
                    purchase.get("can_purchase", False)
                )
                recommendations["budget_remaining"] = purchase.get(
                    "budget_remaining"
                )
                recommendations["cooldown_remaining_minutes"] = purchase.get(
                    "cooldown_remaining_minutes"
                )

        return recommendations

    async def auto_scale_if_needed(self, concurrency: int) -> bool:
        """Trigger automatic scaling if conditions are met."""

        if not self.autoscale_enabled:
            return False

        if self._autoscale_lock is None:
            self._autoscale_lock = asyncio.Lock()

        async with self._autoscale_lock:
            recommendations = await self.get_autoscale_recommendations(concurrency)
            deficit = recommendations.get("deficit", 0)
            if deficit <= 0:
                return False

            now = datetime.now(UTC)
            if (
                self._last_autoscale_time
                and (now - self._last_autoscale_time)
                < timedelta(seconds=self.autoscale_cooldown_seconds)
            ):
                return False

            if not hasattr(self.premium_manager, "ensure_min_proxy_pool"):
                return False

            try:
                result = await self.premium_manager.ensure_min_proxy_pool(
                    recommendations["optimal_proxy_count"]
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(f"Error during autoscale provisioning: {exc}")
                return False

            success = isinstance(result, dict) and result.get("success")
            if success:
                self._last_autoscale_time = now
                logger.info(
                    "Autoscale executed: purchased %(purchased)s proxies (target %(target)s)",
                    {
                        "purchased": result.get("purchased"),
                        "target": result.get("target_count"),
                    },
                )
            return bool(success)

    async def _background_monitoring(self) -> None:
        """Background monitoring task for proxy health."""
        while True:
            try:
                await asyncio.sleep(self.health_check_interval)

                # Periodic health check
                healthy_count = await self._get_healthy_proxy_count()
                if healthy_count < self.min_healthy_proxies:
                    logger.warning(f"Low proxy count detected: {healthy_count}")
                    await self._emergency_proxy_refresh()

                # Autoscale evaluation
                try:
                    await self.auto_scale_if_needed(
                        self.autoscale_default_concurrency
                    )
                except Exception as autoscale_exc:  # noqa: BLE001
                    logger.error(f"Autoscale check failed: {autoscale_exc}")

                # Clean up old statistics
                await self.health_checker.cleanup_old_statistics()
                self.backoff.cleanup_old_states()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in background monitoring: {e}")

    # Legacy method compatibility - renamed to avoid redefinition
    def get_next_proxy_sync_legacy(self) -> Optional[str]:
        """Legacy entry point preserved for callers expecting sync behaviour."""

        try:
            return self.get_next_proxy_sync()
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Error in sync proxy selection: {exc}")
            return None

    def mark_failed(self, proxy: str):
        """Legacy method for backward compatibility."""
        try:
            loop = asyncio.get_event_loop()
            loop.run_until_complete(self.mark_proxy_failure(proxy, "unknown"))
        except Exception as e:
            logger.error(f"Error in legacy mark_failed: {e}")

    def health_check(self, proxy: str, timeout: int = 10) -> bool:
        """Legacy health check method for backward compatibility."""
        if self.config.get("use_async_health_check", False):
            try:
                loop = asyncio.get_event_loop()
                result = loop.run_until_complete(
                    self.health_checker.check_proxy_health(proxy)
                )
                return result.get("is_healthy", False)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"Error during async health check: {exc}")

        # Fallback to original synchronous behaviour to support existing tests
        try:
            response = requests.get(
                "https://httpbin.org/get",
                proxies={"http": proxy, "https": proxy},
                timeout=timeout,
            )
            return 200 <= response.status_code < 400
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Error in legacy health check: {exc}")
            return False

    def validate_proxies(self):
        """Legacy validation method for backward compatibility."""
        try:
            loop = asyncio.get_event_loop()
            healthy_count = loop.run_until_complete(self.validate_and_refresh_pool())
            if healthy_count == 0:
                raise ValueError("No valid proxies available")
        except Exception as e:
            logger.error(f"Error in legacy validate_proxies: {e}")
            raise ValueError("No valid proxies available")
