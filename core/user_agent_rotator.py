"""
Mandatory user-agent rotation system with intelligent patterns.

This module provides production-grade user-agent rotation capabilities with:
- Mandatory rotation on every request (not optional)
- Multiple rotation strategies (sequential, random, weighted, intelligent)
- Domain-specific preferences and memory
- Performance tracking and optimization
- Realistic user-agent progression patterns
- Automatic pool refresh and validation
"""

import random
import time
import re
from typing import Dict, Any, List, Optional
import logging
import asyncio
from fake_useragent import UserAgent, FakeUserAgentError

logger = logging.getLogger(__name__)


class UserAgentRotator:
    """
    Mandatory user-agent rotation system with intelligent selection patterns.

    Features:
    - Mandatory rotation enforcement on every request
    - Multiple rotation strategies for different use cases
    - Domain-specific user-agent preferences and memory
    - Performance tracking and success rate analysis
    - Realistic user-agent progression simulation
    - Automatic pool management and refresh
    """

    def __init__(self, config: Dict[str, Any]):
        """Initialize UserAgentRotator with configuration."""
        self.config = config
        self.enabled = config.get("enabled", True)
        self.mandatory_rotation = config.get("mandatory_rotation", True)
        self.rotation_strategy = config.get("strategy", "intelligent")
        self.ua_pool_size = config.get("pool_size", 100)
        self.refresh_interval = config.get("refresh_interval_hours", 24)

        # Rotation triggers
        self.rotation_triggers = config.get("rotation_triggers", {})
        self.every_request = self.rotation_triggers.get("every_request", True)
        self.on_proxy_rotation = self.rotation_triggers.get("on_proxy_rotation", True)
        self.on_error = self.rotation_triggers.get("on_error", True)
        self.time_based_minutes = self.rotation_triggers.get("time_based_minutes", 30)

        # User agent sources
        self.ua_sources = config.get("user_agent_sources", {})
        self.use_fake_useragent = self.ua_sources.get("fake_useragent", True)
        self.use_custom_lists = self.ua_sources.get("custom_lists", True)
        self.use_real_browser_data = self.ua_sources.get("real_browser_data", True)

        # Filtering options
        self.filtering = config.get("filtering", {})
        self.min_browser_version = self.filtering.get("min_browser_version", 90)
        self.exclude_mobile = self.filtering.get("exclude_mobile", False)
        self.exclude_bots = self.filtering.get("exclude_bots", True)
        self.prefer_chrome = self.filtering.get("prefer_chrome", True)

        # Performance tracking
        self.performance_tracking = config.get("performance_tracking", {})
        self.track_success_rates = self.performance_tracking.get(
            "track_success_rates", True
        )
        self.domain_preferences = self.performance_tracking.get(
            "domain_preferences", True
        )
        self.auto_optimize = self.performance_tracking.get("auto_optimize", True)

        # User agent pools by type
        self.browser_agents: List[str] = []
        self.mobile_agents: List[str] = []
        self.bot_agents: List[str] = []

        # Rotation tracking
        self.current_indices = {"browser": 0, "mobile": 0, "bot": 0}
        self.usage_stats: Dict[str, Any] = {}
        self.domain_ua_preferences: Dict[str, str] = {}
        self.last_used_ua = None
        self.last_rotation_time = time.time()

        # Performance tracking
        self.ua_performance: Dict[str, float] = {}  # Track success rates per UA
        self.ua_usage_count: Dict[str, int] = {}  # Track usage frequency
        self.domain_success_rates: Dict[str, float] = (
            {}
        )  # Track success rates per domain

        # Pool management
        self.pool_last_refresh = 0
        self._loop = self._ensure_event_loop()
        self.pool_refresh_lock = asyncio.Lock()

        # Initialize user agent pools - defer async initialization
        self._initialization_task = None

    async def start(self) -> None:
        """Start the UserAgentRotator and initialize pools."""
        if self._initialization_task is None:
            self._initialization_task = asyncio.create_task(self._initialize_ua_pools())
        if self._initialization_task:
            await self._initialization_task

    @staticmethod
    def _ensure_event_loop() -> asyncio.AbstractEventLoop:
        """Ensure there is an event loop available for async primitives."""
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            return loop

    async def get_next_user_agent(
        self,
        request_type: str = "browser",
        domain: Optional[str] = None,
        force_new: Optional[bool] = None,
    ) -> str:
        """
        Get next user agent with mandatory rotation enforcement.

        Args:
            request_type: Type of user agent ('browser', 'mobile', 'bot')
            domain: Domain for domain-specific preferences
            force_new: Force getting a different UA from last used

        Returns:
            User agent string
        """
        if not self.enabled:
            return self._get_default_user_agent()

        # Check if pool needs refresh
        await self._refresh_pools_if_needed()

        # Enforce mandatory rotation
        if self.mandatory_rotation or force_new:
            force_new = True

        try:
            # Get appropriate pool
            ua_pool = self._get_ua_pool(request_type)
            if not ua_pool:
                logger.warning(f"No user agents available for type: {request_type}")
                return self._get_default_user_agent()

            # Apply rotation strategy
            if self.rotation_strategy == "intelligent":
                ua = await self._get_intelligent_user_agent(ua_pool, domain, force_new)
            elif self.rotation_strategy == "weighted":
                ua = self._get_weighted_user_agent(ua_pool, domain, force_new)
            elif self.rotation_strategy == "random":
                ua = self._get_random_user_agent(ua_pool, force_new)
            else:  # sequential
                ua = self._get_sequential_user_agent(ua_pool, request_type, force_new)

            # Validate user agent
            if not self._validate_user_agent(ua):
                logger.warning(f"Invalid user agent generated: {ua[:50]}...")
                return self._get_default_user_agent()

            # Track usage and rotation
            self._track_ua_usage(ua, domain)
            self.last_used_ua = ua
            self.last_rotation_time = time.time()

            logger.debug(
                f"Selected user agent [{self.rotation_strategy}]: {ua[:60]}..."
            )
            return ua

        except Exception as e:
            logger.error(f"Error getting next user agent: {e}")
            return self._get_default_user_agent()

    async def get_next_user_agent_mandatory(
        self, domain: str = None, force_rotation: bool = True
    ) -> str:
        """
        Get next user agent with mandatory rotation (always returns different UA).

        Args:
            domain: Domain for domain-specific preferences
            force_rotation: Force rotation even if not time-based

        Returns:
            User agent string (guaranteed to be different from last)
        """
        if self.mandatory_rotation:
            await self._refresh_pools_if_needed()
            ua_pool = self._get_ua_pool("browser")
            if ua_pool:
                ua = self._get_sequential_user_agent(ua_pool, "browser", True)
                self._track_ua_usage(ua, domain)
                self.last_used_ua = ua
                self.last_rotation_time = time.time()
                return ua

        return await self.get_next_user_agent(
            request_type="browser", domain=domain, force_new=True
        )

    async def get_user_agent_for_domain(self, domain: str) -> str:
        """
        Get optimal user agent for a specific domain based on historical performance.

        Args:
            domain: Target domain

        Returns:
            Best performing user agent for the domain
        """
        if not self.domain_preferences or domain not in self.domain_ua_preferences:
            return await self.get_next_user_agent(domain=domain)

        # Get best performing UA for this domain
        domain_prefs = self.domain_ua_preferences[domain]
        if domain_prefs:
            # Sort by success rate
            best_ua = max(domain_prefs.items(), key=lambda x: x[1]["success_rate"])
            return best_ua[0]

        return await self.get_next_user_agent(domain=domain)

    def get_realistic_user_agent_chain(self, count: int = 5) -> List[str]:
        """
        Generate realistic user agent progression for session simulation.

        Args:
            count: Number of user agents in the chain

        Returns:
            List of user agents that simulate realistic progression
        """
        if count <= 0:
            return []

        chain = []
        current_base = random.choice(["Chrome", "Firefox", "Safari", "Edge"])

        for i in range(count):
            # Simulate realistic version progression
            if current_base == "Chrome":
                version = random.randint(90, 120)
                ua = self._generate_chrome_ua(version)
            elif current_base == "Firefox":
                version = random.randint(90, 110)
                ua = self._generate_firefox_ua(version)
            elif current_base == "Safari":
                version = random.randint(14, 17)
                ua = self._generate_safari_ua(version)
            else:  # Edge
                version = random.randint(90, 120)
                ua = self._generate_edge_ua(version)

            chain.append(ua)

            # Occasionally switch browser (10% chance)
            if random.random() < 0.1:
                current_base = random.choice(["Chrome", "Firefox", "Safari", "Edge"])

        return chain

    def analyze_user_agent_effectiveness(
        self, ua: str, success: bool, response_time: float, domain: str = None
    ) -> None:
        """
        Analyze user agent effectiveness for intelligent selection.

        Args:
            ua: User agent that was used
            success: Whether the request was successful
            response_time: Response time in seconds
            domain: Domain that was accessed
        """
        if not self.track_success_rates:
            return

        try:
            # Update overall UA performance
            if ua not in self.ua_performance:
                self.ua_performance[ua] = {
                    "total_requests": 0,
                    "successful_requests": 0,
                    "avg_response_time": 0.0,
                    "last_used": time.time(),
                }

            perf = self.ua_performance[ua]
            perf["total_requests"] += 1
            perf["last_used"] = time.time()

            if success:
                perf["successful_requests"] += 1

            # Update average response time
            current_avg = perf["avg_response_time"]
            total_requests = perf["total_requests"]
            perf["avg_response_time"] = (
                (current_avg * (total_requests - 1)) + response_time
            ) / total_requests

            # Update domain-specific performance
            if domain and self.domain_preferences:
                if domain not in self.domain_ua_preferences:
                    self.domain_ua_preferences[domain] = {}

                if ua not in self.domain_ua_preferences[domain]:
                    self.domain_ua_preferences[domain][ua] = {
                        "total_requests": 0,
                        "successful_requests": 0,
                        "success_rate": 0.0,
                        "avg_response_time": 0.0,
                    }

                domain_perf = self.domain_ua_preferences[domain][ua]
                domain_perf["total_requests"] += 1

                if success:
                    domain_perf["successful_requests"] += 1

                # Calculate success rate
                domain_perf["success_rate"] = (
                    domain_perf["successful_requests"] / domain_perf["total_requests"]
                )

                # Update average response time
                current_avg = domain_perf["avg_response_time"]
                total_requests = domain_perf["total_requests"]
                domain_perf["avg_response_time"] = (
                    (current_avg * (total_requests - 1)) + response_time
                ) / total_requests

        except Exception as e:
            logger.error(f"Error analyzing user agent effectiveness: {e}")

    def validate_user_agent(self, ua: str) -> bool:
        """
        Validate user agent format and realism.

        Args:
            ua: User agent string to validate

        Returns:
            True if valid, False otherwise
        """
        return self._validate_user_agent(ua)

    async def update_user_agent_pool(self) -> None:
        """Refresh user agent pool from multiple sources."""
        async with self.pool_refresh_lock:
            try:
                logger.info("Refreshing user agent pools...")

                # Clear existing pools
                self.browser_agents.clear()
                self.mobile_agents.clear()
                self.bot_agents.clear()

                # Load from fake-useragent
                if self.use_fake_useragent:
                    await self._load_fake_useragent_pool()

                # Load from custom lists
                if self.use_custom_lists:
                    self._load_custom_ua_lists()

                # Load from real browser data
                if self.use_real_browser_data:
                    self._load_real_browser_data()

                # Apply filtering
                self._apply_ua_filtering()

                # Ensure minimum pool sizes
                self._ensure_minimum_pool_sizes()

                self.pool_last_refresh = time.time()
                logger.info(
                    f"User agent pools refreshed: {len(self.browser_agents)} browser, "
                    f"{len(self.mobile_agents)} mobile, {len(self.bot_agents)} bot agents"
                )

            except Exception as e:
                logger.error(f"Error updating user agent pool: {e}")

    def get_statistics(self) -> Dict[str, Any]:
        """
        Get comprehensive user agent rotation statistics.

        Returns:
            Dictionary with detailed statistics
        """
        total_requests = sum(self.ua_usage_count.values()) if self.ua_usage_count else 0
        total_successful = (
            sum(perf["successful_requests"] for perf in self.ua_performance.values())
            if self.ua_performance
            else 0
        )

        success_rate = (
            (total_successful / total_requests * 100) if total_requests > 0 else 0
        )

        return {
            "enabled": self.enabled,
            "mandatory_rotation": self.mandatory_rotation,
            "rotation_strategy": self.rotation_strategy,
            "pool_sizes": {
                "browser_agents": len(self.browser_agents),
                "mobile_agents": len(self.mobile_agents),
                "bot_agents": len(self.bot_agents),
            },
            "usage_statistics": {
                "total_requests": total_requests,
                "successful_requests": total_successful,
                "success_rate_percent": round(success_rate, 2),
                "unique_user_agents_used": len(self.ua_usage_count),
                "domains_tracked": len(self.domain_ua_preferences),
            },
            "last_used_ua": (
                self.last_used_ua[:60] + "..." if self.last_used_ua else None
            ),
            "last_rotation_time": self.last_rotation_time,
            "pool_last_refresh": self.pool_last_refresh,
        }

    # Private helper methods

    async def _initialize_ua_pools(self) -> None:
        """Initialize user agent pools on startup."""
        await self.update_user_agent_pool()

    async def _refresh_pools_if_needed(self) -> None:
        """Check if pools need refreshing and refresh if necessary."""
        if time.time() - self.pool_last_refresh > (self.refresh_interval * 3600):
            await self.update_user_agent_pool()

    def _get_ua_pool(self, request_type: str) -> List[str]:
        """Get appropriate user agent pool for request type."""
        if request_type == "mobile":
            return self.mobile_agents
        elif request_type == "bot":
            return self.bot_agents
        else:  # browser
            return self.browser_agents

    async def _get_intelligent_user_agent(
        self, ua_pool: List[str], domain: str = None, force_new: bool = True
    ) -> str:
        """Get user agent using intelligent selection strategy."""
        if not ua_pool:
            return self._get_default_user_agent()

        # If we have domain preferences, use them
        if domain and self.domain_preferences and domain in self.domain_ua_preferences:
            domain_uas = list(self.domain_ua_preferences[domain].keys())
            available_uas = [ua for ua in domain_uas if ua in ua_pool]

            if available_uas:
                # Weight by success rate
                weights = []
                for ua in available_uas:
                    success_rate = self.domain_ua_preferences[domain][ua][
                        "success_rate"
                    ]
                    weights.append(max(success_rate, 0.1))  # Minimum weight

                if force_new and self.last_used_ua in available_uas:
                    # Remove last used UA from selection
                    idx = available_uas.index(self.last_used_ua)
                    available_uas.pop(idx)
                    weights.pop(idx)

                if available_uas:
                    return random.choices(available_uas, weights=weights)[0]

        # Fallback to performance-based selection
        return self._get_weighted_user_agent(ua_pool, domain, force_new)

    def _get_weighted_user_agent(
        self, ua_pool: List[str], domain: str = None, force_new: bool = True
    ) -> str:
        """Get user agent using weighted selection based on performance."""
        if not ua_pool:
            return self._get_default_user_agent()

        # Filter out last used if force_new
        available_uas = ua_pool.copy()
        if force_new and self.last_used_ua in available_uas:
            available_uas.remove(self.last_used_ua)

        if not available_uas:
            available_uas = ua_pool.copy()

        # Calculate weights based on performance
        weights = []
        for ua in available_uas:
            if ua in self.ua_performance:
                perf = self.ua_performance[ua]
                if perf["total_requests"] > 0:
                    success_rate = perf["successful_requests"] / perf["total_requests"]
                    # Factor in recency
                    recency_factor = min(
                        1.0, (time.time() - perf["last_used"]) / 3600
                    )  # Prefer recent UAs
                    weight = success_rate * (1 + recency_factor)
                    weights.append(max(weight, 0.1))
                else:
                    weights.append(0.5)  # Default weight for unused UAs
            else:
                weights.append(0.5)  # Default weight for new UAs

        return random.choices(available_uas, weights=weights)[0]

    def _get_random_user_agent(self, ua_pool: List[str], force_new: bool = True) -> str:
        """Get random user agent from pool."""
        if not ua_pool:
            return self._get_default_user_agent()

        available_uas = ua_pool.copy()
        if force_new and self.last_used_ua in available_uas:
            available_uas.remove(self.last_used_ua)

        if not available_uas:
            available_uas = ua_pool.copy()

        return random.choice(available_uas)

    def _get_sequential_user_agent(
        self, ua_pool: List[str], request_type: str, force_new: bool = True
    ) -> str:
        """Get next user agent in sequential order."""
        if not ua_pool:
            return self._get_default_user_agent()

        current_index = self.current_indices.get(request_type, 0)

        if force_new:
            current_index = (current_index + 1) % len(ua_pool)

        self.current_indices[request_type] = current_index
        return ua_pool[current_index]

    def _validate_user_agent(self, ua: str) -> bool:
        """Validate user agent string."""
        if not ua or len(ua) < 20 or len(ua) > 500:
            return False

        # Check for basic user agent structure
        if not any(
            browser in ua
            for browser in ["Chrome", "Firefox", "Safari", "Edge", "Opera"]
        ):
            return False

        # Check for suspicious patterns
        suspicious_patterns = ["bot", "crawler", "spider", "scraper", "automation"]

        if self.exclude_bots:
            for pattern in suspicious_patterns:
                if pattern.lower() in ua.lower():
                    return False

        return True

    def _track_ua_usage(self, ua: str, domain: str = None) -> None:
        """Track user agent usage statistics."""
        try:
            # Update usage count
            if ua not in self.ua_usage_count:
                self.ua_usage_count[ua] = 0
            self.ua_usage_count[ua] += 1

            # Update general stats
            current_time = time.time()
            if ua not in self.usage_stats:
                self.usage_stats[ua] = {
                    "first_used": current_time,
                    "last_used": current_time,
                    "usage_count": 0,
                }

            self.usage_stats[ua]["last_used"] = current_time
            self.usage_stats[ua]["usage_count"] += 1

        except Exception as e:
            logger.error(f"Error tracking UA usage: {e}")

    async def _load_fake_useragent_pool(self) -> None:
        """Load user agents from fake-useragent library."""
        try:
            ua = UserAgent()

            # Load browser user agents
            for _ in range(min(self.ua_pool_size, 50)):
                try:
                    browser_ua = ua.random
                    if self._validate_user_agent(browser_ua):
                        self.browser_agents.append(browser_ua)
                except (FakeUserAgentError, Exception):
                    continue

            # Load mobile user agents if not excluded
            if not self.exclude_mobile:
                for _ in range(min(self.ua_pool_size // 3, 20)):
                    try:
                        mobile_ua = ua.random
                        if "Mobile" in mobile_ua and self._validate_user_agent(
                            mobile_ua
                        ):
                            self.mobile_agents.append(mobile_ua)
                    except (FakeUserAgentError, Exception):
                        continue

        except Exception as e:
            logger.error(f"Error loading fake-useragent pool: {e}")

    def _load_custom_ua_lists(self) -> None:
        """Load user agents from custom lists."""
        # Add custom high-quality user agents
        custom_browser_uas = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/115.0",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
        ]

        for ua in custom_browser_uas:
            if self._validate_user_agent(ua):
                self.browser_agents.append(ua)

    def _load_real_browser_data(self) -> None:
        """Load user agents from real browser data sources."""
        # This could be enhanced to load from actual browser telemetry data
        # For now, we'll use realistic modern user agents
        real_browser_uas = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
        ]

        for ua in real_browser_uas:
            if self._validate_user_agent(ua):
                self.browser_agents.append(ua)

    def _apply_ua_filtering(self) -> None:
        """Apply filtering options to user agent pools."""
        try:
            # Filter by browser version
            if self.min_browser_version:
                self.browser_agents = [
                    ua
                    for ua in self.browser_agents
                    if self._extract_browser_version(ua) >= self.min_browser_version
                ]

            # Prefer Chrome if configured
            if self.prefer_chrome:
                chrome_uas = [ua for ua in self.browser_agents if "Chrome" in ua]
                if chrome_uas:
                    # Ensure Chrome UAs make up at least 60% of the pool
                    target_chrome_count = int(len(self.browser_agents) * 0.6)
                    while len(chrome_uas) < target_chrome_count and chrome_uas:
                        self.browser_agents.extend(
                            chrome_uas[: target_chrome_count - len(chrome_uas)]
                        )
                        chrome_uas = [
                            ua for ua in self.browser_agents if "Chrome" in ua
                        ]

        except Exception as e:
            logger.error(f"Error applying UA filtering: {e}")

    def _extract_browser_version(self, ua: str) -> int:
        """Extract browser version from user agent string."""
        try:
            # Chrome version extraction
            chrome_match = re.search(r"Chrome/(\d+)", ua)
            if chrome_match:
                return int(chrome_match.group(1))

            # Firefox version extraction
            firefox_match = re.search(r"Firefox/(\d+)", ua)
            if firefox_match:
                return int(firefox_match.group(1))

            # Safari version extraction
            safari_match = re.search(r"Version/(\d+)", ua)
            if safari_match and "Safari" in ua:
                return int(safari_match.group(1))

            return 0

        except Exception:
            return 0

    def _ensure_minimum_pool_sizes(self) -> None:
        """Ensure minimum pool sizes are maintained."""
        min_browser_size = max(10, self.ua_pool_size // 4)
        min_mobile_size = 5

        # Duplicate existing UAs if pools are too small
        while len(self.browser_agents) < min_browser_size and self.browser_agents:
            self.browser_agents.extend(
                self.browser_agents[: min_browser_size - len(self.browser_agents)]
            )

        if not self.exclude_mobile:
            while len(self.mobile_agents) < min_mobile_size and self.mobile_agents:
                self.mobile_agents.extend(
                    self.mobile_agents[: min_mobile_size - len(self.mobile_agents)]
                )

    def _generate_chrome_ua(self, version: int) -> str:
        """Generate realistic Chrome user agent."""
        os_versions = [
            "Windows NT 10.0; Win64; x64",
            "Macintosh; Intel Mac OS X 10_15_7",
            "X11; Linux x86_64",
        ]
        os_version = random.choice(os_versions)
        return f"Mozilla/5.0 ({os_version}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version}.0.0.0 Safari/537.36"

    def _generate_firefox_ua(self, version: int) -> str:
        """Generate realistic Firefox user agent."""
        os_versions = [
            "Windows NT 10.0; Win64; x64; rv:109.0",
            "Macintosh; Intel Mac OS X 10.15; rv:109.0",
            "X11; Linux x86_64; rv:109.0",
        ]
        os_version = random.choice(os_versions)
        return f"Mozilla/5.0 ({os_version}) Gecko/20100101 Firefox/{version}.0"

    def _generate_safari_ua(self, version: int) -> str:
        """Generate realistic Safari user agent."""
        return f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/{version}.0 Safari/605.1.15"

    def _generate_edge_ua(self, version: int) -> str:
        """Generate realistic Edge user agent."""
        return f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version}.0.0.0 Safari/537.36 Edg/{version}.0.0.0"

    def _get_default_user_agent(self) -> str:
        """Get default fallback user agent."""
        return "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
