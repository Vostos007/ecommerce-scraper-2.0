"""
Robots.txt compliance checker for ethical scraping.

This module provides production-grade robots.txt compliance with:
- URL permission checking (allow/disallow directives)
- Crawl delay enforcement and timing
- User-agent specific rules handling
- Sitemap discovery and integration
- Intelligent caching with TTL
- Compliance reporting and statistics
"""

import re
import time
import urllib.robotparser
from urllib.parse import urlparse
from typing import Dict, Any, List, Optional
import asyncio
import aiohttp
import logging

logger = logging.getLogger(__name__)


class RobotsTxtChecker:
    """
    Comprehensive robots.txt compliance checker for ethical scraping.

    Features:
    - URL permission checking with user-agent specificity
    - Crawl delay enforcement and timing
    - Automatic sitemap discovery
    - Intelligent caching with TTL
    - Compliance statistics and reporting
    - Flexible override options for testing
    """

    def __init__(self, config: Dict[str, Any]):
        """Initialize RobotsTxtChecker with configuration."""
        self.config = config
        self.enabled = config.get("enabled", True)
        self.respect_crawl_delay = config.get("respect_crawl_delay", True)
        self.respect_disallow = config.get("respect_disallow", True)
        self.default_user_agent = config.get("default_user_agent", "*")
        self.cache_ttl = config.get("cache_ttl_hours", 24)
        self.timeout = config.get("timeout_seconds", 10)

        # Crawl delay settings
        self.crawl_delay_settings = config.get("crawl_delay_settings", {})
        self.min_delay = self.crawl_delay_settings.get("min_delay_seconds", 1.0)
        self.max_delay = self.crawl_delay_settings.get("max_delay_seconds", 60.0)
        self.default_delay = self.crawl_delay_settings.get("default_delay_seconds", 1.0)
        self.respect_robots_delay = self.crawl_delay_settings.get(
            "respect_robots_delay", True
        )

        # Compliance overrides
        self.compliance_overrides = config.get("compliance_overrides", {})
        self.testing_mode = self.compliance_overrides.get("testing_mode", False)
        self.ignore_domains = set(
            self.compliance_overrides.get("ignore_for_domains", [])
        )
        self.force_allow_patterns = self.compliance_overrides.get(
            "force_allow_patterns", []
        )

        # Sitemap integration
        self.sitemap_integration = config.get("sitemap_integration", {})
        self.prefer_sitemap_urls = self.sitemap_integration.get(
            "prefer_sitemap_urls", True
        )
        self.auto_discover_sitemaps = self.sitemap_integration.get(
            "auto_discover_sitemaps", True
        )
        self.validate_sitemap_urls = self.sitemap_integration.get(
            "validate_sitemap_urls", True
        )

        # Robots.txt cache: domain -> (robots_txt_content, timestamp, parsed_rules)
        self.robots_cache = {}
        self.parsed_robots = {}  # domain -> urllib.robotparser.RobotFileParser

        # Sitemap cache: domain -> list of sitemap URLs
        self.sitemap_cache = {}

        # Last access times for crawl delay enforcement
        self.last_access_times = {}  # domain -> timestamp

        # Compliance tracking
        self.compliance_stats = {
            "total_checks": 0,
            "allowed_requests": 0,
            "blocked_requests": 0,
            "crawl_delays_applied": 0,
            "robots_txt_fetches": 0,
            "robots_txt_errors": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "sitemap_discoveries": 0,
        }

    async def check_url_allowed(
        self, url: str, user_agent: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Comprehensive URL permission checking with crawl delay calculation.

        Args:
            url: URL to check for permission
            user_agent: User-agent to check permissions for

        Returns:
            Dictionary with permission result and crawl delay info
        """
        if not self.enabled:
            return {
                "allowed": True,
                "crawl_delay": self.default_delay,
                "reason": "robots_txt_checking_disabled",
            }

        self.compliance_stats["total_checks"] += 1

        try:
            parsed_url = urlparse(url)
            domain = parsed_url.netloc.lower()

            # Check domain overrides
            if domain in self.ignore_domains:
                self.compliance_stats["allowed_requests"] += 1
                return {
                    "allowed": True,
                    "crawl_delay": self.default_delay,
                    "reason": "domain_in_ignore_list",
                }

            # Check force allow patterns
            for pattern in self.force_allow_patterns:
                if re.search(pattern, url):
                    self.compliance_stats["allowed_requests"] += 1
                    return {
                        "allowed": True,
                        "crawl_delay": self.default_delay,
                        "reason": f"force_allow_pattern_matched: {pattern}",
                    }

            # Get or fetch robots.txt
            robots_parser = await self._get_robots_parser(domain)
            if not robots_parser:
                # If we can't fetch robots.txt, default to allowed with default delay
                logger.warning(
                    f"Could not fetch robots.txt for {domain}, defaulting to allowed"
                )
                self.compliance_stats["allowed_requests"] += 1
                return {
                    "allowed": True,
                    "crawl_delay": self.default_delay,
                    "reason": "robots_txt_fetch_failed",
                }

            # Check URL permission
            ua = user_agent or self.default_user_agent
            allowed = robots_parser.can_fetch(ua, url)

            # Get crawl delay
            crawl_delay = await self._get_crawl_delay_for_domain(
                domain, ua, robots_parser
            )

            if allowed:
                self.compliance_stats["allowed_requests"] += 1
                reason = "allowed_by_robots_txt"
            else:
                self.compliance_stats["blocked_requests"] += 1
                reason = "disallowed_by_robots_txt"

                if self.testing_mode:
                    logger.warning(f"URL blocked by robots.txt (testing mode): {url}")
                    allowed = True
                    reason = "disallowed_but_testing_mode"

            return {
                "allowed": allowed,
                "crawl_delay": crawl_delay,
                "reason": reason,
                "user_agent_used": ua,
            }

        except Exception as e:
            logger.error(f"Error checking URL permission for {url}: {e}")
            # Default to allowed on error
            self.compliance_stats["allowed_requests"] += 1
            return {
                "allowed": True,
                "crawl_delay": self.default_delay,
                "reason": f"error_during_check: {str(e)}",
            }

    async def fetch_robots_txt(self, domain: str) -> Optional[str]:
        """
        Fetch and parse robots.txt for a domain.

        Args:
            domain: Domain to fetch robots.txt for

        Returns:
            Robots.txt content or None if failed
        """
        try:
            robots_url = f"https://{domain}/robots.txt"

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    robots_url,
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                    headers={"User-Agent": "robots.txt checker"},
                ) as response:
                    if response.status == 200:
                        content = await response.text()
                        logger.debug(f"Successfully fetched robots.txt for {domain}")
                        self.compliance_stats["robots_txt_fetches"] += 1
                        return content
                    else:
                        logger.debug(
                            f"robots.txt not found for {domain} (status: {response.status})"
                        )
                        return None

        except Exception as e:
            logger.error(f"Error fetching robots.txt for {domain}: {e}")
            self.compliance_stats["robots_txt_errors"] += 1
            return None

    async def get_crawl_delay(
        self, domain: str, user_agent: Optional[str] = None
    ) -> float:
        """
        Get required crawl delay for a domain and user-agent.

        Args:
            domain: Target domain
            user_agent: User-agent to check delay for

        Returns:
            Crawl delay in seconds
        """
        if not self.respect_crawl_delay:
            return self.default_delay

        try:
            robots_parser = await self._get_robots_parser(domain)
            if not robots_parser:
                return self.default_delay

            ua = user_agent or self.default_user_agent
            return await self._get_crawl_delay_for_domain(domain, ua, robots_parser)

        except Exception as e:
            logger.error(f"Error getting crawl delay for {domain}: {e}")
            return self.default_delay

    async def get_ethical_crawl_delay(
        self, domain: str, user_agent: Optional[str] = None
    ) -> float:
        """
        Get ethical crawl delay considering both robots.txt and last access time.

        Args:
            domain: Target domain
            user_agent: User-agent for delay calculation

        Returns:
            Recommended delay in seconds before next request
        """
        # Get base crawl delay from robots.txt
        base_delay = await self.get_crawl_delay(domain, user_agent)

        # Check time since last access
        current_time = time.time()
        last_access = self.last_access_times.get(domain)

        if last_access is None:
            return max(base_delay, 0.0)

        time_since_last = current_time - last_access

        # Calculate required delay
        if time_since_last >= base_delay:
            # Enough time has passed
            recommended_delay = 0.0
        else:
            # Need to wait longer
            recommended_delay = base_delay - time_since_last

        return max(0.0, recommended_delay)

    async def apply_crawl_delay(
        self, domain: str, user_agent: Optional[str] = None
    ) -> float:
        """
        Apply crawl delay for ethical scraping (actually wait).

        Args:
            domain: Target domain
            user_agent: User-agent for delay calculation

        Returns:
            Actual delay applied in seconds
        """
        if not self.respect_crawl_delay:
            return 0.0

        try:
            delay_needed = await self.get_ethical_crawl_delay(domain, user_agent)

            if delay_needed > 0:
                logger.debug(f"Applying crawl delay for {domain}: {delay_needed:.2f}s")
                await asyncio.sleep(delay_needed)
                self.compliance_stats["crawl_delays_applied"] += 1

            # Update last access time
            self.last_access_times[domain] = time.time()
            return delay_needed

        except Exception as e:
            logger.error(f"Error applying crawl delay for {domain}: {e}")
            return 0.0

    async def get_sitemap_urls(self, domain: str) -> List[str]:
        """
        Extract sitemap URLs from robots.txt.

        Args:
            domain: Domain to get sitemaps for

        Returns:
            List of sitemap URLs
        """
        if not self.auto_discover_sitemaps:
            return []

        # Check cache first
        if domain in self.sitemap_cache:
            cache_time, sitemaps = self.sitemap_cache[domain]
            if time.time() - cache_time < (self.cache_ttl * 3600):
                return sitemaps

        try:
            sitemaps = []
            robots_content = await self.fetch_robots_txt(domain)

            if robots_content:
                # Extract sitemap URLs
                sitemap_pattern = re.compile(
                    r"^sitemap:\s*(.+)$", re.IGNORECASE | re.MULTILINE
                )
                matches = sitemap_pattern.findall(robots_content)

                for match in matches:
                    sitemap_url = match.strip()
                    # Make absolute URL if needed
                    if sitemap_url.startswith("/"):
                        sitemap_url = f"https://{domain}{sitemap_url}"
                    elif not sitemap_url.startswith(("http://", "https://")):
                        sitemap_url = f"https://{domain}/{sitemap_url}"

                    sitemaps.append(sitemap_url)

            # Cache results
            self.sitemap_cache[domain] = (time.time(), sitemaps)

            if sitemaps:
                self.compliance_stats["sitemap_discoveries"] += 1
                logger.info(f"Discovered {len(sitemaps)} sitemaps for {domain}")

            return sitemaps

        except Exception as e:
            logger.error(f"Error getting sitemap URLs for {domain}: {e}")
            return []

    def parse_robots_txt(self, robots_content: str) -> Dict[str, Any]:
        """
        Parse robots.txt content into structured rules.

        Args:
            robots_content: Raw robots.txt content

        Returns:
            Structured representation of robots.txt rules
        """
        try:
            rules = {
                "user_agents": {},
                "sitemaps": [],
                "crawl_delays": {},
                "host": None,
                "raw_content": robots_content,
            }

            current_user_agent = None
            lines = robots_content.split("\n")

            for line in lines:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue

                # Parse directives
                if ":" in line:
                    directive, value = line.split(":", 1)
                    directive = directive.strip().lower()
                    value = value.strip()

                    if directive == "user-agent":
                        current_user_agent = value
                        if current_user_agent not in rules["user_agents"]:
                            rules["user_agents"][current_user_agent] = {
                                "allow": [],
                                "disallow": [],
                            }

                    elif directive == "allow" and current_user_agent:
                        rules["user_agents"][current_user_agent]["allow"].append(value)

                    elif directive == "disallow" and current_user_agent:
                        rules["user_agents"][current_user_agent]["disallow"].append(
                            value
                        )

                    elif directive == "crawl-delay" and current_user_agent:
                        try:
                            rules["crawl_delays"][current_user_agent] = float(value)
                        except ValueError:
                            logger.warning(f"Invalid crawl-delay value: {value}")

                    elif directive == "sitemap":
                        rules["sitemaps"].append(value)

                    elif directive == "host":
                        rules["host"] = value

            return rules

        except Exception as e:
            logger.error(f"Error parsing robots.txt: {e}")
            return {
                "user_agents": {},
                "sitemaps": [],
                "crawl_delays": {},
                "host": None,
                "raw_content": robots_content,
            }

    def get_compliance_statistics(self) -> Dict[str, Any]:
        """
        Get comprehensive robots.txt compliance statistics.

        Returns:
            Dictionary with detailed compliance statistics
        """
        total_checks = self.compliance_stats["total_checks"]
        if total_checks > 0:
            allowed_rate = (
                self.compliance_stats["allowed_requests"] / total_checks
            ) * 100
            blocked_rate = (
                self.compliance_stats["blocked_requests"] / total_checks
            ) * 100
        else:
            allowed_rate = blocked_rate = 0.0

        cache_total = (
            self.compliance_stats["cache_hits"] + self.compliance_stats["cache_misses"]
        )
        if cache_total > 0:
            cache_hit_rate = (self.compliance_stats["cache_hits"] / cache_total) * 100
        else:
            cache_hit_rate = 0.0

        return {
            "enabled": self.enabled,
            "respect_crawl_delay": self.respect_crawl_delay,
            "respect_disallow": self.respect_disallow,
            "total_checks": total_checks,
            "allowed_requests": self.compliance_stats["allowed_requests"],
            "blocked_requests": self.compliance_stats["blocked_requests"],
            "allowed_rate_percent": round(allowed_rate, 2),
            "blocked_rate_percent": round(blocked_rate, 2),
            "crawl_delays_applied": self.compliance_stats["crawl_delays_applied"],
            "robots_txt_fetches": self.compliance_stats["robots_txt_fetches"],
            "robots_txt_errors": self.compliance_stats["robots_txt_errors"],
            "cache_hits": self.compliance_stats["cache_hits"],
            "cache_misses": self.compliance_stats["cache_misses"],
            "cache_hit_rate_percent": round(cache_hit_rate, 2),
            "sitemap_discoveries": self.compliance_stats["sitemap_discoveries"],
            "domains_cached": len(self.robots_cache),
            "testing_mode": self.testing_mode,
        }

    # Private helper methods

    async def _get_robots_parser(
        self, domain: str
    ) -> Optional[urllib.robotparser.RobotFileParser]:
        """Get or create robots.txt parser for domain."""
        # Check cache first
        if domain in self.robots_cache:
            cache_time, robots_content, parser = self.robots_cache[domain]
            if time.time() - cache_time < (self.cache_ttl * 3600):
                self.compliance_stats["cache_hits"] += 1
                return parser

        self.compliance_stats["cache_misses"] += 1

        # Fetch new robots.txt
        robots_content = await self.fetch_robots_txt(domain)
        if not robots_content:
            return None

        try:
            # Create parser
            parser = urllib.robotparser.RobotFileParser()
            parser.parse(robots_content.splitlines())
            parser.set_url(f"https://{domain}/robots.txt")

            # Cache the result
            self.robots_cache[domain] = (time.time(), robots_content, parser)

            return parser

        except Exception as e:
            logger.error(f"Error creating robots parser for {domain}: {e}")
            return None

    async def _get_crawl_delay_for_domain(
        self,
        domain: str,
        user_agent: str,
        robots_parser: urllib.robotparser.RobotFileParser,
    ) -> float:
        """Get crawl delay for specific domain and user-agent."""
        try:
            # Get delay from robots parser
            delay = robots_parser.crawl_delay(user_agent)

            if delay is None:
                # Try with wildcard user-agent
                delay = robots_parser.crawl_delay("*")

            if delay is None:
                delay = self.default_delay
            else:
                # Ensure delay is within reasonable bounds
                delay = max(self.min_delay, min(delay, self.max_delay))

            return float(delay)

        except Exception as e:
            logger.error(f"Error getting crawl delay for {domain}: {e}")
            return self.default_delay

    def _is_cache_valid(self, domain: str) -> bool:
        """Check if cached robots.txt is still valid."""
        if domain not in self.robots_cache:
            return False

        cache_time, _, _ = self.robots_cache[domain]
        return time.time() - cache_time < (self.cache_ttl * 3600)

    def _clean_expired_cache(self) -> None:
        """Clean expired entries from cache."""
        try:
            current_time = time.time()
            cache_ttl_seconds = self.cache_ttl * 3600

            # Clean robots cache
            expired_domains = []
            for domain, (cache_time, _, _) in self.robots_cache.items():
                if current_time - cache_time > cache_ttl_seconds:
                    expired_domains.append(domain)

            for domain in expired_domains:
                del self.robots_cache[domain]
                if domain in self.parsed_robots:
                    del self.parsed_robots[domain]

            # Clean sitemap cache
            expired_sitemaps = []
            for domain, (cache_time, _) in self.sitemap_cache.items():
                if current_time - cache_time > cache_ttl_seconds:
                    expired_sitemaps.append(domain)

            for domain in expired_sitemaps:
                del self.sitemap_cache[domain]

            if expired_domains or expired_sitemaps:
                logger.debug(
                    f"Cleaned {len(expired_domains)} robots.txt and {len(expired_sitemaps)} sitemap cache entries"
                )

        except Exception as e:
            logger.error(f"Error cleaning expired cache: {e}")

    def clear_cache(self) -> None:
        """Clear all cached robots.txt data."""
        self.robots_cache.clear()
        self.parsed_robots.clear()
        self.sitemap_cache.clear()
        logger.info("Cleared all robots.txt cache data")
