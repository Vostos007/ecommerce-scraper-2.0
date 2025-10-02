"""
Premium proxy service integration with automatic management and monitoring.
Supports multiple premium proxy services with Proxy6.net as primary.
"""

import asyncio
import math
import os
import aiohttp
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from utils.logger import get_logger

logger = get_logger(__name__)

SERVICE_ENV_MAPPING = {
    "proxy6": "PROXY6_API_KEY",
    "proxy_seller": "PROXY_SELLER_API_KEY",
}


@dataclass
class ProxyInfo:
    """Information about a premium proxy."""

    proxy_url: str
    host: str
    port: int
    username: str
    password: str
    protocol: str  # http, https, socks5
    country: str
    region: Optional[str] = None
    city: Optional[str] = None
    isp: Optional[str] = None
    expires_at: Optional[datetime] = None
    created_at: datetime = field(default_factory=datetime.now)
    is_active: bool = True
    usage_count: int = 0
    last_used: Optional[datetime] = None
    response_time_avg: float = 0.0
    success_rate: float = 1.0
    cost_per_gb: Optional[float] = None
    monthly_traffic_limit: Optional[float] = None
    used_traffic: float = 0.0

    @property
    def formatted_url(self) -> str:
        """Get formatted proxy URL."""
        return (
            f"{self.protocol}://{self.username}:{self.password}@{self.host}:{self.port}"
        )

    @property
    def is_expired(self) -> bool:
        """Check if proxy has expired."""
        if not self.expires_at:
            return False
        return datetime.now() > self.expires_at

    @property
    def traffic_usage_percentage(self) -> float:
        """Calculate traffic usage percentage."""
        if not self.monthly_traffic_limit:
            return 0.0
        return (self.used_traffic / self.monthly_traffic_limit) * 100

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "proxy_url": self.proxy_url,
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "password": self.password,
            "protocol": self.protocol,
            "country": self.country,
            "region": self.region,
            "city": self.city,
            "isp": self.isp,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "created_at": self.created_at.isoformat(),
            "is_active": self.is_active,
            "usage_count": self.usage_count,
            "last_used": self.last_used.isoformat() if self.last_used else None,
            "response_time_avg": self.response_time_avg,
            "success_rate": self.success_rate,
            "cost_per_gb": self.cost_per_gb,
            "monthly_traffic_limit": self.monthly_traffic_limit,
            "used_traffic": self.used_traffic,
        }


