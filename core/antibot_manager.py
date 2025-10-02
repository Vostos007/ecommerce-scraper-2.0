from __future__ import annotations

import json
import time
import random
import asyncio
import uuid
import logging
import atexit
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from collections import deque
from datetime import datetime, timedelta
from importlib import import_module
from pathlib import Path
from typing import Optional, Dict, List, Any, TypedDict, cast, TYPE_CHECKING, Callable, TypeVar

import aiohttp
from fake_useragent import UserAgent
from core.proxy_rotator import ProxyRotator
from core.proxy_health_checker import ProxyHealthChecker
from core.session_manager import SessionManager
from core.content_validator import ContentValidator
from core.premium_proxy_manager import PremiumProxyManager
from core.exponential_backoff import ExponentialBackoff
from core.captcha_solver import TwoCaptchaManager
from core.user_agent_rotator import UserAgentRotator
from core.robots_checker import RobotsTxtChecker
from core.async_playwright_manager import AsyncPlaywrightManager
from core.antibot_logger import AntiBotLogger
from core.flaresolverr_client import FlareSolverrClient, FlareSolverrError
from utils.logger import get_logger
from core.base_component import ConfigurableComponent
from utils.helpers import looks_like_guard_html

if TYPE_CHECKING:  # pragma: no cover - typing only
    from playwright.sync_api import Browser, BrowserContext, Page
    from playwright.async_api import (
        BrowserContext as AsyncBrowserContext,
        Page as AsyncPage,
    )

_CURL_CFFI_REQUESTS: Any | None = None
_PLAYWRIGHT_SYNC_API: Any | None = None

T = TypeVar("T")


def _get_curl_cffi_requests():
    """Lazy-load curl_cffi only for flows that actually need raw HTTP bypass."""

    global _CURL_CFFI_REQUESTS
    if _CURL_CFFI_REQUESTS is None:
        # Import hotspot (ARCH-009) — defer until antibot HTTP fallback triggers.
        _CURL_CFFI_REQUESTS = import_module("curl_cffi.requests")
    return _CURL_CFFI_REQUESTS


def _get_playwright_sync_api():
    """Lazy-load Playwright sync API to trim cold start before browser usage."""

    global _PLAYWRIGHT_SYNC_API
    if _PLAYWRIGHT_SYNC_API is None:
        _PLAYWRIGHT_SYNC_API = import_module("playwright.sync_api")
    return _PLAYWRIGHT_SYNC_API


def sync_playwright():
    """Provide a patchable shim mirroring playwright.sync_playwright."""

    return _get_playwright_sync_api().sync_playwright()


# Type definitions for configuration structures
class PlaywrightOptions(TypedDict, total=False):
    headless: bool
    slow_mo: int
    debug_mode: bool
    devtools: bool
    args: List[str]


class Config(TypedDict, total=False):
    proxies: List[str]
    playwright_options: PlaywrightOptions
    debug_headless: bool
    timeout: int
    delay_range: List[float]


# Custom exception classes
class BrowserLaunchError(Exception):
    """Raised when browser launch fails"""

    pass


class ProxyConnectionError(Exception):
    """Raised when proxy connection fails"""

    pass


class PageNavigationError(Exception):
    """Raised when page navigation fails"""

    pass


@dataclass
class CircuitBreakerState:
    """Tracks circuit breaker state for a domain"""
    consecutive_failures: int = 0
    recent_results: deque = field(default_factory=lambda: deque(maxlen=50))
    is_open: bool = False
    half_open: bool = False
    opened_at: Optional[datetime] = None
    half_open_attempts: int = 0


class AntibotManager(ConfigurableComponent):
    def __init__(self, config_path: str) -> None:
        config_data: Dict[str, Any] = {}
        config_file = Path(config_path)
        if config_file.is_file():
            config_data = self.load_config(config_path)
        super().__init__(config=config_data)
        self.config_path = config_path
        self.logger: logging.Logger = get_logger(__name__)  # Keep specialized logger
        self.browser: Optional[Browser] = None
        self.playwright = None
        self.proxy_rotator: Optional[ProxyRotator] = None
        self.ua_generator: UserAgent = UserAgent()
        self.playwright_options: PlaywrightOptions = {}
        self.config = cast(Config, self.config)
        self.current_ua_index: int = 0
        self.current_proxy_index: int = 0
        self._playwright_executor: Optional[ThreadPoolExecutor] = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="playwright-sync",
        )
        atexit.register(self._shutdown_playwright_resources)

        # Initialize new proxy infrastructure components
        self.proxy_health_checker: Optional[ProxyHealthChecker] = None
        self.session_manager: Optional[SessionManager] = None
        self.content_validator: Optional[ContentValidator] = None
        self.premium_proxy_manager: Optional[PremiumProxyManager] = None
        self.backoff: Optional[ExponentialBackoff] = None

        # Initialize new anti-bot components
        self.captcha_solver: Optional[TwoCaptchaManager] = None
        self.ua_rotator: Optional[UserAgentRotator] = None
        self.robots_checker: Optional[RobotsTxtChecker] = None
        self.antibot_logger: Optional[AntiBotLogger] = None
        self.playwright_manager: Optional[AsyncPlaywrightManager] = None
        self.browser_contexts: Dict[str, Any] = {}
        self.page_pool: Dict[str, List[Any]] = {}
        self.flaresolverr_client: Optional[FlareSolverrClient] = None
        self._flaresolverr_guard_keywords: List[str] = []
        self._flaresolverr_state: Dict[str, Any] = {
            "available": False,
            "last_health_check": 0.0,
            "health_interval": 120.0,
            "session_name": None,
            "session_created": 0.0,
            "session_ttl": 900.0,
        }
        self._flaresolverr_session_settings: Dict[str, Any] = {}
        self._guard_detection_config: Dict[str, Any] = self.config.get(
            "guard_detection", {}
        )
        self._guard_bypass_tracker: Dict[str, Dict[str, float]] = {}
        antibot_integration = (
            self.config.get("antibot_integration", {})
            if isinstance(self.config, dict)
            else {}
        )
        if isinstance(antibot_integration, dict):
            self._domain_wait_profiles: Dict[str, Any] = (
                antibot_integration.get("domain_overrides", {}) or {}
            )
        else:
            self._domain_wait_profiles = {}
        if isinstance(self._guard_detection_config, dict):
            self._guard_domain_overrides: Dict[str, Any] = (
                self._guard_detection_config.get("domain_overrides", {}) or {}
            )
        else:
            self._guard_domain_overrides = {}

        # Performance tracking
        self.request_stats = {
            "total_requests": 0,
            "successful_requests": 0,
            "failed_requests": 0,
            "blocked_requests": 0,
            "proxy_rotations": 0,
        }

        # Circuit breaker infrastructure
        self.domain_circuit_breakers: Dict[str, CircuitBreakerState] = {}
        self.circuit_timeout = timedelta(minutes=5)  # Try again after 5 min
        self.half_open_max_attempts = 1

        self.stealth_script: str = """
        Object.defineProperty(navigator, 'webdriver', {
          get: () => undefined,
        });
        window.chrome = {
          runtime: {},
        };
        Object.defineProperty(navigator, 'plugins', {
          get: () => [1, 2, 3, 4, 5],
        });
        Object.defineProperty(navigator, 'languages', {
          get: () => ['ru-RU', 'ru', 'en'],
        });
        // Enhanced stealth: canvas fingerprint protection
        const getImageData = CanvasRenderingContext2D.prototype.getImageData;
        CanvasRenderingContext2D.prototype.getImageData = function(x, y, width, height) {
          const imageData = getImageData.apply(this, arguments);
          for (let i = 0; i < imageData.data.length; i += 4) {
            imageData.data[i] += Math.floor(Math.random() * 10) - 5; // Slight randomization
          }
          return imageData;
        };
        // Timing randomization for human-like behavior
        const originalSetTimeout = window.setTimeout;
        window.setTimeout = function(callback, delay) {
          const randomizedDelay = delay + Math.random() * 100 - 50;
          return originalSetTimeout(callback, randomizedDelay);
        };
        """

        self._load_config()
        self._initialize_proxy_infrastructure()
        self._initialize_antibot_components()

    def _run_in_playwright_thread(self, func: Callable[..., T], *args, **kwargs) -> T:
        """Execute blocking Playwright sync calls off the event loop."""

        executor = self._playwright_executor
        if executor is None:
            return func(*args, **kwargs)
        future = executor.submit(func, *args, **kwargs)
        return future.result()

    def _shutdown_playwright_resources(self) -> None:
        """Shutdown sync Playwright resources and associated executor."""

        # Close browser/context if still open
        try:
            if self.browser:
                try:
                    self.browser.close()
                except Exception:  # noqa: BLE001 - best effort cleanup
                    pass
                self.browser = None
        except AttributeError:
            # During interpreter shutdown attributes may already be gone
            pass

        try:
            playwright = getattr(self, "playwright", None)
            if playwright:
                try:
                    playwright.stop()
                except Exception:  # noqa: BLE001
                    pass
                self.playwright = None
        except AttributeError:
            pass

        executor = getattr(self, "_playwright_executor", None)
        if executor:
            executor.shutdown(wait=False, cancel_futures=True)
            self._playwright_executor = None

    def _initialize_proxy_infrastructure(self) -> None:
        """Initialize enhanced proxy infrastructure components."""
        try:
            # Initialize new components with configuration
            proxy_config = self.config.get("proxy_infrastructure", {})

            # Load manual proxies when no in-memory list is present
            if not self.config.get("proxies"):
                manual_file = proxy_config.get("manual_proxy_file")
                if manual_file:
                    scheme = proxy_config.get("manual_proxy_scheme", "http")
                    manual_path = Path(manual_file)
                    if not manual_path.is_absolute():
                        base_dir = (
                            Path(self.config_path).resolve().parent
                            if getattr(self, "config_path", None)
                            else Path.cwd()
                        )
                        manual_path = (base_dir / manual_file).resolve()

                    try:
                        proxies: List[str] = []
                        for raw_line in manual_path.read_text(
                            encoding="utf-8"
                        ).splitlines():
                            line = raw_line.strip()
                            if not line or line.startswith("#"):
                                continue
                            parts = line.split(":")
                            if len(parts) >= 4:
                                host, port, user, password = (
                                    parts[0],
                                    parts[1],
                                    parts[2],
                                    ":".join(parts[3:]),
                                )
                                proxies.append(
                                    f"{scheme}://{user}:{password}@{host}:{port}"
                                )
                            elif len(parts) >= 2:
                                host, port = parts[0], parts[1]
                                proxies.append(f"{scheme}://{host}:{port}")

                        if proxies:
                            self.config["proxies"] = proxies
                            self.logger.info(
                                "Loaded %s proxies from %s",
                                len(proxies),
                                manual_path,
                            )
                        else:
                            self.logger.warning(
                                "Manual proxy file %s did not contain any usable proxies",
                                manual_path,
                            )
                    except FileNotFoundError:
                        self.logger.warning(
                            "Manual proxy file not found: %s",
                            manual_path,
                        )
                    except OSError as exc:
                        self.logger.warning(
                            "Failed reading manual proxy file %s: %s",
                            manual_path,
                            exc,
                        )

            if not proxy_config.get("enabled", True):
                self.logger.info("Proxy infrastructure disabled via configuration")
                self.proxy_health_checker = None
                self.session_manager = None
                self.content_validator = None
                self.premium_proxy_manager = None
                self.backoff = None
                self.proxy_rotator = None
                return

            # Initialize proxy health checker
            health_config = proxy_config.get("proxy_health", {})
            self.proxy_health_checker = ProxyHealthChecker(health_config)

            # Initialize session manager
            session_config = proxy_config.get("session_management", {})
            if session_config.get("enabled", True):
                self.session_manager = SessionManager(session_config)
            else:
                self.session_manager = None

            # Initialize content validator
            content_config = proxy_config.get("content_validation", {})
            self.content_validator = ContentValidator(content_config)

            # Initialize premium proxy manager
            premium_config = proxy_config.get("premium_proxies", {})
            self.premium_proxy_manager = PremiumProxyManager(premium_config)

            # Initialize exponential backoff
            backoff_config = proxy_config.get("exponential_backoff", {})
            self.backoff = ExponentialBackoff(backoff_config)

            # Initialize enhanced proxy rotator
            if self.config.get("proxies"):
                try:
                    enhanced_config = {
                        "health_checker": health_config,
                        "premium_proxies": premium_config,
                        "backoff": backoff_config,
                        "content_validator": content_config,
                    }
                    self.proxy_rotator = ProxyRotator(
                        self.config["proxies"], enhanced_config
                    )
                    self.logger.info(
                        f"Enhanced proxy infrastructure initialized with {len(self.config['proxies'])} proxies"
                    )
                except Exception as e:
                    self.logger.warning(f"Enhanced proxy initialization failed: {e}")
                    # Fallback to basic proxy rotator
                    self.proxy_rotator = ProxyRotator(self.config["proxies"])
                    if hasattr(self.proxy_rotator, "validate_proxies"):
                        self.proxy_rotator.validate_proxies()
                    self.logger.info(
                        f"Fallback to basic proxy rotator with {len(self.proxy_rotator.proxies)} valid proxies"
                    )
            else:
                self.logger.warning(
                    "No proxies configured, proxy infrastructure disabled"
                )

        except Exception as e:
            self.logger.error(f"Failed to initialize proxy infrastructure: {e}")
            # Continue without enhanced features
            if self.config.get("proxies"):
                try:
                    self.proxy_rotator = ProxyRotator(self.config["proxies"])
                    if hasattr(self.proxy_rotator, "validate_proxies"):
                        self.proxy_rotator.validate_proxies()
                    self.logger.warning("Using basic proxy rotator as fallback")
                except ValueError:
                    self.logger.warning(
                        "No valid proxies available, continuing without proxies"
                    )
                    self.proxy_rotator = None

    def _initialize_antibot_components(self) -> None:
        """Initialize enhanced anti-bot components."""
        try:
            guard_config = (
                self.config.get("guard_detection")
                if isinstance(self.config, dict)
                else None
            )
            if (not guard_config) and getattr(self, "config_path", None):
                try:
                    guard_config = self.load_config(self.config_path).get(
                        "guard_detection", {}
                    )
                except Exception:  # noqa: BLE001
                    guard_config = {}
            self._guard_detection_config = guard_config or {}
            self._guard_bypass_tracker.clear()
            # Initialize CAPTCHA solver
            captcha_config = self.config.get("captcha_solving", {})
            if captcha_config.get("enabled", False):
                self.captcha_solver = TwoCaptchaManager(captcha_config)
                self.logger.info("2captcha solver initialized")
            else:
                self.logger.debug("CAPTCHA solving disabled")

            # Initialize user agent rotator
            ua_config = self.config.get("user_agent_rotation", {})
            self.ua_rotator = UserAgentRotator(ua_config)
            self.logger.info("User agent rotator initialized")

            # Initialize robots.txt checker
            robots_config = self.config.get("robots_compliance", {})
            self.robots_checker = RobotsTxtChecker(robots_config)
            self.logger.info("Robots.txt checker initialized")

            # Initialize async Playwright manager for advanced browser handling
            playwright_config = self.config.get("playwright_optimization", {})
            if playwright_config.get("enabled", True):
                self.playwright_manager = AsyncPlaywrightManager(
                    playwright_config, logger=self.logger
                )
                self.logger.info("Async Playwright manager initialized")
            else:
                self.logger.debug("Playwright optimization disabled in configuration")

            # Initialize anti-bot logger
            logging_config = self.config.get("antibot_logging", {})
            self.antibot_logger = AntiBotLogger(logging_config)
            self.logger.info("Anti-bot logger initialized")

            # Initialize FlareSolverr integration
            flaresolverr_config = self.config.get("flaresolverr", {})
            if flaresolverr_config.get("enabled"):
                try:
                    self.flaresolverr_client = FlareSolverrClient(flaresolverr_config)
                    integration_settings = flaresolverr_config.get(
                        "integration_settings", {}
                    )
                    guard_triggers = integration_settings.get(
                        "guard_detection_triggers", []
                    )
                    self._flaresolverr_guard_keywords = [
                        trigger.lower()
                        for trigger in guard_triggers
                        if isinstance(trigger, str)
                    ]
                    self._flaresolverr_state["health_interval"] = float(
                        integration_settings.get("health_check_interval_seconds", 120.0)
                    )
                    perf_settings = flaresolverr_config.get("performance_settings", {})
                    self._flaresolverr_state["session_ttl"] = float(
                        perf_settings.get("session_ttl_seconds", 900.0)
                    )
                    self._flaresolverr_session_settings = flaresolverr_config.get(
                        "session_management", {}
                    )
                    self.logger.info(
                        "FlareSolverr client initialized (endpoint %s)",
                        self.flaresolverr_client.endpoint,
                    )
                except Exception as exc:  # noqa: BLE001
                    self.logger.error(
                        "Failed to initialize FlareSolverr client: %s", exc
                    )
                    self.flaresolverr_client = None

        except Exception as e:
            self.logger.error(f"Failed to initialize anti-bot components: {e}")
            # Continue without enhanced features if initialization fails

    async def get_headers(self) -> Dict[str, str]:
        """Generate baseline headers for HTTP clients with rotated user-agent."""

        headers: Dict[str, str] = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Connection": "keep-alive",
        }

        try:
            headers["User-Agent"] = self.rotate_user_agent()
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("Failed to rotate user agent, fallback to generator: %s", exc)
            headers["User-Agent"] = self.ua_generator.random

        return headers

    async def get_proxy(self) -> Optional[str]:
        """Return a validated proxy address if available."""

        try:
            return await self.get_validated_proxy()
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("Unable to acquire proxy: %s", exc)
            return None

    def run_playwright_task(self, func: Callable[..., T], *args, **kwargs) -> T:
        """Execute callable inside the dedicated Playwright executor thread."""

        return self._run_in_playwright_thread(func, *args, **kwargs)

    async def start(self) -> None:
        """Start all async lifecycle components."""
        try:
            # Start session manager background cleanup
            if self.session_manager and hasattr(self.session_manager, "start"):
                self.session_manager.start()
                self.logger.debug("Session manager started")

            # Start proxy rotator background tasks
            if self.proxy_rotator and hasattr(self.proxy_rotator, "start"):
                await self.proxy_rotator.start()
                self.logger.debug("Proxy rotator started")

            # Start premium proxy manager auto-refresh
            if self.premium_proxy_manager and hasattr(
                self.premium_proxy_manager, "start_auto_refresh"
            ):
                await self.premium_proxy_manager.start_auto_refresh()
                self.logger.debug("Premium proxy manager started")

            # Start user agent rotator properly
            if self.ua_rotator and hasattr(self.ua_rotator, "start"):
                await self.ua_rotator.start()
                self.logger.debug("User agent rotator started")

            # Start anti-bot logger background tasks
            if self.antibot_logger and hasattr(self.antibot_logger, "start"):
                await self.antibot_logger.start()
                self.logger.debug("Anti-bot logger started")

            self.logger.info("AntibotManager async lifecycle started successfully")

        except Exception as e:
            self.logger.error(f"Error starting async lifecycle: {e}")
            raise

    async def stop(self) -> None:
        """Stop async lifecycle components and release Playwright resources."""

        try:
            if self.antibot_logger and hasattr(self.antibot_logger, "stop"):
                await self.antibot_logger.stop()

            if self.ua_rotator and hasattr(self.ua_rotator, "stop"):
                await self.ua_rotator.stop()

            if self.premium_proxy_manager and hasattr(
                self.premium_proxy_manager, "stop_auto_refresh"
            ):
                await self.premium_proxy_manager.stop_auto_refresh()

            if self.proxy_rotator and hasattr(self.proxy_rotator, "stop"):
                await self.proxy_rotator.stop()

            if self.session_manager and hasattr(self.session_manager, "stop"):
                await self.session_manager.stop()

        finally:
            self._shutdown_playwright_resources()

    def _load_config(self) -> None:
        """Load configuration using unified config loader."""
        try:
            # Use inherited config from base class (already loaded)
            self.proxies: List[str] = self.config.get("proxies", [])
            self.user_agents: List[str] = self.config.get("user_agents", [])
            self.playwright_options = self.config.get("playwright_options", {})
            self.current_ua_index = 0
            debug_options = self.config.get("debug_options", {})
            if debug_options.get("enabled", False):
                self.playwright_options["debug_mode"] = True
                self.playwright_options["slow_mo"] = debug_options.get(
                    "slow_motion_ms", 0
                )
                self.playwright_options["devtools"] = debug_options.get(
                    "browser_devtools", False
                )
            if debug_options.get("headless_override", False):
                self.playwright_options["headless"] = False
            self._validate_config()
        except Exception as e:
            self.logger.error(f"Error processing antibot configuration: {e}")
            self.config = {}
            self.proxies = []
            self.playwright_options = {}
        except json.JSONDecodeError as e:
            self.logger.error(f"Invalid JSON in configuration file: {e}")
            self.config = {}
            self.proxies = []
            self.playwright_options = {}

    def _validate_config(self) -> None:
        """Validate configuration parameters"""
        if "timeout" in self.config:
            timeout = self.config["timeout"]
            if not isinstance(timeout, int) or timeout < 1 or timeout > 300:
                self.logger.warning("Invalid timeout value, using default 30")
                self.config["timeout"] = 30
        if "delay_range" in self.config:
            delay_range = self.config["delay_range"]
            if (
                not isinstance(delay_range, list)
                or len(delay_range) != 2
                or delay_range[0] >= delay_range[1]
            ):
                self.logger.warning("Invalid delay_range, using default [1, 3]")
                self.config["delay_range"] = [1, 3]

    def _get_browser_sync(self) -> Browser:
        if self.browser is None:
            try:
                self.logger.info("Launching browser...")
                playwright_sync = _get_playwright_sync_api()
                p = sync_playwright().start()
                proxy_settings = None
                try:
                    proxy = self.rotate_proxy()
                except ProxyConnectionError as exc:
                    proxy = None
                    self.logger.warning(
                        "Proxy rotation failed during browser launch: %s",
                        exc,
                    )
                if proxy:
                    proxy_settings = playwright_sync.ProxySettings(server=proxy)
                    self.logger.debug("Using proxy: %s", proxy)

                headless_mode = self.playwright_options.get("headless", True)
                if self.playwright_options.get("debug_mode", False):
                    headless_mode = False
                    self.logger.info("Debug mode enabled, running in headed mode")

                launch_kwargs = {
                    "headless": headless_mode,
                    "slow_mo": self.playwright_options.get("slow_mo", 0),
                    "proxy": proxy_settings,
                }
                extra_args = self.playwright_options.get("args")
                if extra_args:
                    launch_kwargs["args"] = extra_args

                self.browser = p.chromium.launch(**launch_kwargs)
                self.playwright = p
                self.logger.info("Browser launched successfully")
            except Exception as e:  # noqa: BLE001
                self.logger.error("Browser launch failed: %s", e)
                raise BrowserLaunchError(f"Failed to launch browser: {e}") from e
        return self.browser

    def get_browser(self) -> Browser:
        return self._run_in_playwright_thread(self._get_browser_sync)

    def _create_context_sync(self, ua: str) -> BrowserContext:
        try:
            browser = self._get_browser_sync()
            proxy_settings = None
            try:
                proxy = self.rotate_proxy()
            except ProxyConnectionError as exc:
                proxy = None
                self.logger.warning(
                    "Proxy rotation failed during context creation: %s",
                    exc,
                )
            if proxy:
                playwright_sync = _get_playwright_sync_api()
                proxy_settings = playwright_sync.ProxySettings(server=proxy)
                self.logger.debug("Context using proxy: %s", proxy)
            context_kwargs = {
                "user_agent": ua,
                "viewport": {"width": 1920, "height": 1080},
                "locale": "ru-RU",
            }
            if proxy_settings:
                context_kwargs["proxy"] = proxy_settings

            context = browser.new_context(**context_kwargs)
            self.logger.debug("Created browser context with UA: %s...", ua[:50])
            return context
        except Exception as e:  # noqa: BLE001
            self.logger.error("Failed to create browser context: %s", e)
            raise BrowserLaunchError(f"Context creation failed: {e}") from e

    def get_context(self, ua: str) -> BrowserContext:
        return self._run_in_playwright_thread(self._create_context_sync, ua)

    def _create_page_sync(self, ua: Optional[str]) -> Page:
        try:
            resolved_ua = ua or self.ua_generator.random
            context = self._create_context_sync(resolved_ua)
            page = context.new_page()
            page.add_init_script(self.stealth_script)
            self.logger.debug("Created page with stealth script applied")
            return page
        except Exception as e:  # noqa: BLE001
            self.logger.error("Failed to create page: %s", e)
            raise PageNavigationError(f"Page creation failed: {e}") from e

    def get_page(self, ua: Optional[str] = None) -> Page:
        return self._run_in_playwright_thread(self._create_page_sync, ua)

    def get_random_user_agent(self) -> str:
        """Shared UA generation for async scraper compatibility"""
        if self.ua_rotator:
            # Use async method in sync context - get a browser UA synchronously
            try:
                import asyncio

                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # If loop is running, we can't use it directly
                    ua = self.ua_rotator._get_default_user_agent()
                else:
                    ua = loop.run_until_complete(self.ua_rotator.get_next_user_agent())
            except Exception as e:
                self.logger.warning(f"Failed to get UA from rotator: {e}")
                ua = self.ua_generator.random
        else:
            ua = self.ua_generator.random

        self.logger.debug(f"Generated UA: {ua[:50]}...")
        return ua

    def rotate_user_agent(self) -> str:
        """Rotate through configured user agents with rotator fallback."""
        if getattr(self, "user_agents", None):
            if not isinstance(self.current_ua_index, int):
                self.current_ua_index = 0

            user_agent = self.user_agents[
                self.current_ua_index % max(1, len(self.user_agents))
            ]
            self.current_ua_index = (self.current_ua_index + 1) % max(
                1, len(self.user_agents)
            )
            return user_agent

        if self.ua_rotator:
            try:
                # Prefer dedicated sync helper if available
                if hasattr(self.ua_rotator, "get_next_user_agent_sync"):
                    return self.ua_rotator.get_next_user_agent_sync()

                import asyncio

                loop = asyncio.get_event_loop()
                if loop.is_running():
                    return self.ua_rotator._get_default_user_agent()
                return loop.run_until_complete(
                    self.ua_rotator.get_next_user_agent_mandatory()
                )
            except Exception as exc:  # noqa: BLE001
                self.logger.debug(
                    f"User agent rotator unavailable, falling back to generator: {exc}"
                )

        return self.get_random_user_agent()

    async def get_async_session(self) -> tuple[aiohttp.ClientSession, Optional[str]]:
        """Returns aiohttp session with proper stealth headers and enhanced error handling"""
        full_proxy = None  # Initialize to ensure it's always defined

        try:
            import aiohttp

            # Get user agent from rotator if available
            if self.ua_rotator:
                user_agent = await self.ua_rotator.get_next_user_agent_mandatory()
            else:
                user_agent = self.get_random_user_agent()

            headers = {
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "DNT": "1",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Cache-Control": "max-age=0",
            }

            connector = aiohttp.TCPConnector(
                limit=100,
                limit_per_host=30,
                enable_cleanup_closed=True,
                use_dns_cache=True,
                ttl_dns_cache=300,
            )

            proxy = None
            if self.proxy_rotator:
                proxy = await self.rotate_proxy_async()
                if proxy:
                    self.logger.debug(f"Async session using proxy: {proxy}")

            timeout = aiohttp.ClientTimeout(
                total=self.config.get("timeout", 30), connect=10
            )

            session = aiohttp.ClientSession(
                connector=connector, headers=headers, timeout=timeout, trust_env=True
            )

            # Enhanced session cleanup and resource management
            original_close = session.close

            async def enhanced_close():
                self.logger.debug("Closing aiohttp session and cleaning up resources")
                await original_close()
                # Additional cleanup if needed

            session.close = enhanced_close

            # No global proxy set on session to avoid private API; use per-request proxy

            if proxy:
                # Build full proxy URL with credentials for per-request use
                if isinstance(proxy, str):
                    # Assume format like 'http://host:port' or 'http://user:pass@host:port'
                    if "@" not in proxy:
                        # No auth in string, assume no credentials
                        full_proxy = proxy
                    else:
                        # Already has auth in format http://user:pass@host:port
                        full_proxy = proxy
                elif hasattr(proxy, "host") and hasattr(proxy, "port"):
                    # Build from object with auth
                    auth_str = ""
                    if hasattr(proxy, "auth") and proxy.auth:
                        if isinstance(proxy.auth, tuple) and len(proxy.auth) == 2:
                            username, password = proxy.auth
                            auth_str = f"{username}:{password}@"
                        else:
                            # Fallback to base64 if auth is pre-encoded, but prefer URL
                            import base64

                            auth_str = f"{base64.b64encode(str(proxy.auth).encode()).decode()}@"
                    scheme = getattr(proxy, "scheme", "http")
                    full_proxy = f"{scheme}://{auth_str}{proxy.host}:{proxy.port}"
                else:
                    full_proxy = None

            self.logger.debug("Created enhanced aiohttp session with stealth headers")
            return session, full_proxy
        except Exception as e:
            self.logger.error(f"Failed to create async session: {e}")
            raise ProxyConnectionError(f"Async session creation failed: {e}") from e

    def fetch_sitemap(self, url: str) -> str:
        """Fetch sitemap using curl_cffi for better TLS fingerprinting"""
        try:
            self.logger.info(f"Fetching sitemap from {url}")
            start_time = time.time()
            response = _get_curl_cffi_requests().get(
                url, impersonate="chrome110", timeout=self.config.get("timeout", 30)
            )
            response.raise_for_status()
            load_time = time.time() - start_time
            self.logger.info(
                f"Sitemap fetched successfully in {load_time:.2f}s, size: {len(response.text)} bytes"
            )
            return response.text
        except Exception as e:
            self.logger.error(f"Sitemap fetch failed with curl_cffi: {e}")
            return ""

    def rotate_proxy(self) -> Optional[str]:
        proxies_list = getattr(self, "proxies", [])

        if proxies_list:
            if not isinstance(self.current_proxy_index, int):
                self.current_proxy_index = 0
            proxy = proxies_list[self.current_proxy_index % len(proxies_list)]
            self.current_proxy_index = (self.current_proxy_index + 1) % len(
                proxies_list
            )
            self.logger.debug(f"Rotated to proxy (static list): {proxy}")
            return proxy

        proxy: Optional[str] = None

        if self.proxy_rotator:
            try:
                proxy = self.proxy_rotator.get_next_proxy_sync()
            except Exception as exc:  # noqa: BLE001
                self.logger.error(f"Proxy rotation error via rotator: {exc}")
                proxy = None

        if proxy:
            self.logger.debug(f"Rotated to proxy: {proxy}")
            return proxy

        self.logger.warning("No proxy available from rotator")
        return None

    async def rotate_proxy_async(self) -> Optional[str]:
        """Enhanced async proxy rotation with health checking."""
        if not self.proxy_rotator:
            return None
        try:
            # Use enhanced async proxy selection
            proxy = await self.proxy_rotator.get_next_proxy()

            if proxy:
                self.request_stats["proxy_rotations"] += 1
                self.logger.debug(f"Enhanced async rotated to proxy: {proxy[:50]}...")
            return proxy

        except Exception as e:
            self.logger.warning(f"Async proxy rotation failed, attempting refresh: {e}")
            if hasattr(self.proxy_rotator, "validate_and_refresh_pool"):
                try:
                    healthy_count = await self.proxy_rotator.validate_and_refresh_pool()
                    if healthy_count > 0:
                        proxy = await self.proxy_rotator.get_next_proxy()
                        self.logger.info(
                            f"Proxy pool refreshed, using: {proxy[:50]}..."
                        )
                        return proxy
                except Exception as refresh_error:
                    self.logger.error(f"Proxy pool refresh failed: {refresh_error}")

            self.logger.error(f"Enhanced async proxy rotation error: {e}")
            raise ProxyConnectionError(f"Async proxy rotation failed: {e}") from e

    async def get_validated_proxy(self) -> Optional[str]:
        """
        Get healthy proxy with validation.

        Returns:
            Validated proxy URL or None if no healthy proxies available
        """
        if not self.proxy_rotator:
            return None

        # Determine max attempts based on available proxies
        max_attempts = 5  # Default fallback
        try:
            if hasattr(self.proxy_rotator, "proxies") and self.proxy_rotator.proxies:
                max_attempts = min(len(self.proxy_rotator.proxies), 5)
        except Exception:
            pass  # Use default max_attempts

        for attempt in range(max_attempts):
            try:
                if hasattr(self.proxy_rotator, "get_next_proxy"):
                    proxy = await self.proxy_rotator.get_next_proxy()
                    if proxy and self.proxy_health_checker:
                        # Quick health check
                        is_healthy = self.proxy_health_checker.is_proxy_healthy(proxy)
                        if not is_healthy:
                            self.logger.warning(
                                f"Selected proxy {proxy[:50]} failed health check, trying next (attempt {attempt + 1}/{max_attempts})"
                            )
                            continue  # Try next proxy
                    return proxy  # Return valid proxy or None
                else:
                    return await self.rotate_proxy_async()

            except Exception as e:
                self.logger.warning(
                    f"Error getting proxy on attempt {attempt + 1}: {e}"
                )
                continue  # Try again

        self.logger.error(
            f"Failed to get validated proxy after {max_attempts} attempts"
        )
        return None
    def _get_or_create_breaker(self, domain: str) -> CircuitBreakerState:
        """Get or create circuit breaker state for domain."""
        if domain not in self.domain_circuit_breakers:
            self.domain_circuit_breakers[domain] = CircuitBreakerState()
        return self.domain_circuit_breakers[domain]
    
    def _is_circuit_open(self, domain: str) -> bool:
        """Check if circuit breaker is open for domain."""
        breaker = self._get_or_create_breaker(domain)

        if breaker.is_open:
            # Half-open state after timeout
            if breaker.opened_at and datetime.now() - breaker.opened_at > self.circuit_timeout:
                breaker.is_open = False
                breaker.half_open = True
                breaker.half_open_attempts = 0
                self.logger.info(f"Circuit breaker half-open for {domain}")
                return False
            return True

        if breaker.half_open and breaker.half_open_attempts >= self.half_open_max_attempts:
            self.logger.warning(
                f"Half-open attempt limit reached for {domain}; re-opening circuit"
            )
            self._open_circuit(domain)
            return True

        return False
    
    def _should_open_circuit(self, domain: str) -> bool:
        """Determine if circuit should open."""
        breaker = self._get_or_create_breaker(domain)
        
        # Open after 20 consecutive failures
        if breaker.consecutive_failures >= 20:
            self.logger.warning(
                f"Opening circuit for {domain}: {breaker.consecutive_failures} consecutive failures"
            )
            return True
        
        # Open if 80% error rate in last 50 requests
        if len(breaker.recent_results) >= 50:
            error_rate = breaker.recent_results.count(False) / len(breaker.recent_results)
            if error_rate >= 0.8:
                self.logger.warning(
                    f"Opening circuit for {domain}: {error_rate:.1%} error rate"
                )
                return True
        
        return False
    
    def _record_success(self, domain: str) -> None:
        """Record successful request."""
        breaker = self._get_or_create_breaker(domain)
        breaker.consecutive_failures = 0
        breaker.recent_results.append(True)
        if breaker.half_open:
            breaker.half_open = False
            breaker.half_open_attempts = 0
            breaker.opened_at = None
            self.logger.info(f"Circuit breaker CLOSED for {domain} after successful probe")
    
    def _record_failure(self, domain: str) -> None:
        """Record failed request."""
        breaker = self._get_or_create_breaker(domain)
        breaker.consecutive_failures += 1
        breaker.recent_results.append(False)
        if breaker.half_open:
            breaker.half_open = False
            breaker.half_open_attempts = 0
            self.logger.warning(
                f"Circuit breaker HALF-OPEN probe failed for {domain}; reopening"
            )
            self._open_circuit(domain)
    
    def _open_circuit(self, domain: str) -> None:
        """Open circuit breaker."""
        breaker = self._get_or_create_breaker(domain)
        breaker.is_open = True
        breaker.half_open = False
        breaker.half_open_attempts = 0
        breaker.opened_at = datetime.now()
        self.logger.error(
            f"Circuit breaker OPENED for {domain}. "
            f"Will retry after {self.circuit_timeout.total_seconds()/60:.0f} minutes"
        )

    def _before_request(self, domain: str) -> None:
        """Update breaker state prior to executing a request."""
        breaker = self._get_or_create_breaker(domain)
        if breaker.half_open:
            breaker.half_open_attempts += 1

    async def check_domain_health(self, domain: str, timeout: int = 10) -> bool:
        """
        Pre-flight health check before starting mass export.
        Returns True if domain is accessible, False otherwise.
        
        Args:
            domain: Domain to check (without protocol)
            timeout: Timeout in seconds
            
        Returns:
            True if domain is accessible, False otherwise
        """
        test_url = f"https://{domain}/"
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    test_url,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                    allow_redirects=True
                ) as resp:
                    is_healthy = resp.status < 500
                    self.logger.info(
                        f"Health check for {domain}: "
                        f"status={resp.status}, healthy={is_healthy}"
                    )
                    return is_healthy
        except asyncio.TimeoutError:
            self.logger.error(f"Health check timeout for {domain} after {timeout}s")
            return False
        except Exception as e:
            self.logger.error(f"Health check failed for {domain}: {e}")
            return False


    async def make_request_with_retry(
        self,
        url: str,
        method: str = "GET",
        session: Optional[aiohttp.ClientSession] = None,
        **kwargs,
    ) -> Optional[Dict[str, Any]]:
        """
        Make HTTP request with automatic retry and proxy rotation.

        Args:
            url: URL to request
            method: HTTP method
            session: Optional aiohttp session
            **kwargs: Additional request parameters

        Returns:
            Response data dictionary or None if failed
        """
        # Extract domain for circuit breaker
        domain = self._extract_domain(url)
        
        # CHECK CIRCUIT BREAKER FIRST
        if self._is_circuit_open(domain):
            self.logger.warning(
                f"Circuit open for {domain}, skipping request to {url}"
            )
            return None

        self._before_request(domain)
        
        session_created_here = False
        if not session:
            session, proxy = await self.get_async_session()
            kwargs["proxy"] = proxy
            session_created_here = True

        attempt = 0
        # Use backoff max_attempts if available, otherwise fallback to default
        max_attempts = self.backoff.max_attempts if self.backoff else 3
        proxy = kwargs.get("proxy")
        request_cookies = kwargs.get("cookies")
        if request_cookies and hasattr(request_cookies, "items"):
            request_cookies = dict(request_cookies.items())
        try:
            while attempt < max_attempts:
                try:
                    self.request_stats["total_requests"] += 1
                    start_time = time.time()

                    # Make request
                    async with session.request(method, url, **kwargs) as response:
                        response_time = time.time() - start_time
                        content = await response.text()
                        status_code = response.status

                        # Validate response content
                        if self.content_validator:
                            validation_result = (
                                self.content_validator.validate_response(content, url)
                            )

                            guard_triggered = False
                            if not validation_result.is_valid:
                                guard_triggered = self._should_use_flaresolverr(
                                    content,
                                    validation_result.block_type,
                                    status_code,
                                    domain,
                                )
                                if guard_triggered:
                                    flaresolverr_response = (
                                        await self._solve_with_flaresolverr(
                                            url,
                                            method,
                                            headers=dict(kwargs.get("headers", {})),
                                            data=kwargs.get("data")
                                            or kwargs.get("json"),
                                            proxy=proxy,
                                            domain=domain,
                                            cookies=request_cookies,
                                        )
                                    )
                                    if flaresolverr_response:
                                        self.request_stats["successful_requests"] += 1
                                        return flaresolverr_response

                                self.logger.warning(
                                    f"Invalid content detected: {validation_result.block_type}"
                                )
                                self.request_stats["blocked_requests"] += 1

                                # Handle blocked content
                                await self._handle_blocked_response(
                                    proxy, validation_result, attempt
                                )

                                # Try with new proxy
                                proxy = await self.get_validated_proxy()
                                kwargs["proxy"] = proxy
                                attempt += 1
                                continue

                        if (
                            self.flaresolverr_client
                            and self.flaresolverr_client.is_enabled()
                            and self._should_use_flaresolverr(
                                content,
                                None,
                                status_code,
                                domain,
                            )
                        ):
                            flaresolverr_response = await self._solve_with_flaresolverr(
                                url,
                                method,
                                headers=dict(kwargs.get("headers", {})),
                                data=kwargs.get("data") or kwargs.get("json"),
                                proxy=proxy,
                                domain=domain,
                                cookies=request_cookies,
                            )
                            if flaresolverr_response:
                                self.request_stats["successful_requests"] += 1
                                return flaresolverr_response

                        # Successful request
                        self.request_stats["successful_requests"] += 1
                        
                        # Record success for circuit breaker
                        self._record_success(domain)

                        # Update proxy statistics
                        if proxy and self.proxy_rotator:
                            await self.proxy_rotator.mark_proxy_success(
                                proxy, response_time, content
                            )

                        # Log proxy success if logger available
                        if proxy and self.antibot_logger:
                            performance_score = 1.0 / max(
                                response_time, 0.1
                            )  # Simple performance score
                            await self.antibot_logger.log_proxy_rotation(
                                proxy, proxy, "successful_request", performance_score
                            )

                        # Update session if applicable
                        if self.session_manager:
                            domain = self._extract_domain(url)
                            cookies = {
                                cookie.name: cookie.value
                                for cookie in response.cookies.values()
                            }
                            headers = dict(response.headers)
                            await self.session_manager.update_session(
                                domain, cookies=cookies, headers=headers
                            )

                        return {
                            "status": response.status,
                            "content": content,
                            "headers": dict(response.headers),
                            "response_time": response_time,
                            "proxy_used": proxy,
                            "attempt": attempt + 1,
                        }

                except Exception as e:
                    self.request_stats["failed_requests"] += 1
                    error_type = self._classify_error(e)

                    self.logger.warning(
                        f"Request failed (attempt {attempt + 1}): {error_type}"
                    )

                    # Update proxy failure tracking
                    if proxy and self.proxy_rotator:
                        await self.proxy_rotator.mark_proxy_failure(proxy, error_type)

                    # Log proxy failure if logger available
                    if proxy and self.antibot_logger:
                        burned = error_type in ["blocked", "captcha", "bot_detection"]
                        await self.antibot_logger.log_proxy_failure(
                            proxy, error_type, str(e), burned
                        )

                    # Check if we should retry
                    if self.backoff and proxy:
                        should_retry = self.backoff.should_retry(
                            proxy, attempt, error_type
                        )
                        if should_retry:
                            # Wait with exponential backoff
                            delay = await self.backoff.wait_with_backoff(
                                proxy, attempt, error_type
                            )
                            self.logger.debug(f"Retrying after {delay:.2f}s delay")

                            # Get new proxy for retry
                            proxy = await self.get_validated_proxy()
                            kwargs["proxy"] = proxy
                        else:
                            self.logger.error(
                                f"Max retries reached or should not retry for {error_type}"
                            )
                            break

                    attempt += 1

            self.logger.error(f"Request failed after {max_attempts} attempts")
            
            # Record failure and check if circuit should open
            self._record_failure(domain)
            if self._should_open_circuit(domain):
                self._open_circuit(domain)
            
            return None

        finally:
            # Close session only if we created it
            if session_created_here and session and not session.closed:
                await session.close()

    async def validate_response_content(self, response: str, url: str) -> bool:
        """
        Validate response content for blocks/captcha.

        Args:
            response: Response content
            url: URL that was requested

        Returns:
            True if content is valid, False if blocked
        """
        if not self.content_validator:
            return True  # Assume valid if validator not available

        try:
            validation_result = self.content_validator.validate_response(response, url)

            if not validation_result.is_valid:
                self.logger.warning(
                    f"Content validation failed: {validation_result.block_type}"
                )
                self.logger.debug(
                    f"Block indicators: {validation_result.block_indicators}"
                )
                return False

            if validation_result.quality_score < 0.5:
                self.logger.warning(
                    f"Low content quality: {validation_result.quality_score:.2f}"
                )
                return False

            return True

        except Exception as e:
            self.logger.error(f"Error validating response content: {e}")
            return True  # Assume valid on error

    def handle_proxy_failure(self, proxy: str, error_type: str) -> None:
        """
        Handle proxy failures with automatic replacement.

        Args:
            proxy: Proxy URL that failed
            error_type: Type of error encountered
        """
        try:
            if self.proxy_rotator and hasattr(self.proxy_rotator, "mark_proxy_failure"):
                asyncio.create_task(
                    self.proxy_rotator.mark_proxy_failure(proxy, error_type)
                )
            else:
                # Fallback to legacy handling
                if self.proxy_rotator:
                    self.proxy_rotator.mark_failed(proxy)

            self.logger.warning(f"Proxy failure handled: {proxy[:50]} - {error_type}")

        except Exception as e:
            self.logger.error(f"Error handling proxy failure: {e}")

    async def get_session_for_domain(self, domain: str) -> Optional[Dict[str, Any]]:
        """
        Get stored session data for domain.

        Args:
            domain: Domain name

        Returns:
            Session data dictionary or None
        """
        if not self.session_manager:
            return None

        try:
            session_data = await self.session_manager.load_session(domain)
            if session_data and session_data.is_valid():
                return {
                    "cookies": session_data.cookies,
                    "headers": session_data.headers,
                    "auth_tokens": session_data.auth_tokens,
                    "user_agent": session_data.user_agent,
                }
            return None

        except Exception as e:
            self.logger.error(f"Error loading session for {domain}: {e}")
            return None

    async def save_session_for_domain(
        self, domain: str, session_data: Dict[str, Any]
    ) -> bool:
        """
        Save session data for domain.

        Args:
            domain: Domain name
            session_data: Session data to save

        Returns:
            True if saved successfully
        """
        if not self.session_manager:
            return False

        try:
            return await self.session_manager.save_session(domain, session_data)

        except Exception as e:
            self.logger.error(f"Error saving session for {domain}: {e}")
            return False

    async def get_comprehensive_antibot_stats(self) -> Dict[str, Any]:
        """
        Get comprehensive anti-bot infrastructure statistics.

        Returns:
            Statistics dictionary including all components
        """
        stats = {
            "request_stats": self.request_stats.copy(),
            "proxy_rotator_available": self.proxy_rotator is not None,
            "health_checker_available": self.proxy_health_checker is not None,
            "session_manager_available": self.session_manager is not None,
            "content_validator_available": self.content_validator is not None,
            "premium_manager_available": self.premium_proxy_manager is not None,
            "backoff_available": self.backoff is not None,
            "captcha_solver_available": self.captcha_solver is not None,
            "ua_rotator_available": self.ua_rotator is not None,
            "robots_checker_available": self.robots_checker is not None,
            "antibot_logger_available": self.antibot_logger is not None,
        }

        try:
            # Add proxy rotator stats
            if self.proxy_rotator and hasattr(
                self.proxy_rotator, "get_proxy_statistics"
            ):
                proxy_stats = await self.proxy_rotator.get_proxy_statistics()
                stats["proxy_rotator_stats"] = proxy_stats

            # Add session manager stats
            if self.session_manager and hasattr(
                self.session_manager, "get_cache_stats"
            ):
                session_stats = self.session_manager.get_cache_stats()
                stats["session_stats"] = session_stats

            # Add backoff stats
            if self.backoff and hasattr(self.backoff, "get_global_statistics"):
                backoff_stats = self.backoff.get_global_statistics()
                stats["backoff_stats"] = backoff_stats

            # Add premium proxy stats
            if self.premium_proxy_manager and hasattr(
                self.premium_proxy_manager, "monitor_proxy_usage"
            ):
                premium_stats = await self.premium_proxy_manager.monitor_proxy_usage()
                stats["premium_proxy_stats"] = premium_stats

            # Add CAPTCHA solver stats
            if self.captcha_solver and hasattr(self.captcha_solver, "get_statistics"):
                captcha_stats = self.captcha_solver.get_statistics()
                stats["captcha_solver_stats"] = captcha_stats

            # Add user agent rotator stats
            if self.ua_rotator and hasattr(self.ua_rotator, "get_statistics"):
                ua_stats = self.ua_rotator.get_statistics()
                stats["user_agent_stats"] = ua_stats

            # Add robots checker stats
            if self.robots_checker and hasattr(
                self.robots_checker, "get_compliance_statistics"
            ):
                robots_stats = self.robots_checker.get_compliance_statistics()
                stats["robots_compliance_stats"] = robots_stats

            # Add anti-bot logger stats
            if self.antibot_logger and hasattr(
                self.antibot_logger, "get_comprehensive_statistics"
            ):
                logger_stats = self.antibot_logger.get_comprehensive_statistics()
                stats["antibot_logger_stats"] = logger_stats

        except Exception as e:
            self.logger.error(f"Error gathering comprehensive anti-bot stats: {e}")
            stats["stats_error"] = str(e)

        return stats

    # Keep the old method for backward compatibility
    async def get_proxy_infrastructure_stats(self) -> Dict[str, Any]:
        """Get proxy infrastructure statistics (legacy method)."""
        return await self.get_comprehensive_antibot_stats()

    async def _handle_blocked_response(
        self, proxy: str, validation_result, attempt: int
    ) -> None:
        """Handle blocked response by updating proxy status."""
        if not proxy:
            return

        block_type = validation_result.block_type or "unknown_block"

        # Mark proxy as potentially burned based on block type
        if block_type in ["captcha", "blocked", "bot_detection"]:
            if self.proxy_rotator:
                await self.proxy_rotator.mark_proxy_burned(proxy, block_type)
        else:
            if self.proxy_rotator:
                await self.proxy_rotator.mark_proxy_failure(proxy, block_type)

    def _should_use_flaresolverr(
        self,
        content: str,
        block_type: Optional[str],
        status: Optional[int],
        domain: Optional[str],
    ) -> bool:
        if not self.flaresolverr_client or not self.flaresolverr_client.is_enabled():
            return False

        if not content:
            return False

        if not isinstance(content, str):
            try:
                content = content.decode("utf-8", "ignore")
            except AttributeError:
                content = str(content)

        lowered_block = (block_type or "").lower()
        guard_triggered = False

        domain_override = self._resolve_domain_override(
            domain, self._guard_domain_overrides
        )

        if domain_override:
            keywords = domain_override.get("keywords")
            if keywords and content:
                lowered_content = content.lower()
                if any(str(keyword).lower() in lowered_content for keyword in keywords):
                    guard_triggered = True

            status_codes = domain_override.get("status_codes")
            if status_codes and status in status_codes:
                guard_triggered = True

        if lowered_block in {"bot_detection", "captcha", "rate_limit", "blocked"}:
            guard_triggered = True

        if not guard_triggered and looks_like_guard_html(content):
            guard_triggered = True

        if not guard_triggered and self._flaresolverr_guard_keywords:
            lowered_content = content.lower()
            if any(
                keyword in lowered_content
                for keyword in self._flaresolverr_guard_keywords
            ):
                guard_triggered = True

        if not guard_triggered and status in {403, 429}:
            guard_triggered = True

        if guard_triggered and domain and domain.endswith("6wool.ru"):
            self.logger.info(
                "Detected 6wool.ru DDoS-Guard challenge; enabling FlareSolverr fallback"
            )

        if not guard_triggered:
            return False

        max_attempts = int(
            self._guard_detection_config.get("max_bypass_attempts", 0) or 0
        )
        cooldown_seconds = float(
            self._guard_detection_config.get("cooldown_seconds", 0) or 0.0
        )

        if domain_override:
            if isinstance(domain_override.get("max_bypass_attempts"), int):
                max_attempts = int(domain_override["max_bypass_attempts"])
            if isinstance(domain_override.get("cooldown_seconds"), (int, float)):
                cooldown_seconds = float(domain_override["cooldown_seconds"])

        if max_attempts <= 0:
            return True

        domain_key = domain or "__global__"
        tracker = self._guard_bypass_tracker.setdefault(
            domain_key, {"attempts": 0, "cooldown_until": 0.0}
        )
        now = time.time()

        cooldown_until = float(tracker.get("cooldown_until", 0.0))
        if cooldown_until and now < cooldown_until:
            self.logger.debug(
                "FlareSolverr guard cooldown active for %s (%.0fs remaining)",
                domain_key,
                cooldown_until - now,
            )
            return False

        attempts = int(tracker.get("attempts", 0))
        if attempts >= max_attempts:
            tracker["attempts"] = 0
            if cooldown_seconds > 0:
                tracker["cooldown_until"] = now + cooldown_seconds
                self.logger.info(
                    "Reached max FlareSolverr bypass attempts for %s; cooling down %.0fs",
                    domain_key,
                    cooldown_seconds,
                )
            else:
                tracker["cooldown_until"] = now
            return False

        tracker["attempts"] = attempts + 1
        tracker["cooldown_until"] = 0.0
        return True

    async def _is_flaresolverr_available(self) -> bool:
        if not self.flaresolverr_client or not self.flaresolverr_client.is_enabled():
            return False

        now = time.time()
        interval = float(self._flaresolverr_state.get("health_interval", 120.0))
        last_check = float(self._flaresolverr_state.get("last_health_check", 0.0))

        if now - last_check < interval and self._flaresolverr_state.get("available"):
            return True

        available = await self.flaresolverr_client.health_check()
        self._flaresolverr_state["available"] = available
        self._flaresolverr_state["last_health_check"] = now
        if not available:
            self.logger.debug("FlareSolverr health check failed; disabling temporarily")
        return available

    async def _ensure_flaresolverr_session(self, domain: str) -> Optional[str]:
        if not self.session_settings_enabled():
            return None

        ttl = float(self._flaresolverr_state.get("session_ttl", 900.0))
        existing = self._flaresolverr_state.get("session_name")
        created_at = float(self._flaresolverr_state.get("session_created", 0.0))
        now = time.time()

        if existing and now - created_at < ttl:
            return existing

        session_name = f"ws-{domain.replace('.', '-')}-{uuid.uuid4().hex[:8]}"
        try:
            created = await self.flaresolverr_client.create_session(session_name)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("Failed to create FlareSolverr session: %s", exc)
            return None

        if created:
            self._flaresolverr_state["session_name"] = created
            self._flaresolverr_state["session_created"] = now
            return created
        return None

    def session_settings_enabled(self) -> bool:
        if not self.flaresolverr_client:
            return False
        return bool(self._flaresolverr_session_settings.get("enabled", False))

    async def _solve_with_flaresolverr(
        self,
        url: str,
        method: str,
        headers: Optional[Dict[str, str]],
        data: Optional[Any],
        proxy: Optional[str],
        domain: str,
        cookies: Optional[Dict[str, str]] = None,
    ) -> Optional[Dict[str, Any]]:
        if not await self._is_flaresolverr_available():
            return None

        session_name = None
        if self.session_settings_enabled():
            session_name = await self._ensure_flaresolverr_session(domain)

        def _coerce_mapping(value: Optional[Dict[str, Any]]) -> Dict[str, str]:
            if not value:
                return {}
            if isinstance(value, dict):
                return {str(k): str(v) for k, v in value.items() if v is not None}
            if hasattr(value, "items"):
                return {
                    str(k): str(v)
                    for k, v in dict(value.items()).items()
                    if v is not None
                }
            return {}

        session_cookies: Dict[str, str] = {}
        session_headers: Dict[str, str] = {}
        if self.session_manager:
            session = await self.session_manager.load_session(domain)
            if session:
                session_cookies.update(session.cookies or {})
                session_headers.update(session.headers or {})
                if session.user_agent and "User-Agent" not in session_headers:
                    session_headers["User-Agent"] = session.user_agent

        combined_headers: Dict[str, str] = {}
        if session_headers:
            combined_headers.update(_coerce_mapping(session_headers))
        combined_headers.update(_coerce_mapping(headers))

        combined_cookies: Dict[str, str] = {}
        if session_cookies:
            combined_cookies.update(_coerce_mapping(session_cookies))
        combined_cookies.update(_coerce_mapping(cookies))

        try:
            if method.upper() == "GET":
                solved = await self.flaresolverr_client.solve_get_request(
                    url,
                    headers=combined_headers or None,
                    cookies=combined_cookies or None,
                    proxy=proxy,
                    session=session_name,
                )
            else:
                solved = await self.flaresolverr_client.solve_post_request(
                    url,
                    data=data,
                    headers=combined_headers or None,
                    cookies=combined_cookies or None,
                    proxy=proxy,
                    session=session_name,
                )
        except FlareSolverrError as exc:
            self.logger.warning("FlareSolverr solve failed: %s", exc)
            return None

        if not solved:
            return None

        self.logger.info("FlareSolverr bypass successful for %s", url)
        html_content = solved.get("html", "")
        response_time = solved.get("response_time") or 0.0
        headers = solved.get("headers", {})
        cookies = solved.get("cookies", {})
        status = solved.get("status", 200)

        if self.session_manager:
            await self.session_manager.update_session(
                domain, cookies=cookies, headers=headers
            )

        tracker = self._guard_bypass_tracker.get(domain)
        if tracker:
            tracker["attempts"] = 0
            tracker["cooldown_until"] = 0.0

        return {
            "status": status,
            "content": html_content,
            "headers": headers,
            "response_time": response_time,
            "proxy_used": proxy,
            "attempt": 1,
            "flaresolverr": True,
            "final_url": solved.get("url"),
            "user_agent": solved.get("user_agent"),
        }

    def fetch_json_via_flaresolverr(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        cookies: Optional[Dict[str, str]] = None,
        timeout: int = 10,
    ) -> Optional[Any]:
        """Synchronously resolve a JSON endpoint via FlareSolverr when enabled."""

        if not (self.flaresolverr_client and self.flaresolverr_client.is_enabled()):
            return None

        async def _runner() -> Optional[Dict[str, Any]]:
            domain = self._extract_domain(url)
            return await self._solve_with_flaresolverr(
                url,
                "GET",
                headers,
                None,
                None,
                domain,
                cookies=cookies,
            )

        try:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop and loop.is_running():
                future = asyncio.run_coroutine_threadsafe(_runner(), loop)
                result = future.result(timeout=timeout)
            else:
                result = asyncio.run(_runner())
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("FlareSolverr JSON fetch failed for %s: %s", url, exc)
            return None

        if not result:
            return None

        content = result.get("content")
        if not content:
            return None

        try:
            return json.loads(content)
        except json.JSONDecodeError:
            self.logger.debug("FlareSolverr response not valid JSON for %s", url)
            return None

    def _classify_error(self, error: Exception) -> str:
        """Classify error for retry logic."""
        error_str = str(error).lower()

        if "timeout" in error_str or "timed out" in error_str:
            return "timeout"
        elif "proxy" in error_str:
            return "proxy_error"
        elif "connection" in error_str:
            return "network"
        elif "429" in error_str or "rate limit" in error_str:
            return "rate_limit"
        elif "403" in error_str or "forbidden" in error_str:
            return "blocked"
        elif "5" in error_str and any(
            code in error_str for code in ["500", "502", "503", "504"]
        ):
            return "http_5xx"
        elif "4" in error_str and any(
            code in error_str for code in ["400", "401", "404"]
        ):
            return "http_4xx"
        else:
            return "unknown"

    def _extract_domain(self, url: str) -> str:
        """Extract domain from URL."""
        try:
            from urllib.parse import urlparse

            return urlparse(url).netloc
        except Exception:
            return url

    def _resolve_domain_override(
        self,
        domain: Optional[str],
        overrides: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        if not domain or not overrides or not isinstance(overrides, dict):
            return {}

        normalized = domain.lower()
        candidates = [normalized]
        if normalized.startswith("www."):
            candidates.append(normalized[4:])

        for candidate in candidates:
            value = overrides.get(candidate)
            if isinstance(value, dict):
                return value

        return {}

    async def make_ethical_request(
        self,
        url: str,
        method: str = "GET",
        session: Optional[aiohttp.ClientSession] = None,
        **kwargs,
    ) -> Optional[Dict[str, Any]]:
        """
        Enhanced request method with full anti-bot integration.

        This method implements the complete enhanced request flow:
        1. Pre-request: Check robots.txt compliance and get crawl delay
        2. Request preparation: Mandatory UA rotation, proxy selection
        3. Request execution: With comprehensive error handling and logging
        4. Response validation: CAPTCHA detection and automatic solving
        5. Post-request: Performance logging and metrics collection

        Args:
            url: URL to request
            method: HTTP method
            session: Optional aiohttp session
            **kwargs: Additional request parameters

        Returns:
            Response data dictionary or None if failed
        """
        # Check if antibot_logger is enabled for enhanced features
        if not (self.antibot_logger and self.antibot_logger.enabled):
            return await self.make_request_with_retry(url, method, session, **kwargs)

        request_id = None
        domain = self._extract_domain(url)

        try:
            # Phase 1: Pre-request - Check robots.txt compliance
            robots_result = await self.check_robots_compliance(url)
            if not robots_result["allowed"]:
                if self.antibot_logger:
                    await self.antibot_logger.log_robots_compliance_check(
                        url,
                        robots_result.get("user_agent_used", "*"),
                        False,
                        robots_result["crawl_delay"],
                        robots_result["reason"],
                    )

                self.logger.warning(
                    f"Request blocked by robots.txt: {url} - {robots_result['reason']}"
                )
                return None

            # Apply crawl delay if required
            if robots_result["crawl_delay"] > 0 and self.robots_checker:
                await self.robots_checker.apply_crawl_delay(domain)

            # Phase 2: Request preparation - Mandatory UA rotation
            old_ua = (
                getattr(self.ua_rotator, "last_used_ua", None)
                if self.ua_rotator
                else None
            )
            user_agent = await self.get_next_user_agent_mandatory(
                domain, force_rotation=True
            )

            # Log UA rotation if changed
            if old_ua and old_ua != user_agent and self.antibot_logger:
                await self.antibot_logger.log_user_agent_rotation(
                    old_ua,
                    user_agent,
                    domain=domain,
                    strategy=getattr(self.ua_rotator, "rotation_strategy", "unknown"),
                )

            # Update headers with new user agent
            if "headers" not in kwargs:
                kwargs["headers"] = {}
            kwargs["headers"]["User-Agent"] = user_agent

            # Log request start
            if self.antibot_logger:
                request_id = await self.antibot_logger.log_request_start(
                    url,
                    method=method,
                    user_agent=user_agent,
                    proxy=kwargs.get("proxy") or "",
                )

            # Phase 3: Request execution with retry logic
            start_time = time.time()
            response_data = await self.make_request_with_retry(
                url, method, session, **kwargs
            )
            response_time = time.time() - start_time

            if not response_data:
                # Log failure
                if self.antibot_logger and request_id:
                    await self.antibot_logger.log_request_complete(
                        request_id,
                        status_code=0,
                        response_time=response_time,
                        content_length=0,
                        error="Request failed",
                        blocked=True,
                        captcha_detected=False,
                    )
                return None

            # Phase 4: Response validation - CAPTCHA detection and solving
            content = response_data.get("content", "")
            captcha_result = await self.solve_captcha_if_detected(
                content, url, kwargs.get("proxy")
            )

            if captcha_result:
                # CAPTCHA was detected and solved, retry request with solution
                self.logger.info(f"CAPTCHA solved for {url}, retrying request")

                # Update any necessary parameters with CAPTCHA solution
                # This would depend on the specific CAPTCHA implementation
                response_data = await self.make_request_with_retry(
                    url, method, session, **kwargs
                )
                response_time = time.time() - start_time

            # Phase 5: Post-request - Performance logging and metrics
            if self.antibot_logger and request_id:
                captcha_detected = captcha_result is not None
                blocked = (
                    response_data.get("status", 0) in [403, 429]
                    if response_data
                    else False
                )

                await self.antibot_logger.log_request_complete(
                    request_id,
                    status_code=response_data.get("status", 0) if response_data else 0,
                    response_time=response_time,
                    content_length=len(content) if content else 0,
                    error="",
                    blocked=blocked,
                    captcha_detected=captcha_detected,
                )

            # Update user agent effectiveness
            if self.ua_rotator and response_data:
                success = 200 <= response_data.get("status", 0) < 300
                self.ua_rotator.analyze_user_agent_effectiveness(
                    user_agent, success, response_time, domain
                )

            return response_data

        except Exception as e:
            self.logger.error(f"Error in ethical request for {url}: {e}")

            # Log error
            if self.antibot_logger and request_id:
                await self.antibot_logger.log_request_complete(
                    request_id,
                    status_code=0,
                    response_time=(
                        time.time() - start_time if "start_time" in locals() else 0
                    ),
                    content_length=0,
                    error=str(e),
                    blocked=True,
                    captcha_detected=False,
                )

            return None

    async def solve_captcha_if_detected(
        self, content: str, url: str, proxy: Optional[str] = None
    ) -> Optional[str]:
        """
        Automatic CAPTCHA detection and solving.

        Args:
            content: HTML content to analyze
            url: URL where content was retrieved
            proxy: Proxy used for the request

        Returns:
            CAPTCHA solution token or None if no CAPTCHA detected/solved
        """
        if not self.captcha_solver or not content:
            return None

        try:
            # Detect CAPTCHA type and parameters
            detection_result = await self.captcha_solver.detect_captcha_type(
                content, url
            )

            if not detection_result["detected"]:
                return None

            # Log CAPTCHA detection
            if self.antibot_logger:
                await self.antibot_logger.log_captcha_detection(
                    url,
                    detection_result["type"],
                    site_key=detection_result.get("site_key") or "",
                    action=detection_result.get("action") or "",
                    confidence=detection_result.get("confidence") or 0.0,
                )

            self.logger.info(f"CAPTCHA detected: {detection_result['type']} on {url}")

            # Get user agent for solving
            user_agent = (
                await self.get_next_user_agent_mandatory() if self.ua_rotator else None
            )

            # Solve based on CAPTCHA type
            solution = None
            start_time = time.time()

            if detection_result["type"] == "recaptcha_v2":
                solution = await self.captcha_solver.solve_recaptcha_v2(
                    detection_result["site_key"], url, proxy or "", user_agent or ""
                )
            elif detection_result["type"] == "recaptcha_v3":
                action = detection_result.get("action", "submit")
                solution = await self.captcha_solver.solve_recaptcha_v3(
                    detection_result["site_key"],
                    url,
                    action,
                    proxy or "",
                    user_agent or "",
                )
            elif detection_result["type"] == "hcaptcha":
                solution = await self.captcha_solver.solve_hcaptcha(
                    detection_result["site_key"], url, proxy or "", user_agent or ""
                )
            elif detection_result["type"] == "image_captcha":
                image_url = detection_result.get("image_url")
                if not image_url:
                    self.logger.warning("Image CAPTCHA detected but no image URL found")
                    return None

                start_time = time.time()
                try:
                    # Get user agent
                    user_agent = (
                        await self.get_next_user_agent_mandatory()
                        if self.ua_rotator
                        else None
                    )

                    # Create session for image fetch
                    headers = {"User-Agent": user_agent} if user_agent else {}
                    connector = aiohttp.TCPConnector(limit=10, limit_per_host=5)
                    async with aiohttp.ClientSession(
                        connector=connector, headers=headers
                    ) as session:
                        # Use proxy if provided
                        proxy_url = proxy
                        async with session.get(
                            image_url, proxy=proxy_url, timeout=30
                        ) as img_response:
                            if img_response.status != 200:
                                self.logger.error(
                                    f"Failed to fetch image CAPTCHA: HTTP {img_response.status}"
                                )
                                return None

                            image_bytes = await img_response.read()

                            # Solve the CAPTCHA
                            solution = await self.captcha_solver.solve_image_captcha(
                                image_bytes
                            )
                            solve_time = time.time() - start_time

                            if solution:
                                self.logger.info(
                                    f"Image CAPTCHA solved successfully in {solve_time:.2f}s"
                                )
                                # Log solve attempt
                                if self.antibot_logger:
                                    await self.antibot_logger.log_captcha_solve_attempt(
                                        "image_captcha", solve_time, True, 0.0, ""
                                    )
                                return solution
                            else:
                                self.logger.warning(
                                    f"Image CAPTCHA solving failed after {solve_time:.2f}s"
                                )
                                if self.antibot_logger:
                                    await self.antibot_logger.log_captcha_solve_attempt(
                                        "image_captcha",
                                        solve_time,
                                        False,
                                        0.0,
                                        "Solving failed",
                                    )
                                return None

                except Exception as e:
                    solve_time = time.time() - start_time
                    self.logger.error(f"Error solving image CAPTCHA: {e}")
                    if self.antibot_logger:
                        await self.antibot_logger.log_captcha_solve_attempt(
                            "image_captcha", solve_time, False, 0.0, str(e)
                        )
                    return None

            solve_time = time.time() - start_time

            # Log solving attempt
            if self.antibot_logger:
                await self.antibot_logger.log_captcha_solve_attempt(
                    detection_result["type"],
                    solve_time,
                    solution is not None,
                    0.0,
                    "" if solution else "Solving failed",
                )

            return solution

        except Exception as e:
            self.logger.error(f"Error solving CAPTCHA for {url}: {e}")

            if self.antibot_logger:
                await self.antibot_logger.log_captcha_solve_attempt(
                    "unknown", 0, False, 0.0, str(e)
                )

            return None

    async def get_next_user_agent_mandatory(
        self, domain: Optional[str] = None, force_rotation: bool = True
    ) -> str:
        """
        Mandatory user-agent rotation for every request.

        Args:
            domain: Domain for domain-specific preferences
            force_rotation: Force rotation even if not time-based

        Returns:
            User agent string (guaranteed to be different from last)
        """
        if not self.ua_rotator:
            return self.get_random_user_agent()

        try:
            # Capture the previously used UA before rotation so logging reflects the actual change
            old_ua = getattr(self.ua_rotator, "last_used_ua", None)

            # Get new user agent with mandatory rotation
            user_agent = await self.ua_rotator.get_next_user_agent_mandatory(
                domain, force_rotation
            )

            # Log rotation only when the UA actually changed
            if self.antibot_logger and old_ua and old_ua != user_agent:
                await self.antibot_logger.log_user_agent_rotation(
                    old_ua,
                    user_agent,
                    domain=domain,
                    strategy=getattr(self.ua_rotator, "rotation_strategy", "unknown"),
                )

            return user_agent

        except Exception as e:
            self.logger.error(f"Error getting mandatory user agent: {e}")
            return self.get_random_user_agent()

    async def get_optimized_browser_context(
        self,
        domain: Optional[str],
        proxy: Optional[str] = None,
        force_rotation: bool = False,
    ) -> Optional[AsyncBrowserContext]:
        """Retrieve an optimized async Playwright browser context for a domain."""
        if not self.playwright_manager:
            self.logger.debug(
                "Playwright manager unavailable, cannot provide browser context"
            )
            return None

        user_agent: Optional[str] = None
        if self.ua_rotator:
            try:
                user_agent = await self.get_next_user_agent_mandatory(
                    domain=domain, force_rotation=force_rotation
                )
            except Exception as exc:  # noqa: BLE001
                self.logger.debug(f"Failed to rotate user agent for context: {exc}")

        context = await self.playwright_manager.get_optimized_browser_context(
            domain=domain, proxy=proxy, user_agent=user_agent
        )

        if context and domain:
            self.browser_contexts[domain] = context

        return context

    async def create_stealth_context(
        self,
        domain: Optional[str],
        proxy: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> Optional[AsyncBrowserContext]:
        """Create or reuse a stealth-enabled Playwright context tied to anti-bot settings."""
        if not self.playwright_manager:
            return None

        ua = user_agent
        if not ua and self.ua_rotator:
            ua = await self.get_next_user_agent_mandatory(
                domain=domain, force_rotation=False
            )

        context = await self.playwright_manager.get_optimized_browser_context(
            domain=domain, proxy=proxy, user_agent=ua
        )

        if context and self.stealth_script:
            try:
                await context.add_init_script(self.stealth_script)
            except Exception as exc:  # noqa: BLE001
                self.logger.debug(f"Failed to apply stealth script to context: {exc}")

        if context and domain:
            self.browser_contexts[domain] = context

        return context

    async def navigate_with_antibot(
        self,
        page: AsyncPage,
        url: str,
        wait_for: Optional[str] = None,
        solve_captcha: bool = True,
    ) -> bool:
        """Navigate a Playwright page with full anti-bot safeguards."""
        if not self.playwright_manager:
            self.logger.warning(
                "Playwright manager unavailable, cannot navigate with anti-bot"
            )
            return False

        domain = None
        try:
            domain = self._extract_domain(url)
        except Exception:  # noqa: BLE001
            domain = None

        domain_wait_config = self._resolve_domain_override(
            domain, self._domain_wait_profiles
        )

        user_agent = None
        try:
            user_agent = await page.evaluate("() => navigator.userAgent")
        except Exception:  # noqa: BLE001
            pass

        if self.robots_checker:
            compliance = await self.robots_checker.check_url_allowed(
                url, user_agent=user_agent
            )
            if not compliance.get("allowed", True):
                self.logger.warning(f"Navigation blocked by robots.txt rules: {url}")
                return False

            crawl_delay = compliance.get("crawl_delay", 0)
            if crawl_delay:
                await asyncio.sleep(crawl_delay)

        if domain and domain.endswith("6wool.ru") and domain_wait_config:
            self.logger.info(
                "sixwool.ru navigation profile enabled: selectors=%s retries=%s",
                domain_wait_config.get("wait_for_selectors"),
                domain_wait_config.get("navigation_retries", 1),
            )

        domain_wait_selector = None
        if not wait_for and domain_wait_config:
            wait_candidates = domain_wait_config.get("wait_for_selectors")
            if isinstance(wait_candidates, list) and wait_candidates:
                domain_wait_selector = wait_candidates[0]

        max_attempts = 1
        if domain_wait_config and isinstance(
            domain_wait_config.get("navigation_retries"), int
        ):
            max_attempts = max(1, int(domain_wait_config["navigation_retries"]))

        backoff_schedule: List[float] = []
        if domain_wait_config:
            schedule = domain_wait_config.get("retry_backoff_seconds")
            if isinstance(schedule, list):
                backoff_schedule = schedule

        success = False
        attempts = 0
        while attempts < max_attempts:
            attempts += 1
            selector_to_wait = wait_for or domain_wait_selector
            success = await self.playwright_manager.navigate_with_optimization(
                page,
                url,
                wait_for=selector_to_wait,
            )
            if success:
                break

            if attempts < max_attempts:
                delay = 2.0
                if backoff_schedule:
                    idx = min(attempts - 1, len(backoff_schedule) - 1)
                    try:
                        delay = float(backoff_schedule[idx])
                    except (TypeError, ValueError):
                        delay = 2.0
                self.logger.info(
                    "Navigation retry for %s (%d/%d) after %.1fs backoff",
                    url,
                    attempts + 1,
                    max_attempts,
                    delay,
                )
                await asyncio.sleep(delay)

        if success:
            if domain_wait_config:
                wait_timeout = int(
                    domain_wait_config.get("wait_selector_timeout", 35000) or 35000
                )
                selectors = domain_wait_config.get("wait_for_selectors")
                if isinstance(selectors, list):
                    for selector in selectors:
                        if not selector or selector == (
                            wait_for or domain_wait_selector
                        ):
                            continue
                        try:
                            await page.wait_for_selector(selector, timeout=wait_timeout)
                        except Exception as exc:  # noqa: BLE001
                            self.logger.debug(
                                "Post-navigation wait for %s failed: %s",
                                selector,
                                exc,
                            )

                additional_wait = domain_wait_config.get("playwright_wait_seconds")
                if isinstance(additional_wait, (int, float)) and additional_wait > 0:
                    self.logger.debug(
                        "Hydration pause %.1fs for domain %s",
                        additional_wait,
                        domain or "unknown",
                    )
                    await asyncio.sleep(additional_wait)

            return True

        if solve_captcha and self.captcha_solver:
            try:
                html = await page.content()
                detection = await self.captcha_solver.detect_captcha_type(html, url)
                if detection.get("detected"):
                    self.logger.info(
                        f"CAPTCHA detected during navigation ({detection.get('type')}), solving..."
                    )
                    # Delegating to existing CAPTCHA solving workflow
                    await self.solve_captcha_if_detected(html, url)
            except Exception as exc:  # noqa: BLE001
                self.logger.debug(
                    f"CAPTCHA handling failed after navigation error: {exc}"
                )

        return False

    async def check_robots_compliance(
        self, url: str, user_agent: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Pre-request robots.txt compliance checking.

        Args:
            url: URL to check for compliance
            user_agent: User agent to check permissions for

        Returns:
            Dictionary with compliance result and crawl delay info
        """
        if not self.robots_checker:
            return {
                "allowed": True,
                "crawl_delay": 1.0,
                "reason": "robots_checker_not_available",
                "user_agent_used": user_agent or "*",
            }

        try:
            # Check URL permission and get crawl delay
            result = await self.robots_checker.check_url_allowed(
                url, user_agent or self.robots_checker.default_user_agent
            )

            # Log compliance check
            if self.antibot_logger:
                await self.antibot_logger.log_robots_compliance_check(
                    url,
                    result.get("user_agent_used", user_agent or "*"),
                    result["allowed"],
                    result["crawl_delay"],
                    result["reason"],
                )

            return result

        except Exception as e:
            self.logger.error(f"Error checking robots compliance for {url}: {e}")

            # Default to allowed on error
            return {
                "allowed": True,
                "crawl_delay": 1.0,
                "reason": f"robots_check_error: {str(e)}",
                "user_agent_used": user_agent or "*",
            }

    def human_delay(self, seconds: Optional[float] = None) -> None:
        delay_range = self.config.get("delay_range", [1, 3])
        if seconds is None:
            seconds = random.uniform(delay_range[0], delay_range[1])
        self.logger.debug(f"Human delay: {seconds:.2f}s")
        time.sleep(seconds)

    async def async_human_delay(self, seconds: Optional[float] = None) -> None:
        import asyncio

        delay_range = self.config.get("delay_range", [1, 3])
        if seconds is None:
            seconds = random.uniform(delay_range[0], delay_range[1])
        self.logger.debug(f"Async human delay: {seconds:.2f}s")
        await asyncio.sleep(seconds)

    def human_scroll(self, page: Page, direction: str = "down", steps: int = 3) -> None:
        viewport = page.viewport_size or {"width": 1920, "height": 1080}
        scroll_amount = int(viewport["height"] * 0.1)
        for i in range(steps):
            if direction == "down":
                page.evaluate(f"window.scrollBy(0, {scroll_amount})")
            else:
                page.evaluate(f"window.scrollBy(0, -{scroll_amount})")
            delay = random.uniform(0.5, 1.5)
            self.logger.debug(f"Scroll step {i+1}/{steps}, delay: {delay:.2f}s")
            time.sleep(delay)

    def human_mouse_move(self, page: Page, pattern: str = "random") -> None:
        viewport = page.viewport_size or {"width": 1920, "height": 1080}
        for _ in range(5):
            if pattern == "random":
                x = random.randint(0, viewport["width"])
                y = random.randint(0, viewport["height"])
            page.mouse.move(x, y)
            time.sleep(random.uniform(0.2, 0.8))

    def reload_config(self) -> None:
        """Hot-reload configuration from file"""
        self.logger.info("Reloading configuration...")
        self._load_config()
        self.logger.info("Configuration reloaded successfully")

    def export_config(self, export_path: str) -> None:
        """Export current configuration to file"""
        try:
            with open(export_path, "w") as f:
                json.dump(self.config, f, indent=2)
            self.logger.info(f"Configuration exported to {export_path}")
        except Exception as e:
            self.logger.error(f"Failed to export configuration: {e}")

    def import_config(self, import_path: str) -> None:
        """Import configuration from file"""
        try:
            with open(import_path, "r") as f:
                imported_config = json.load(f)
            self.config.update(imported_config)
            self._load_config()  # Re-validate and reload
            self.logger.info(f"Configuration imported from {import_path}")
        except Exception as e:
            self.logger.error(f"Failed to import configuration: {e}")

    def monitor_memory_usage(self) -> Dict[str, Any]:
        """Monitor memory usage and browser performance"""
        import psutil
        import os

        process = psutil.Process(os.getpid())
        memory_info = process.memory_info()

        stats = {
            "rss": memory_info.rss / 1024 / 1024,  # MB
            "vms": memory_info.vms / 1024 / 1024,  # MB
            "browser_active": self.browser is not None,
            "timestamp": time.time(),
        }

        self.logger.debug(f"Memory usage: {stats['rss']:.2f} MB RSS")
        return stats

    def cleanup_browser_cache(self) -> None:
        """Clean up browser cache and temporary files"""
        if self.browser:
            try:
                # Clear browser cache if supported
                self.logger.info("Clearing browser cache...")
                # Note: Playwright doesn't have direct cache clearing, but we can restart browser
                self.restart_browser()
            except Exception as e:
                self.logger.error(f"Failed to cleanup browser cache: {e}")

    def restart_browser(self) -> None:
        """Restart browser instance for fresh session"""
        if self.browser:
            try:
                self.browser.close()
                self.logger.info("Browser closed for restart")
            except Exception as e:
                self.logger.warning(f"Error closing browser: {e}")
            finally:
                self.browser = None
                self.playwright = None

        # Re-launch browser
        self.get_browser()
        self.logger.info("Browser restarted successfully")

    async def cleanup(self) -> None:
        """Cleanup all anti-bot components and resources."""
        try:
            self.logger.info("Starting AntibotManager cleanup...")

            # Cleanup anti-bot logger
            if self.antibot_logger and hasattr(self.antibot_logger, "cleanup"):
                await self.antibot_logger.cleanup()

            # Close browser
            if self.browser:
                try:
                    self.browser.close()
                    self.logger.debug("Browser closed during cleanup")
                except Exception as e:
                    self.logger.warning(f"Error closing browser during cleanup: {e}")
                finally:
                    self.browser = None

            # Clear caches
            if self.robots_checker and hasattr(self.robots_checker, "clear_cache"):
                self.robots_checker.clear_cache()

            self.logger.info("AntibotManager cleanup completed successfully")

        except Exception as e:
            self.logger.error(f"Error during AntibotManager cleanup: {e}")

    def enable_debug_mode(self, enable: bool = True) -> None:
        """Enable or disable debug mode with visual debugging options"""
        if enable:
            self.playwright_options["debug_mode"] = True
            self.playwright_options["headless"] = False
            self.playwright_options["slow_mo"] = (
                1000  # 1 second delay for step-by-step execution
            )
            self.playwright_options["devtools"] = True
            self.logger.info("Debug mode enabled with visual debugging")
        else:
            self.playwright_options["debug_mode"] = False
            self.playwright_options["headless"] = True
            self.playwright_options["slow_mo"] = 0
            self.playwright_options["devtools"] = False
            self.logger.info("Debug mode disabled")

    def take_debug_screenshot(self, page: Page, name: str) -> None:
        """Take screenshot for debugging purposes"""
        try:
            screenshot_path = f"debug_screenshot_{name}_{int(time.time())}.png"
            page.screenshot(path=screenshot_path)
            self.logger.info(f"Debug screenshot saved: {screenshot_path}")
        except Exception as e:
            self.logger.error(f"Failed to take debug screenshot: {e}")