class PremiumProxyManager:
    """Premium proxy service manager with automatic management."""

    def __init__(self, config: Dict):
        self.config = config
        self.enabled = config.get("enabled", False)
        self.primary_service = config.get("primary_service", "proxy6")

        # Proxy6.net configuration
        self.proxy6_config = config.get("proxy6", {})
        self.api_key = self._resolve_api_key(
            self.proxy6_config, SERVICE_ENV_MAPPING["proxy6"]
        )
        self.api_url = self.proxy6_config.get("api_url", "https://proxy6.net/api")
        self.proxy_type = self.proxy6_config.get("proxy_type", "http")
        self.country = self.proxy6_config.get("country", "RU")
        self.auto_refresh = self.proxy6_config.get("auto_refresh", True)
        self.refresh_interval = self.proxy6_config.get("refresh_interval_seconds", 3600)
        self.min_proxy_count = self.proxy6_config.get("min_proxy_count", 10)

        # Backup services configuration
        self.backup_services = config.get("backup_services", {})
        if isinstance(self.backup_services, dict):
            for service_name, service_config in self.backup_services.items():
                if not isinstance(service_config, dict):
                    continue
                env_name = SERVICE_ENV_MAPPING.get(
                    service_name, f"{service_name.upper()}_API_KEY"
                )
                service_config["api_key"] = self._resolve_api_key(service_config, env_name)
                if service_config.get("enabled") and not service_config.get("api_key"):
                    logger.warning(
                        "Backup premium proxy service '%s' enabled but missing API key",
                        service_name,
                    )

        # Cost management
        cost_config = config.get("cost_management", {})
        self.max_monthly_cost = cost_config.get("max_monthly_cost", 100.0)
        self.track_usage = cost_config.get("track_usage", True)
        self.auto_scale = cost_config.get("auto_scale", True)

        auto_purchase_config = config.get("auto_purchase", {})
        self.auto_purchase_enabled = auto_purchase_config.get("enabled", False)
        self.max_purchase_batch_size = auto_purchase_config.get(
            "max_batch_size", 10
        )
        self.purchase_cooldown_minutes = auto_purchase_config.get(
            "cooldown_minutes", 30
        )
        self.cost_per_proxy = float(auto_purchase_config.get("cost_per_proxy", 2.0))
        self.last_purchase_time: Optional[datetime] = None

        # Internal state
        self.active_proxies: Dict[str, ProxyInfo] = {}
        self.proxy_pool: List[ProxyInfo] = []
        self.last_refresh: Optional[datetime] = None
        self.total_monthly_cost: float = 0.0
        self.total_traffic_used: float = 0.0

        # Session for API calls
        self.session: Optional[aiohttp.ClientSession] = None

        # Auto-refresh task
        self._refresh_task: Optional[asyncio.Task] = None

        if self.enabled and self.api_key:
            logger.info(f"PremiumProxyManager initialized for {self.primary_service}")
        else:
            logger.info("PremiumProxyManager disabled")

    async def start_auto_refresh(self) -> None:
        """Start auto-refresh if enabled and in async context."""
        if self.enabled and self.api_key and self.auto_refresh:
            self._start_auto_refresh()

    async def __aenter__(self):
        """Async context manager entry."""
        await self._ensure_session()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self._close_session()

    async def _ensure_session(self):
        """Ensure aiohttp session exists."""
        if self.session is None:
            timeout = aiohttp.ClientTimeout(total=30)
            self.session = aiohttp.ClientSession(timeout=timeout)
            return

        closed_attr = getattr(self.session, "closed", False)
        is_closed = False

        if isinstance(closed_attr, bool):
            is_closed = closed_attr
        elif isinstance(closed_attr, (int, float)):
            is_closed = bool(closed_attr)
        else:
            is_closed = False

        if is_closed:
            timeout = aiohttp.ClientTimeout(total=30)
            self.session = aiohttp.ClientSession(timeout=timeout)

    async def _close_session(self):
        """Close aiohttp session."""
        if self.session and not self.session.closed:
            await self.session.close()

    def _start_auto_refresh(self):
        """Start automatic proxy pool refresh."""
        if self._refresh_task:
            self._refresh_task.cancel()

        try:
            # Only create task if we have a running event loop
            asyncio.get_running_loop()
            self._refresh_task = asyncio.create_task(self._auto_refresh_loop())
            logger.info("Premium proxy auto-refresh started")
        except RuntimeError:
            logger.warning(
                "No event loop running, premium proxy auto-refresh will start when needed"
            )

    async def _auto_refresh_loop(self):
        """Automatic proxy refresh loop."""
        while True:
            try:
                await asyncio.sleep(self.refresh_interval)
                if self.enabled:
                    await self.refresh_proxy_pool()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in auto-refresh loop: {e}")

    def _resolve_api_key(self, service_config: Optional[Dict[str, Any]], env_name: str) -> str:
        api_key = ""
        if isinstance(service_config, dict):
            api_key = service_config.get("api_key") or ""

        if api_key:
            return api_key

        env_value = os.getenv(env_name, "")
        if env_value:
            return env_value

        logger.debug("API key for %s not provided; proceeding without credentials", env_name)
        return ""

    async def _request_json(
        self, url: str, *, params: Optional[Dict[str, Any]] = None
    ) -> Optional[tuple[int, Any]]:
        """Execute GET request and return (status, json) pair.

        Handles both real aiohttp.ClientSession instances and AsyncMock stubs used in tests.
        """

        await self._ensure_session()

        if not self.session:
            return None

        response_ctx = self.session.get(url, params=params)
        response_obj = None

        try:
            if hasattr(response_ctx, "__aenter__"):
                async with response_ctx as response:  # type: ignore[misc]
                    response_obj = response
                    return response.status, await response.json()

            response_obj = await response_ctx
            if hasattr(response_obj, "__aenter__"):
                async with response_obj as nested_response:  # type: ignore[misc]
                    response_obj = nested_response
                    return nested_response.status, await nested_response.json()

            status = getattr(response_obj, "status", None)
            json_method = getattr(response_obj, "json", None)
            payload = await json_method() if callable(json_method) else None
            return status, payload

        finally:
            if response_obj and not hasattr(response_ctx, "__aenter__"):
                release = getattr(response_obj, "release", None)
                if callable(release):
                    await release()

    async def fetch_proxy_list(self) -> List[ProxyInfo]:
        """
        Fetch active proxies from Proxy6.net API.

        Returns:
            List of ProxyInfo objects
        """
        if not self.enabled or not self.api_key:
            logger.warning("Premium proxy manager not properly configured")
            return []

        try:
            # Proxy6.net API endpoint for getting proxy list
            url = f"{self.api_url}/{self.api_key}/getproxy"
            params = {"state": "active", "descr": "yes"}

            response = await self._request_json(url, params=params)
            if response is None:
                logger.error("Failed to obtain HTTP session for premium proxy fetch")
                return []

            status, data = response

            if status != 200:
                logger.error(f"Proxy6 API error: {status}")
                return []

            if not isinstance(data, dict):
                logger.error("Proxy6 API returned unexpected payload")
                return []

            if data.get("status") == "error":
                logger.error(f"Proxy6 API error: {data.get('error')}")
                return []

            proxies = []
            proxy_list = data.get("list", {})

            for proxy_id, proxy_data in proxy_list.items():
                try:
                    proxy_info = self._parse_proxy6_data(proxy_id, proxy_data)
                    if proxy_info:
                        proxies.append(proxy_info)
                except Exception as e:
                    logger.warning(f"Error parsing proxy data for {proxy_id}: {e}")

            logger.info(f"Fetched {len(proxies)} proxies from Proxy6.net")
            return proxies

        except Exception as e:
            logger.error(f"Error fetching proxy list from Proxy6.net: {e}")
            return []

    async def refresh_proxy_pool(self) -> bool:
        """
        Refresh proxy pool from premium service.

        Returns:
            True if refresh was successful
        """
        if not self.enabled:
            return False

        try:
            logger.info("Refreshing proxy pool from premium service")

            # Fetch new proxy list
            new_proxies = await self.fetch_proxy_list()

            if not new_proxies:
                logger.warning("No proxies returned from premium service")
                return False

            # Update proxy pool
            old_count = len(self.proxy_pool)
            self.proxy_pool = new_proxies

            # Update active proxies mapping
            self.active_proxies.clear()
            for proxy in self.proxy_pool:
                if proxy.is_active and not proxy.is_expired:
                    self.active_proxies[proxy.proxy_url] = proxy

            self.last_refresh = datetime.now()

            logger.info(
                f"Proxy pool refreshed: {old_count} → {len(self.proxy_pool)} proxies"
            )
            return True

        except Exception as e:
            logger.error(f"Error refreshing proxy pool: {e}")
            return False

    async def validate_proxy_credentials(self) -> bool:
        """
        Validate proxy service credentials.

        Returns:
            True if credentials are valid
        """
        if not self.enabled or not self.api_key:
            return False

        try:
            # Test API key with account info endpoint
            url = f"{self.api_url}/{self.api_key}/getbalance"

            response = await self._request_json(url)
            if response is None:
                logger.error(
                    "Failed to obtain HTTP session for credential validation"
                )
                return False

            status, data = response

            if status != 200:
                logger.error(f"Credential validation failed: HTTP {status}")
                return False

            if not isinstance(data, dict):
                logger.error("Credential validation failed: unexpected payload")
                return False

            if data.get("status") == "error":
                error_msg = data.get("error", "Unknown error")
                logger.error(f"Credential validation failed: {error_msg}")
                return False

            # Log account info
            balance = data.get("balance", "Unknown")
            currency = data.get("currency", "Unknown")
            logger.info(
                f"Proxy6.net credentials valid. Balance: {balance} {currency}"
            )

            return True

        except Exception as e:
            logger.error(f"Error validating credentials: {e}")
            return False

    async def monitor_proxy_usage(self) -> Dict[str, Any]:
        """
        Monitor proxy usage and costs.

        Returns:
            Dictionary with usage statistics
        """
        if not self.enabled:
            return {"error": "Premium proxy manager not enabled"}

        try:
            # Calculate usage statistics
            active_count = len(
                [p for p in self.proxy_pool if p.is_active and not p.is_expired]
            )
            expired_count = len([p for p in self.proxy_pool if p.is_expired])
            total_usage = sum(p.usage_count for p in self.proxy_pool)
            avg_response_time = 0.0

            response_times = [
                p.response_time_avg for p in self.proxy_pool if p.response_time_avg > 0
            ]
            if response_times:
                avg_response_time = sum(response_times) / len(response_times)

            # Calculate success rates
            success_rates = [
                p.success_rate for p in self.proxy_pool if p.usage_count > 0
            ]
            avg_success_rate = (
                sum(success_rates) / len(success_rates) if success_rates else 0.0
            )

            # Traffic statistics
            total_traffic = sum(p.used_traffic for p in self.proxy_pool)
            traffic_costs = sum(
                (p.used_traffic * p.cost_per_gb) if p.cost_per_gb else 0.0
                for p in self.proxy_pool
            )

            usage_stats = {
                "total_proxies": len(self.proxy_pool),
                "active_proxies": active_count,
                "expired_proxies": expired_count,
                "total_usage_count": total_usage,
                "avg_response_time": avg_response_time,
                "avg_success_rate": avg_success_rate,
                "total_traffic_gb": total_traffic,
                "estimated_traffic_cost": traffic_costs,
                "monthly_cost_used": self.total_monthly_cost,
                "monthly_budget_remaining": max(
                    0, self.max_monthly_cost - self.total_monthly_cost
                ),
                "last_refresh": (
                    self.last_refresh.isoformat() if self.last_refresh else None
                ),
                "proxy_countries": self._get_country_distribution(),
                "proxy_protocols": self._get_protocol_distribution(),
            }

            if self.auto_purchase_enabled:
                cooldown_remaining = 0
                if self.last_purchase_time:
                    elapsed = datetime.now() - self.last_purchase_time
                    remaining = (
                        self.purchase_cooldown_minutes
                        - (elapsed.total_seconds() / 60)
                    )
                    cooldown_remaining = max(0, math.ceil(remaining))

                usage_stats.update(
                    {
                        "auto_purchase_enabled": True,
                        "last_purchase_time": self.last_purchase_time.isoformat()
                        if self.last_purchase_time
                        else None,
                        "purchase_cooldown_remaining": cooldown_remaining,
                        "max_purchase_batch_size": self.max_purchase_batch_size,
                        "cost_per_proxy": self.cost_per_proxy,
                    }
                )

            # Add warnings
            warnings = []
            if self.total_monthly_cost > self.max_monthly_cost * 0.8:
                warnings.append("Monthly cost approaching budget limit")
            if active_count < self.min_proxy_count:
                warnings.append(
                    f"Active proxy count below minimum ({active_count}/{self.min_proxy_count})"
                )
            if avg_success_rate < 0.8:
                warnings.append(f"Average success rate low ({avg_success_rate:.1%})")

            usage_stats["warnings"] = warnings

            return usage_stats

        except Exception as e:
            logger.error(f"Error monitoring proxy usage: {e}")
            return {"error": str(e)}

    def can_purchase_proxies(self, count: int) -> bool:
        """Check whether purchasing additional proxies is allowed."""

        if not self.auto_purchase_enabled or count <= 0:
            return False

        now = datetime.now()
        if self.last_purchase_time and (
            now - self.last_purchase_time
        ) < timedelta(minutes=self.purchase_cooldown_minutes):
            return False

        estimated_cost = count * self.cost_per_proxy
        if self.total_monthly_cost + estimated_cost > self.max_monthly_cost:
            return False

        return True

    def get_purchase_recommendations(self, deficit: int) -> Dict[str, Any]:
        """Return recommendation for purchasing proxies given deficit."""

        budget_remaining = max(
            0.0, self.max_monthly_cost - self.total_monthly_cost
        )
        cooldown_remaining = 0
        if self.last_purchase_time:
            elapsed = datetime.now() - self.last_purchase_time
            remaining = (
                self.purchase_cooldown_minutes - (elapsed.total_seconds() / 60)
            )
            cooldown_remaining = max(0, math.ceil(remaining))

        if deficit <= 0:
            return {
                "can_purchase": False,
                "recommended_count": 0,
                "estimated_cost": 0.0,
                "budget_remaining": budget_remaining,
                "cooldown_remaining_minutes": cooldown_remaining,
            }

        if self.cost_per_proxy <= 0:
            max_by_budget = deficit
        else:
            max_by_budget = math.floor(budget_remaining / self.cost_per_proxy)

        recommended = max(
            0,
            min(deficit, self.max_purchase_batch_size, max_by_budget),
        )

        can_purchase = (
            self.auto_purchase_enabled
            and recommended > 0
            and cooldown_remaining == 0
            and self.can_purchase_proxies(recommended)
        )

        estimated_cost = recommended * self.cost_per_proxy

        return {
            "can_purchase": can_purchase,
            "recommended_count": recommended,
            "estimated_cost": estimated_cost,
            "budget_remaining": budget_remaining,
            "cooldown_remaining_minutes": cooldown_remaining,
        }

    async def ensure_min_proxy_pool(self, target_count: int) -> Dict[str, Any]:
        """Ensure that at least target_count proxies are available."""

        active_count = len(
            [p for p in self.proxy_pool if p.is_active and not p.is_expired]
        )
        result: Dict[str, Any] = {
            "target_count": target_count,
            "current_count": active_count,
            "purchased": 0,
            "cost": 0.0,
            "success": False,
            "message": "",
        }

        if target_count <= 0:
            result.update({"success": True, "message": "No target specified"})
            return result

        if not self.auto_purchase_enabled:
            result["message"] = "Auto purchase disabled"
            return result

        deficit = max(0, target_count - active_count)
        if deficit <= 0:
            result.update({"success": True, "message": "Proxy pool sufficient"})
            return result

        recommendations = self.get_purchase_recommendations(deficit)
        recommended = int(recommendations.get("recommended_count", 0))
        if recommended <= 0 or not recommendations.get("can_purchase", False):
            result["message"] = "Purchase conditions not met"
            return result

        if not self.can_purchase_proxies(recommended):
            result["message"] = "Budget or cooldown prevents purchase"
            return result

        purchase_success = await self.purchase_additional_proxies(recommended)
        if not purchase_success:
            result["message"] = "Provider purchase failed"
            return result

        cost = recommended * self.cost_per_proxy
        self.total_monthly_cost += cost
        self.last_purchase_time = datetime.now()
        result.update(
            {
                "purchased": recommended,
                "cost": cost,
                "success": True,
                "message": "Proxies purchased successfully",
            }
        )
        return result

    def get_best_proxies(
        self,
        count: int = 5,
        country: Optional[str] = None,
        protocol: Optional[str] = None,
    ) -> List[ProxyInfo]:
        """
        Get best performing proxies based on criteria.

        Args:
            count: Number of proxies to return
            country: Filter by country code
            protocol: Filter by protocol type

        Returns:
            List of best ProxyInfo objects
        """
        available_proxies = [
            p for p in self.proxy_pool if p.is_active and not p.is_expired
        ]

        # Apply filters
        if country:
            available_proxies = [
                p for p in available_proxies if p.country.upper() == country.upper()
            ]

        if protocol:
            available_proxies = [
                p for p in available_proxies if p.protocol.lower() == protocol.lower()
            ]

        if not available_proxies:
            return []

        # Sort by performance score
        def calculate_score(proxy: ProxyInfo) -> float:
            # Combine success rate, response time, and usage
            success_score = proxy.success_rate
            response_score = max(
                0, 1.0 - (proxy.response_time_avg / 10.0)
            )  # 10s = 0 score
            usage_score = max(
                0, 1.0 - (proxy.usage_count / 1000.0)
            )  # 1000 uses = 0 score

            return (success_score * 0.5) + (response_score * 0.3) + (usage_score * 0.2)

        available_proxies.sort(key=calculate_score, reverse=True)
        return available_proxies[:count]

    def mark_proxy_used(
        self,
        proxy_url: str,
        response_time: float,
        success: bool,
        traffic_used: float = 0.0,
    ) -> None:
        """
        Mark proxy as used and update statistics.

        Args:
            proxy_url: Proxy URL that was used
            response_time: Response time in seconds
            success: Whether the request was successful
            traffic_used: Traffic used in MB
        """
        if proxy_url in self.active_proxies:
            proxy = self.active_proxies[proxy_url]

            proxy.usage_count += 1
            proxy.last_used = datetime.now()
            proxy.used_traffic += traffic_used

            # Update response time (moving average)
            if proxy.response_time_avg == 0:
                proxy.response_time_avg = response_time
            else:
                proxy.response_time_avg = (proxy.response_time_avg * 0.9) + (
                    response_time * 0.1
                )

            # Update success rate (moving average)
            if proxy.usage_count == 1:
                proxy.success_rate = 1.0 if success else 0.0
            else:
                current_successes = proxy.success_rate * (proxy.usage_count - 1)
                if success:
                    current_successes += 1
                proxy.success_rate = current_successes / proxy.usage_count

            # Update traffic costs
            if proxy.cost_per_gb and traffic_used > 0:
                cost = (traffic_used / 1024) * proxy.cost_per_gb  # Convert MB to GB
                self.total_monthly_cost += cost

    async def purchase_additional_proxies(
        self, count: int, country: Optional[str] = None, period_days: int = 30
    ) -> bool:
        """
        Purchase additional proxies from service.

        Args:
            count: Number of proxies to purchase
            country: Country code for proxies
            period_days: Subscription period in days

        Returns:
            True if purchase was successful
        """
        if not self.enabled or not self.api_key:
            logger.error("Cannot purchase proxies: service not configured")
            return False

        try:
            await self._ensure_session()

            # Calculate cost estimate
            estimated_cost = (
                count * 2.0 * (period_days / 30)
            )  # $2 per proxy per month estimate

            if self.total_monthly_cost + estimated_cost > self.max_monthly_cost:
                logger.warning(
                    f"Purchase would exceed monthly budget: {estimated_cost}"
                )
                return False

            # Proxy6.net API for purchasing proxies
            url = f"{self.api_url}/{self.api_key}/buy"
            params = {
                "count": count,
                "period": period_days,
                "country": country or self.country,
                "type": self.proxy_type,
            }

            async with self.session.get(url, params=params) as response:
                if response.status != 200:
                    logger.error(f"Purchase failed: HTTP {response.status}")
                    return False

                data = await response.json()

                if data.get("status") == "error":
                    error_msg = data.get("error", "Unknown error")
                    logger.error(f"Purchase failed: {error_msg}")
                    return False

                # Purchase successful
                purchase_id = data.get("id")
                logger.info(
                    f"Successfully purchased {count} proxies (ID: {purchase_id})"
                )

                # Refresh proxy pool to get new proxies
                await asyncio.sleep(5)  # Wait for provisioning
                await self.refresh_proxy_pool()

                return True

        except Exception as e:
            logger.error(f"Error purchasing additional proxies: {e}")
            return False

    def _parse_proxy6_data(
        self, proxy_id: str, proxy_data: Dict
    ) -> Optional[ProxyInfo]:
        """Parse Proxy6.net API response data into ProxyInfo object."""
        try:
            host = proxy_data.get("host")
            port = proxy_data.get("port")
            username = proxy_data.get("user")
            password = proxy_data.get("pass")
            protocol = proxy_data.get("type", "http")
            country = proxy_data.get("country", "Unknown")

            if not all([host, port, username, password]):
                logger.warning(f"Incomplete proxy data for {proxy_id}")
                return None

            # Parse expiration date
            expires_at = None
            if proxy_data.get("date_end"):
                try:
                    expires_at = datetime.strptime(
                        proxy_data["date_end"], "%Y-%m-%d %H:%M:%S"
                    )
                except ValueError:
                    logger.warning(f"Invalid expiration date for proxy {proxy_id}")

            proxy_url = f"{protocol}://{username}:{password}@{host}:{port}"

            return ProxyInfo(
                proxy_url=proxy_url,
                host=host,
                port=int(port),
                username=username,
                password=password,
                protocol=protocol,
                country=country,
                region=proxy_data.get("region"),
                city=proxy_data.get("city"),
                isp=proxy_data.get("isp"),
                expires_at=expires_at,
                is_active=proxy_data.get("active", True),
                cost_per_gb=2.0,  # Approximate cost per GB
                monthly_traffic_limit=100.0,  # 100GB limit estimate
            )

        except Exception as e:
            logger.error(f"Error parsing proxy data for {proxy_id}: {e}")
            return None

    def _get_country_distribution(self) -> Dict[str, int]:
        """Get distribution of proxies by country."""
        distribution = {}
        for proxy in self.proxy_pool:
            country = proxy.country or "Unknown"
            distribution[country] = distribution.get(country, 0) + 1
        return distribution

    def _get_protocol_distribution(self) -> Dict[str, int]:
        """Get distribution of proxies by protocol."""
        distribution = {}
        for proxy in self.proxy_pool:
            protocol = proxy.protocol or "Unknown"
            distribution[protocol] = distribution.get(protocol, 0) + 1
        return distribution

    async def get_account_info(self) -> Dict[str, Any]:
        """
        Get account information from proxy service.

        Returns:
            Account information dictionary
        """
        if not self.enabled or not self.api_key:
            return {"error": "Service not configured"}

        try:
            await self._ensure_session()

            url = f"{self.api_url}/{self.api_key}/getbalance"

            async with self.session.get(url) as response:
                if response.status != 200:
                    return {"error": f"HTTP {response.status}"}

                data = await response.json()

                if data.get("status") == "error":
                    return {"error": data.get("error")}

                return {
                    "balance": data.get("balance"),
                    "currency": data.get("currency"),
                    "user_id": data.get("user_id"),
                    "service": "proxy6.net",
                }

        except Exception as e:
            logger.error(f"Error getting account info: {e}")
            return {"error": str(e)}

    async def cleanup_expired_proxies(self) -> int:
        """
        Remove expired proxies from pool.

        Returns:
            Number of proxies removed
        """
        initial_count = len(self.proxy_pool)

        # Remove expired proxies
        self.proxy_pool = [p for p in self.proxy_pool if not p.is_expired]

        # Update active proxies mapping
        expired_urls = []
        for url, proxy in self.active_proxies.items():
            if proxy.is_expired:
                expired_urls.append(url)

        for url in expired_urls:
            del self.active_proxies[url]

        removed_count = initial_count - len(self.proxy_pool)

        if removed_count > 0:
            logger.info(f"Cleaned up {removed_count} expired proxies")

        return removed_count

    def get_proxy_by_url(self, proxy_url: str) -> Optional[ProxyInfo]:
        """Get proxy info by URL."""
        return self.active_proxies.get(proxy_url)

    def get_active_proxy_count(self) -> int:
        """Get count of active, non-expired proxies."""
        return len([p for p in self.proxy_pool if p.is_active and not p.is_expired])

    async def close(self):
        """Cleanup resources."""
        if self._refresh_task:
            self._refresh_task.cancel()

        await self._close_session()

        logger.info("PremiumProxyManager closed")
