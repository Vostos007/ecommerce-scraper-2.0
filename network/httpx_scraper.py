"""
Modern HTTP scraper using httpx for better performance and HTTP/2 support.
Based on 2025 best practices for async web scraping.
"""

import asyncio
import logging
import time
import json
import re
import random
from datetime import UTC, datetime
from html import unescape
from itertools import product
from typing import Dict, List, Optional, Any, Tuple, Set, Callable
from urllib.parse import urlparse, urljoin
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from bs4 import BeautifulSoup

from utils.data_paths import COMPILED_DATA_ROOT, get_site_paths
from utils.export_writers import write_product_exports
from network.firecrawl_client import FirecrawlClient
from core.proxy_policy_manager import (
    ProxyFlowController,
    build_proxy_controller,
    load_proxy_policy_files,
    load_residential_proxy_list,
    BudgetStatus,
)

try:
    import httpx
except ImportError:
    raise ImportError("httpx is required but not installed. Run: pip install httpx")

from fake_useragent import UserAgent


@dataclass
class ScrapeMetrics:
    """Metrics for tracking scraping performance"""
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    total_time: float = 0.0
    response_times: List[float] = None

    def __post_init__(self):
        if self.response_times is None:
            self.response_times = []

    @property
    def success_rate(self) -> float:
        if self.total_requests == 0:
            return 0.0
        return self.successful_requests / self.total_requests

    @property
    def avg_response_time(self) -> float:
        if not self.response_times:
            return 0.0
        return sum(self.response_times) / len(self.response_times)


class ProxyUnavailableError(RuntimeError):
    """Raised when a proxy step can't acquire required resources."""


class ResidentialBudgetError(RuntimeError):
    """Raised when residential usage exceeds allowed quotas."""


class ModernHttpxScraper:
    """
    Modern HTTP scraper using httpx for high-performance async scraping.
    Follows 2025 best practices with connection pooling, HTTP/2 support, and retry logic.
    """

    def __init__(
        self,
        config_path: str = "config/settings.json",
        firecrawl_client: Optional[FirecrawlClient] = None,
    ):
        self.config_path = config_path
        self.config = self._load_config()
        self.httpx_config = self.config.get("httpx_scraper", {})
        self.logger = logging.getLogger(__name__)
        self.firecrawl_client = firecrawl_client

        # Metrics tracking
        self.metrics = ScrapeMetrics()
        self.last_scraped_products: List[Dict[str, Any]] = []

        # HTTPX client configuration cache
        self._client_config = self._get_httpx_client_config()

        # External progress hook (used for interactive debugging/monitoring)
        self._progress_hook: Optional[Callable[[str, Dict[str, Any]], None]] = None
        
        # User agent rotation
        try:
            self.ua = UserAgent()
        except Exception as e:
            self.logger.warning(f"Failed to initialize UserAgent: {e}")
            self.ua = None
            
        self.client: Optional[httpx.AsyncClient] = None
        
        # Initialize parsers
        try:
            from parsers.product_parser import ProductParser
            from parsers.variation.api import extract_variations
            from core.antibot_manager import AntibotManager
            from core.sitemap_analyzer import SitemapAnalyzer

            self.antibot = AntibotManager(config_path)
            self.analyzer = SitemapAnalyzer(self.antibot, "")
            self.product_parser = ProductParser(
                self.antibot,
                self.analyzer,
                html="",
                firecrawl_client=self.firecrawl_client,
            )
            self._extract_variations = extract_variations
        except Exception as exc:  # pragma: no cover - defensive fallback for minimal runs
            self.logger.warning(
                "Falling back to lightweight HTML parser due to initialization error: %s",
                exc,
            )
            self.antibot = SimpleNamespace()
            self.analyzer = SimpleNamespace(base_url="")
            self.product_parser = None
            self._extract_variations = None
        
        self.proxy_policy_data: Dict[str, Any] = {}
        self.proxy_flow: Optional[ProxyFlowController] = None
        self.bandwidth_config: Dict[str, Any] = {}
        self.fetch_policy_defaults: Dict[str, Any] = {}
        self._residential_proxy_cache: Optional[str] = None
        self._residential_proxy_list: List[str] = []
        self._proxy_policy_dir: Path | None = None
        self._init_proxy_policy()
        
        # Semaphore for concurrent requests
        max_concurrent = self.httpx_config.get("max_concurrent", 50)
        self.semaphore = asyncio.Semaphore(max_concurrent)

    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from JSON file"""
        try:
            with open(self.config_path, 'r') as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Failed to load config from {self.config_path}: {e}")
            return {}

    def _init_proxy_policy(self) -> None:
        """Initialize proxy orchestration policy and budgets."""

        policy_dir = (
            self.config.get("proxy_policy", {}).get("config_dir")
            if isinstance(self.config, dict)
            else None
        ) or "config/proxy"

        try:
            self._proxy_policy_dir = Path(policy_dir)
            self.proxy_policy_data = load_proxy_policy_files(policy_dir)
            self.bandwidth_config = (
                self.proxy_policy_data.get("global", {}).get(
                    "bandwidth_optimization", {}
                )
                or {}
            )
            self.fetch_policy_defaults = (
                self.proxy_policy_data.get("global", {}).get(
                    "fetch_defaults", {}
                )
                or {}
            )

            # Reuse Antibot controller when available to share budgets/state
            existing_flow: Optional[ProxyFlowController] = getattr(
                self.antibot, "proxy_flow", None
            )
            if existing_flow:
                self.proxy_flow = existing_flow
            else:
                proxies_cfg = self.proxy_policy_data.get("proxies", {}) or {}
                self.proxy_flow = ProxyFlowController(
                    global_config=self.proxy_policy_data.get("global", {}),
                    proxy_configs=proxies_cfg,
                    site_profiles=self.proxy_policy_data.get("sites", {}),
                    residential_config=proxies_cfg.get("residential", {}),
                )
        except FileNotFoundError:
            self.logger.warning(
                "Proxy policy directory %s not found; continuing without policy",
                policy_dir,
            )
            self.proxy_policy_data = {}
            self.proxy_flow = getattr(self.antibot, "proxy_flow", None)
        except Exception as exc:  # noqa: BLE001
            self.logger.error(f"Failed to load proxy policy from {policy_dir}: {exc}")
            self.proxy_policy_data = {}
            self.proxy_flow = getattr(self.antibot, "proxy_flow", None)

    def _get_httpx_client_config(self) -> Dict[str, Any]:
        """Build httpx client configuration from settings"""
        timeout_config = self.httpx_config.get("timeout", {})
        limits_config = self.httpx_config.get("limits", {})
        
        return {
            "timeout": httpx.Timeout(
                connect=timeout_config.get("connect", 10.0),
                read=timeout_config.get("read", 30.0),
                write=timeout_config.get("write", 30.0),
                pool=timeout_config.get("pool", 30.0)
            ),
            "limits": httpx.Limits(
                max_keepalive_connections=limits_config.get("max_keepalive_connections", 100),
                max_connections=limits_config.get("max_connections", 200),
                keepalive_expiry=limits_config.get("keepalive_expiry", 60.0)
            ),
            "http2": self.httpx_config.get("http2", False),
            "verify": self.httpx_config.get("verify_ssl", True),
            "follow_redirects": self.httpx_config.get("follow_redirects", True)
        }

    def _get_headers(self) -> Dict[str, str]:
        """Generate headers with optional user agent rotation"""
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }
        
        if self.ua and self.httpx_config.get("user_agent_rotation", True):
            try:
                headers["User-Agent"] = self.ua.random
            except Exception:
                headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        else:
            headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            
        return headers

    async def __aenter__(self):
        """Async context manager entry"""
        self.client = httpx.AsyncClient(**self._client_config)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        if self.client:
            await self.client.aclose()

    async def fetch_url(
        self, url: str, allow_redirect_resolution: bool = True
    ) -> Optional[Tuple[str, float, Dict[str, Any]]]:
        """Fetch URL using multi-step proxy orchestration when available."""

        if not self.client:
            raise RuntimeError("HTTP client not initialized. Use async context manager.")

        async with self.semaphore:
            if not self.proxy_flow:
                return await self._legacy_fetch_url(url, allow_redirect_resolution)

            domain = urlparse(url).netloc
            flow_state = self.proxy_flow.start_flow(domain)
            attempt = 0

            while True:
                step = flow_state.current_step
                attempt += 1
                self.metrics.total_requests += 1
                try:
                    html, response_time, metadata, bytes_used = (
                        await self._execute_transport_step(
                            step,
                            url,
                            domain,
                            allow_redirect_resolution,
                        )
                    )
                    metadata.setdefault("attempt", attempt)
                    metadata["transport_step"] = step
                    if bytes_used is not None:
                        metadata.setdefault("content_length", bytes_used)

                    self.metrics.successful_requests += 1
                    self.metrics.response_times.append(response_time)
                    flow_state.record_outcome("success")
                    return html, response_time, metadata

                except httpx.TooManyRedirects:
                    if allow_redirect_resolution:
                        resolved = await self._resolve_redirect_chain(url)
                        if resolved and resolved != url:
                            url = resolved
                            continue
                    self.metrics.failed_requests += 1
                    flow_state.record_outcome("fatal_error")
                    break
                except httpx.ConnectTimeout:
                    outcome = "connect_timeout"
                except httpx.ReadTimeout:
                    outcome = "http_timeout"
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code
                    outcome = (
                        f"http_{status}"
                        if status in {403, 429, 503}
                        else "fatal_error"
                    )
                except ResidentialBudgetError as exc:
                    self.logger.warning(
                        "Residential budget blocked for %s: %s", domain, exc
                    )
                    outcome = "fatal_error"
                except ProxyUnavailableError as exc:
                    self.logger.debug(
                        "Transport step %s unavailable for %s: %s",
                        step,
                        domain,
                        exc,
                    )
                    outcome = "proxy_unavailable"
                except Exception as exc:  # noqa: BLE001
                    self.logger.warning(
                        "Unhandled error on transport step %s for %s: %s",
                        step,
                        domain,
                        exc,
                    )
                    outcome = "fatal_error"

                self.metrics.failed_requests += 1
                flow_state.record_outcome(outcome)

                if flow_state.current_step == "unavailable" or not flow_state.remaining_steps:
                    break

        return None

    async def _legacy_fetch_url(
        self, url: str, allow_redirect_resolution: bool = True
    ) -> Optional[Tuple[str, float, Dict[str, Any]]]:
        """
        Fetch a single URL with retry logic and metrics tracking
        Returns: (html_content, response_time, metadata) or None if failed
        """
        if not self.client:
            raise RuntimeError("HTTP client not initialized. Use async context manager.")
            
        async with self.semaphore:
            start_time = time.time()
            retry_attempts = self.httpx_config.get("retry_attempts", 3)
            retry_delay = self.httpx_config.get("retry_delay", 1.0)
            
            for attempt in range(retry_attempts + 1):
                try:
                    self.metrics.total_requests += 1
                    self._emit_progress_hook(
                        "request_start",
                        {
                            "url": url,
                            "attempt": attempt + 1,
                            "total_attempts": retry_attempts + 1,
                        },
                    )
                    
                    headers = self._get_headers()
                    response = await self.client.get(url, headers=headers)
                    
                    response_time = time.time() - start_time
                    self.metrics.response_times.append(response_time)
                    
                    if response.status_code == 200:
                        self.metrics.successful_requests += 1
                        
                        metadata = {
                            "status_code": response.status_code,
                            "content_type": response.headers.get("content-type", ""),
                            "content_length": len(response.content),
                            "response_time": response_time,
                            "attempt": attempt + 1,
                            "url": url
                        }
                        
                        self.logger.debug(f"Successfully fetched {url} in {response_time:.2f}s")
                        self._emit_progress_hook(
                            "request_success",
                            {
                                "url": url,
                                "status": response.status_code,
                                "response_time": response_time,
                                "metadata": metadata,
                            },
                        )
                        return response.text, response_time, metadata
                    
                    elif response.status_code in [429, 503, 502, 504]:  # Rate limiting or server errors
                        if attempt < retry_attempts:
                            jitter = random.uniform(0.5, 1.5)
                            wait_time = retry_delay * (2 ** attempt) * jitter  # Exponential backoff + jitter
                            self.logger.warning(
                                "HTTP %s for %s, retrying in %.2fs",
                                response.status_code,
                                url,
                                wait_time,
                            )
                            await asyncio.sleep(wait_time)
                            continue
                    
                    self.logger.error(f"HTTP {response.status_code} for {url}")
                    self.metrics.failed_requests += 1
                    self._emit_progress_hook(
                        "request_error",
                        {
                            "url": url,
                            "status": response.status_code,
                        },
                    )
                    return None
                    
                except httpx.TooManyRedirects:
                    if allow_redirect_resolution:
                        resolved = await self._resolve_redirect_chain(url)
                        if resolved and resolved != url:
                            self.logger.debug(
                                "Resolved redirect loop %s -> %s", url, resolved
                            )
                            return await self._legacy_fetch_url(
                                resolved, allow_redirect_resolution=False
                            )
                    self.logger.error(f"Too many redirects for {url}")
                    self.metrics.failed_requests += 1
                    return None

                except httpx.TimeoutException:
                    if attempt < retry_attempts:
                        wait_time = retry_delay * (2 ** attempt) * random.uniform(0.5, 1.5)
                        self.logger.warning(
                            "Timeout for %s, attempt %s/%s; sleeping %.2fs",
                            url,
                            attempt + 1,
                            retry_attempts + 1,
                            wait_time,
                        )
                        await asyncio.sleep(wait_time)
                        continue
                    self.logger.error(f"Final timeout for {url}")
                    self.metrics.failed_requests += 1
                    self._emit_progress_hook(
                        "request_timeout",
                        {
                            "url": url,
                            "attempts": retry_attempts + 1,
                        },
                    )
                    return None
                    
                except Exception as e:
                    if attempt < retry_attempts:
                        wait_time = retry_delay * (2 ** attempt) * random.uniform(0.5, 1.5)
                        self.logger.warning(
                            "Error fetching %s: %s, retrying in %.2fs",
                            url,
                            e,
                            wait_time,
                        )
                        await asyncio.sleep(wait_time)
                        continue
                    self.logger.error(f"Final error fetching {url}: {e}")
                    self.metrics.failed_requests += 1
                    self._emit_progress_hook(
                        "request_failed",
                        {
                            "url": url,
                            "error": str(e),
                            "attempts": retry_attempts + 1,
                        },
                    )
                    return None
            
            return None

    async def _execute_transport_step(
        self,
        step: str,
        url: str,
        domain: str,
        allow_redirect_resolution: bool,
    ) -> Tuple[str, float, Dict[str, Any], int]:
        """Execute a single transport step from the proxy policy."""

        if step == "direct":
            return await self._httpx_request(
                url,
                domain,
                proxy=None,
                allow_redirect_resolution=allow_redirect_resolution,
                transport="direct",
            )

        if step == "datacenter_proxy":
            proxy_url: Optional[str] = None
            try:
                get_proxy = getattr(self.antibot, "get_validated_proxy", None)
                if get_proxy:
                    proxy_url = await get_proxy()
            except Exception as exc:  # noqa: BLE001
                raise ProxyUnavailableError(f"datacenter proxy rotation failed: {exc}")

            if not proxy_url:
                raise ProxyUnavailableError("no datacenter proxy available")

            return await self._httpx_request(
                url,
                domain,
                proxy=proxy_url,
                allow_redirect_resolution=allow_redirect_resolution,
                transport="datacenter",
            )

        if step == "antibot":
            make_request = getattr(self.antibot, "make_request_with_retry", None)
            if not make_request:
                raise ProxyUnavailableError("antibot manager not available")

            response = await make_request(url)
            if not response:
                raise ProxyUnavailableError("antibot pipeline returned no data")

            content = response.get("content", "")
            response_time = float(response.get("response_time", 0.0))
            bytes_used = len(content.encode("utf-8", errors="ignore"))
            metadata = {
                "status_code": response.get("status", 0),
                "content_type": (response.get("headers", {}) or {}).get(
                    "Content-Type", ""
                ),
                "proxy": response.get("proxy_used"),
            }
            budget_status = self._record_budget_usage(
                domain,
                bytes_used,
                transport="antibot",
            )
            if budget_status and budget_status.reason:
                metadata["budget_reason"] = budget_status.reason
            return content, response_time, metadata, bytes_used

        if step == "flaresolverr":
            solver_client = getattr(self.antibot, "flaresolverr_client", None)
            if not solver_client or not solver_client.is_enabled():
                raise ProxyUnavailableError("FlareSolverr is disabled")

            result = await solver_client.solve_get_request(url)
            if not result or "solution" not in result:
                raise ProxyUnavailableError("FlareSolverr returned no solution")

            solution = result.get("solution", {})
            content = solution.get("response", "")
            bytes_used = len(content.encode("utf-8", errors="ignore"))
            response_time = float(solution.get("loadTime", 0.0))
            metadata = {
                "status_code": solution.get("status", 0),
                "solver": "flaresolverr",
            }
            budget_status = self._record_budget_usage(
                domain,
                bytes_used,
                transport="flaresolverr",
            )
            if budget_status and budget_status.reason:
                metadata["budget_reason"] = budget_status.reason
            return content, response_time, metadata, bytes_used

        if step == "residential_burst":
            if not self.proxy_flow or not self.proxy_flow.can_use_residential(domain):
                raise ResidentialBudgetError("residential flow gated by controller")

            proxy_url = self._resolve_residential_proxy_url()
            if not proxy_url:
                raise ProxyUnavailableError("no residential gateway configured")

            html, response_time, metadata, bytes_used = await self._httpx_request(
                url,
                domain,
                proxy=proxy_url,
                allow_redirect_resolution=allow_redirect_resolution,
                transport="residential",
            )

            status = self.proxy_flow.record_residential_request(
                domain,
                bytes_used,
                now=datetime.now(UTC).replace(tzinfo=None),
            )
            if status.blocked:
                raise ResidentialBudgetError(status.reason or "residential hard limit")

            self.proxy_flow.residential_controller.record_success(domain)
            if status.reason:
                metadata["budget_reason"] = status.reason
            return html, response_time, metadata, bytes_used

        raise ProxyUnavailableError(f"Unknown transport step: {step}")

    async def _httpx_request(
        self,
        url: str,
        domain: str,
        *,
        proxy: Optional[str],
        allow_redirect_resolution: bool,
        transport: str,
    ) -> Tuple[str, float, Dict[str, Any], int]:
        """Perform an HTTPX request with optional streaming safeguards."""

        if not self.client:
            raise RuntimeError("HTTP client not initialized")

        headers = self._get_headers()
        request_kwargs: Dict[str, Any] = {"headers": headers}

        follow_redirects = self.httpx_config.get("follow_redirects", True)
        request_kwargs["follow_redirects"] = (
            follow_redirects and allow_redirect_resolution
        )

        start_time = time.time()
        bytes_downloaded = 0

        client = self.client
        temp_client: Optional[httpx.AsyncClient] = None
        if proxy:
            temp_client = httpx.AsyncClient(**self._client_config, proxy=proxy)
            client = temp_client

        try:
            if self.bandwidth_config.get("enable_streaming", True):
                chunk_size = max(1, int(self.bandwidth_config.get("chunk_size_kb", 64)))
                chunk_bytes = chunk_size * 1024
                markers = [
                    marker.lower()
                    for marker in self.bandwidth_config.get(
                        "early_termination_markers", []
                    )
                ]
                max_bytes = int(self.fetch_policy_defaults.get("max_html_bytes", 0))
                abort_on_large = self.bandwidth_config.get(
                    "abort_on_large_response", True
                )

                stream_kwargs = dict(request_kwargs)
                content_parts: List[str] = []
                async with client.stream("GET", url, **stream_kwargs) as response:
                    status_code = response.status_code
                    response.raise_for_status()

                    async for chunk in response.aiter_text(chunk_size=chunk_bytes):
                        content_parts.append(chunk)
                        bytes_downloaded += len(chunk.encode("utf-8", errors="ignore"))
                        lower_chunk = chunk.lower()
                        if markers and any(marker in lower_chunk for marker in markers):
                            break
                        if (
                            max_bytes
                            and abort_on_large
                            and bytes_downloaded >= max_bytes
                        ):
                            break

                    html = "".join(content_parts)
                    response_time = time.time() - start_time
                    metadata = {
                        "status_code": status_code,
                        "content_type": response.headers.get("content-type", ""),
                        "proxy": proxy,
                        "transport": transport,
                        "domain": domain,
                    }
                    budget_status = None
                    if transport != "residential":
                        budget_status = self._record_budget_usage(
                            domain,
                            bytes_downloaded,
                            transport=transport,
                        )
                    if budget_status and budget_status.reason:
                        metadata["budget_reason"] = budget_status.reason
                    return html, response_time, metadata, bytes_downloaded

            response = await client.get(url, **request_kwargs)
        except httpx.RequestError:
            raise
        finally:
            if temp_client is not None:
                await temp_client.aclose()

        status_code = response.status_code
        response.raise_for_status()
        html = response.text
        bytes_downloaded = len(response.content)
        response_time = time.time() - start_time
        metadata = {
            "status_code": status_code,
            "content_type": response.headers.get("content-type", ""),
            "proxy": proxy,
            "transport": transport,
            "domain": domain,
        }
        budget_status = None
        if transport != "residential":
            budget_status = self._record_budget_usage(
                domain,
                bytes_downloaded,
                transport=transport,
            )
        if budget_status and budget_status.reason:
            metadata["budget_reason"] = budget_status.reason
        return html, response_time, metadata, bytes_downloaded

    def _record_budget_usage(
        self, domain: str, bytes_used: int, transport: str
    ) -> Optional[BudgetStatus]:
        if not self.proxy_flow or bytes_used <= 0:
            return None

        if transport == "residential":
            return None

        proxy_type = "residential" if transport == "residential" else transport
        try:
            return self.proxy_flow.budget_manager.consume(
                site=domain,
                bytes_used=bytes_used,
                proxy_type=proxy_type,
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.debug(
                "Failed to record budget usage for %s (%s): %s",
                domain,
                transport,
                exc,
            )
            return None

    def _resolve_residential_proxy_url(self) -> Optional[str]:
        if self._residential_proxy_cache:
            return self._residential_proxy_cache

        residential_cfg = (
            self.proxy_policy_data.get("proxies", {}).get("residential")
            if self.proxy_policy_data
            else None
        )
        if not residential_cfg:
            return None

        endpoint = residential_cfg.get("endpoint")
        auth = residential_cfg.get("auth", {})
        if not endpoint:
            source_file = residential_cfg.get("source_file")
            if source_file:
                if not self._residential_proxy_list:
                    file_path = Path(source_file)
                    if not file_path.is_absolute() and self._proxy_policy_dir:
                        file_path = self._proxy_policy_dir / source_file
                    scheme = residential_cfg.get("scheme", "http")
                    self._residential_proxy_list = load_residential_proxy_list(
                        file_path, scheme=scheme
                    )
                if self._residential_proxy_list:
                    return random.choice(self._residential_proxy_list)
            return None

        try:
            parsed = urlparse(endpoint)
            username = auth.get("user")
            password = auth.get("pass")
            userinfo = ""
            if username and password:
                userinfo = f"{username}:{password}@"
            host = parsed.hostname or ""
            port = f":{parsed.port}" if parsed.port else ""
            proxy_url = f"{parsed.scheme}://{userinfo}{host}{port}"
            self._residential_proxy_cache = proxy_url
            return proxy_url
        except Exception as exc:  # noqa: BLE001
            self.logger.error(f"Failed to resolve residential proxy endpoint: {exc}")
            return None
    async def fetch_urls_batch(self, urls: List[str]) -> List[Optional[Tuple[str, float, Dict[str, Any]]]]:
        """
        Fetch multiple URLs concurrently with controlled concurrency
        """
        self.logger.info(f"Fetching {len(urls)} URLs with max {self.semaphore._value} concurrent requests")
        
        tasks = [self.fetch_url(url) for url in urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Handle exceptions in results
        processed_results = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                self.logger.error(f"Exception for URL {urls[i]}: {result}")
                processed_results.append(None)
            else:
                processed_results.append(result)
                
        return processed_results

    def _fallback_parse_product(self, html: str, url: str) -> Dict[str, Any]:
        """Very lightweight HTML parsing that works for generic pages."""
        soup = None
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            soup = None

        title = None
        if soup:
            title_tag = soup.find("h1") or soup.find("title")
            if title_tag:
                title = title_tag.get_text(strip=True)
        if not title:
            title_match = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
            if title_match:
                title = title_match.group(1).strip()

        if not title:
            og_match = re.search(
                r"<meta[^>]+property=[\"']og:title[\"'][^>]+content=[\"']([^\"']+)[\"']",
                html,
                re.IGNORECASE,
            )
            if og_match:
                title = og_match.group(1).strip()

        clean_title = title or url

        price = self._extract_price_from_html(html)
        stock_quantity = self._extract_stock_from_html(html, soup)
        in_stock = stock_quantity is None or stock_quantity > 0
        site_domain = urlparse(url).netloc

        return {
            "url": url,
            "name": clean_title,
            "price": price,
            "base_price": price,
            "in_stock": in_stock,
            "stock_quantity": stock_quantity if stock_quantity is not None else 0,
            "site_domain": site_domain,
            "scraped_at": datetime.now(UTC).isoformat(),
            "variations": [],
        }

    @staticmethod
    def _extract_price_from_html(html: str) -> float:
        """Attempt to locate a numeric price in the markup."""
        price_candidates = re.findall(r"(?:(?:price|cost)[^0-9]{0,15})?(\d+[\.,]?\d{0,2})", html, re.IGNORECASE)
        for candidate in price_candidates:
            try:
                normalized = float(candidate.replace(",", "."))
                if normalized > 0:
                    return round(normalized, 2)
            except ValueError:
                continue
        return 0.0

    def _parse_product(self, html: str, url: str) -> Optional[Dict[str, Any]]:
        """Try project-specific parser first, then fallback to generic parsing."""
        parsed: Optional[Dict[str, Any]] = None

        if self.product_parser:
            try:
                parsed = self.product_parser.parse_product_page(html, url)
            except Exception as exc:  # pragma: no cover - best effort resilience
                self.logger.debug(f"ProductParser failed for %s: %s", url, exc)
                parsed = None

        if not parsed:
            parsed = self._fallback_parse_product(html, url)

        if not parsed:
            return None

        domain = urlparse(url).netloc
        disable_variations = domain.endswith("atmospherestore.ru")

        vp_variations: List[Dict[str, Any]] = []
        if not disable_variations and self._extract_variations:
            try:
                vp_variations = (
                    self._extract_variations(
                        source=domain,
                        html=html,
                        url=url,
                        antibot=self.antibot,
                    )
                    or []
                )
            except Exception as exc:  # pragma: no cover
                self.logger.debug(f"VariationParser failed for %s: %s", url, exc)
                vp_variations = []

        base_price_numeric = 0.0
        if parsed.get("price") is not None:
            try:
                base_price_numeric = float(parsed.get("price", 0) or 0)
            except (TypeError, ValueError):
                base_price_numeric = 0.0

        html_variations: List[Dict[str, Any]] = []
        if not disable_variations:
            html_variations = self._extract_variations_from_html(
                html, base_price_numeric
            )

        if html_variations:
            variations = html_variations
        else:
            variations = vp_variations

        if disable_variations:
            parsed["variations"] = []
        elif variations:
            parsed["variations"] = variations
        else:
            parsed.setdefault("variations", [])

        price_value = parsed.get("price") or parsed.get("base_price")
        if price_value is None:
            extracted_price = self._extract_price_from_html(html)
            parsed["price"] = extracted_price
            parsed["base_price"] = extracted_price
        else:
            try:
                numeric_price = float(str(price_value).replace(",", "."))
            except (TypeError, ValueError):
                numeric_price = 0.0
            parsed["price"] = numeric_price
            parsed.setdefault("base_price", numeric_price)

        parsed.setdefault("url", url)
        parsed.setdefault("site_domain", urlparse(url).netloc)
        parsed.setdefault("scraped_at", datetime.now(UTC).isoformat())
        if parsed.get("variations"):
            parsed["in_stock"] = any(
                bool(v.get("in_stock"))
                or (v.get("stock") not in (None, 0, False))
                or (v.get("stock_quantity") not in (None, 0))
                for v in parsed["variations"]
            )
            quantity_candidates = []
            for v in parsed["variations"]:
                stock_value = v.get("stock_quantity") or v.get("stock")
                try:
                    if stock_value is not None:
                        quantity_candidates.append(int(stock_value))
                except (TypeError, ValueError):
                    continue
            if quantity_candidates:
                parsed["stock_quantity"] = max(quantity_candidates)
            if not parsed.get("price") or parsed.get("price") in (0, 0.0, "0"):
                first_price = next(
                    (v.get("price") for v in parsed["variations"] if v.get("price")),
                    0,
                )
                try:
                    parsed["price"] = round(float(first_price), 2) if first_price else 0.0
                except (TypeError, ValueError):
                    parsed["price"] = 0.0
            parsed.setdefault("base_price", parsed.get("price", 0.0))
        if "stock_quantity" not in parsed or parsed["stock_quantity"] in (None, ""):
            stock_fallback = self._extract_stock_from_html(html)
            if stock_fallback is not None:
                parsed["stock_quantity"] = stock_fallback
                parsed["in_stock"] = stock_fallback > 0

        parsed.setdefault("in_stock", True)
        parsed.setdefault("stock_quantity", 0)

        return parsed

    def _is_product_page(self, html: str) -> bool:
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            return False

        body = soup.find("body")
        if body and "single-product" in (body.get("class") or []):
            return True

        if soup.find("form", class_=re.compile("variations_form")):
            return True

        return False

    async def _discover_product_urls(self, base_url: str, max_items: int) -> List[str]:
        if max_items <= 1:
            return [base_url]

        queue: List[str] = [base_url]
        discovered: List[str] = []
        visited: set[str] = set()

        while queue and len(discovered) < max_items:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)

            fetched = await self.fetch_url(current)
            if not fetched:
                continue

            html, _, _ = fetched

            if self._is_product_page(html):
                if current not in discovered:
                    discovered.append(current)
                continue

            try:
                soup = BeautifulSoup(html, "html.parser")
            except Exception:
                soup = None

            if not soup:
                continue

            base_domain = urlparse(base_url).netloc

            for link in soup.select("li.product a[href], a.woocommerce-LoopProduct-link"):
                href = link.get("href")
                if not href:
                    continue
                absolute = urljoin(current, href).split("#")[0]
                if urlparse(absolute).netloc != base_domain:
                    continue
                if absolute not in discovered:
                    discovered.append(absolute)
                if len(discovered) >= max_items:
                    break

            if len(discovered) >= max_items:
                break

            for page_link in soup.select("a.page-numbers"):
                href = page_link.get("href")
                if not href:
                    continue
                absolute = urljoin(current, href).split("#")[0]
                if urlparse(absolute).netloc != base_domain:
                    continue
                if absolute not in visited and absolute not in queue:
                    queue.append(absolute)

        return discovered[:max_items]

    def _extract_variations_from_html(self, html: str, base_price: float) -> List[Dict[str, Any]]:
        variations: List[Dict[str, Any]] = []

        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            return variations

        structured_variations: List[Dict[str, Any]] = []

        form = soup.find("form", class_=re.compile("variations_form"))
        if form and form.get("data-product_variations"):
            data_attr = form.get("data-product_variations")
            try:
                decoded = json.loads(unescape(data_attr))
            except Exception as exc:
                self.logger.debug(f"Could not decode variations JSON: {exc}")
            else:
                for entry in decoded:
                    attrs = entry.get("attributes", {}) or {}
                    labels = []
                    for key, value in attrs.items():
                        if not value:
                            continue
                        clean_key = key.replace("attribute_", "")
                        labels.append(f"{clean_key}:{value}")
                    label = ", ".join(labels) or str(entry.get("variation_id", ""))

                    price_raw = (
                        entry.get("display_price")
                        or entry.get("price")
                        or entry.get("regular_price")
                        or 0
                    )
                    try:
                        price_value = round(float(price_raw), 2)
                    except (TypeError, ValueError):
                        price_value = base_price

                    stock_value = entry.get("stock_quantity") or entry.get("max_qty")
                    try:
                        stock_value = (
                            int(stock_value) if stock_value not in (None, "") else None
                        )
                    except (TypeError, ValueError):
                        stock_value = None

                    structured_variations.append(
                        {
                            "type": "options",
                            "value": label,
                            "price": price_value,
                            "stock_quantity": stock_value,
                            "sku": entry.get("sku"),
                            "variation_id": entry.get("variation_id"),
                        }
                    )

        select_nodes = soup.select('select[name^="option["]')
        option_groups: List[List[Dict[str, Any]]] = []

        if select_nodes:
            for select in select_nodes:
                label_node = select.find_previous(
                    "span", class_=re.compile("product-page__input-box-title")
                )
                label_text = (
                    label_node.get_text(strip=True).replace("*", "").strip()
                    if label_node
                    else "Опция"
                )

                option_variants: List[Dict[str, Any]] = []
                for option in select.find_all("option"):
                    option_id = option.get("value")
                    if not option_id:
                        continue
                    text_value = option.get_text(strip=True)
                    price_delta = option.get("data-price") or option.get("data-price-delta") or option.get("data_price")
                    try:
                        price_delta_val = float(price_delta) if price_delta else 0.0
                    except (TypeError, ValueError):
                        price_delta_val = 0.0

                    stock_raw = option.get("data-opt-quantity") or option.get(
                        "data-stock"
                    )
                    try:
                        stock_val = (
                            int(stock_raw)
                            if stock_raw not in (None, "", "∞")
                            else None
                        )
                    except (TypeError, ValueError):
                        stock_val = None

                    option_variants.append(
                        {
                            "label": label_text,
                            "value": text_value,
                            "option_id": option_id,
                            "price_delta": price_delta_val,
                            "stock": stock_val,
                        }
                    )

                if option_variants:
                    option_groups.append(option_variants)

        radio_inputs = soup.select(
            '.product-page__input-box input[type="radio"][name^="option"], '
            '.product-page__input-box input[type="checkbox"][name^="option"]'
        )

        if radio_inputs:
            grouped: Dict[str, List[Dict[str, Any]]] = {}
            for input_elem in radio_inputs:
                name = input_elem.get("name")
                if not name:
                    continue
                label_node = input_elem.find_previous(
                    "span", class_=re.compile("product-page__input-box-title")
                )
                label_text = (
                    label_node.get_text(strip=True).replace("*", "").strip()
                    if label_node
                    else "Опция"
                )

                label_for = input_elem.get("id")
                value_label = ""
                if label_for:
                    assoc_label = soup.find("label", {"for": label_for})
                    if assoc_label:
                        value_label = assoc_label.get("title") or assoc_label.get_text(
                            strip=True
                        )

                text_value = value_label or input_elem.get("value") or ""
                price_delta = input_elem.get("data-price") or input_elem.get(
                    "data-price-delta"
                )
                try:
                    price_delta_val = float(price_delta) if price_delta else 0.0
                except (TypeError, ValueError):
                    price_delta_val = 0.0

                stock_raw = input_elem.get("data-opt-quantity") or input_elem.get(
                    "data-stock"
                )
                try:
                    stock_val = (
                        int(stock_raw)
                        if stock_raw not in (None, "", "∞")
                        else None
                    )
                except (TypeError, ValueError):
                    stock_val = None

                grouped.setdefault(name, {"label": label_text, "options": []})
                grouped[name]["options"].append(
                    {
                        "label": label_text,
                        "value": text_value,
                        "option_id": input_elem.get("value") or label_for,
                        "price_delta": price_delta_val,
                        "stock": stock_val,
                    }
                )

            for group in grouped.values():
                if group["options"]:
                    option_groups.append(group["options"])

        if option_groups:
            for combination in product(*option_groups):
                combo_label = ", ".join(
                    f"{item['label']}: {item['value']}" for item in combination
                )
                price_value = round(
                    base_price + sum(item["price_delta"] for item in combination), 2
                )
                stock_candidates = [
                    item["stock"] for item in combination if item["stock"] is not None
                ]
                stock_value = min(stock_candidates) if stock_candidates else None

                structured_variations.append(
                    {
                        "type": "options",
                        "value": combo_label,
                        "price": price_value,
                        "stock_quantity": stock_value,
                        "option_codes": [item["option_id"] for item in combination],
                    }
                )

        # Deduplicate variations by label/value
        seen_variations: Set[str] = set()
        for item in structured_variations:
            key = f"{item.get('type')}::{item.get('value')}"
            if key in seen_variations:
                continue
            seen_variations.add(key)
            variations.append(item)

        return variations

    async def _resolve_redirect_chain(self, url: str) -> Optional[str]:
        headers = self._get_headers()
        try:
            async with httpx.AsyncClient(
                timeout=self.client.timeout,
                follow_redirects=False,
                headers=headers,
            ) as resolver:
                current = url
                visited: Set[str] = set()
                for _ in range(8):
                    if current in visited:
                        break
                    visited.add(current)
                    response = await resolver.get(current)
                    location = response.headers.get("Location")
                    if response.status_code in (301, 302, 303, 307, 308) and location:
                        next_url = urljoin(current, location)
                        if "product_id=" in next_url:
                            return next_url
                        if next_url == current:
                            return current
                        current = next_url
                        continue
                    return current
        except Exception as exc:
            self.logger.debug(f"Redirect resolution failed for %s: %s", url, exc)
            return None

        return None

    @staticmethod
    def _extract_stock_from_html(html: str, soup: Optional[BeautifulSoup] = None) -> Optional[int]:
        candidates_selectors = [
            "#quantityCountText",
            "span.quantityCountText",
            "span.products-full-list__status.status.instock",
            "span.stock span",  # WooCommerce-like
        ]

        text_value = None

        try:
            if soup is None:
                soup = BeautifulSoup(html, "html.parser")
        except Exception:
            soup = None

        if soup:
            for selector in candidates_selectors:
                node = soup.select_one(selector)
                if node:
                    text_value = node.get_text(strip=True)
                    break

        if not text_value:
            raw_match = re.search(
                r"quantityCountText[^>]*>([^<]+)<",
                html,
                re.IGNORECASE,
            )
            if raw_match:
                text_value = raw_match.group(1)

        if not text_value:
            return None

        digits = re.findall(r"\d+", text_value)
        if not digits:
            return None

        try:
            return int(digits[0])
        except (TypeError, ValueError):
            return None

    async def scrape_products(
        self,
        base_url: str,
        product_urls: List[str],
        email: str,
        *,
        progress_hook: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        """
        Main scraping method with integrated variation parsing
        """
        previous_hook = self._progress_hook
        self._progress_hook = progress_hook

        start_time = time.time()
        successful_products = 0
        variations_found = 0
        collected_products: List[Dict[str, Any]] = []
        failures: Dict[str, str] = {}

        target_urls = list(dict.fromkeys(product_urls))

        if target_urls and len(target_urls) == 1:
            normalized_target = target_urls[0].rstrip("/")
            if normalized_target == base_url.rstrip("/"):
                discovered_urls = await self._discover_product_urls(
                    base_url,
                    max_items=self.config.get("max_products_per_run", 50),
                )
                if discovered_urls:
                    target_urls = discovered_urls

        self.logger.info(
            "Starting httpx scraping of %s URLs with variation parsing", len(target_urls)
        )

        # Update analyzer base URL
        self.analyzer.base_url = base_url

        # Batch fetch all URLs
        results = await self.fetch_urls_batch(target_urls)

        # Process results with variation parsing
        for i, result in enumerate(results):
            if result:
                html, response_time, metadata = result
                product_url = target_urls[i]
                self._emit_progress_hook(
                    "parse_start",
                    {
                        "url": product_url,
                        "response_time": response_time,
                    },
                )
                try:
                    product_data = self._parse_product(html, product_url)

                    if not product_data:
                        self.logger.warning(f"Failed to parse product data from {product_url}")
                        failures[product_url] = 'parse_failed'
                        self._emit_progress_hook(
                            "parse_failed",
                            {
                                "url": product_url,
                                "reason": "parse_failed",
                            },
                        )
                        continue

                    successful_products += 1
                    variations_found += len(product_data.get('variations', []))
                    collected_products.append(product_data)
                    self.logger.debug(
                        "Parsed product %s with %s variations",
                        product_data.get('name', 'Unknown'),
                        len(product_data.get('variations', [])),
                    )
                    self._emit_progress_hook(
                        "parse_success",
                        {
                            "url": product_url,
                            "variations": len(product_data.get('variations', [])),
                        },
                    )

                except Exception as e:
                    self.logger.error(f"Error parsing product {product_url}: {e}")
                    failures[product_url] = str(e)
                    self._emit_progress_hook(
                        "parse_exception",
                        {
                            "url": product_url,
                            "error": str(e),
                        },
                    )

        total_time = time.time() - start_time
        self.metrics.total_time = total_time

        self.last_scraped_products = collected_products
        export_path: Optional[Path] = None
        export_path_excel: Optional[Path] = None
        if collected_products:
            domain = urlparse(base_url).netloc if base_url else ""
            export_path = None
            if domain:
                try:
                    site_paths = get_site_paths(domain)
                    export_path = site_paths.exports_dir / "httpx_latest.json"
                except Exception:  # noqa: BLE001
                    export_path = None

            if export_path is None:
                fallback_dir = COMPILED_DATA_ROOT / "httpx"
                fallback_dir.mkdir(parents=True, exist_ok=True)
                export_path = fallback_dir / "httpx_latest.json"

            artifacts = write_product_exports(collected_products, export_path)
            if artifacts.json_path:
                export_path = artifacts.json_path
            if artifacts.excel_path:
                export_path_excel = artifacts.excel_path

        result_payload = {
            "success": successful_products > 0,
            "scraped_products": successful_products,
            "variations": variations_found,
            "total_urls": len(target_urls),
            "processing_time": total_time,
            "success_rate": self.metrics.success_rate,
            "avg_response_time": self.metrics.avg_response_time,
            "method": "httpx_with_variations",
            "products": collected_products,
            "failures": failures,
            "export_path": str(export_path) if export_path else None,
            "export_path_excel": str(export_path_excel) if export_path_excel else None,
        }

        self._emit_progress_hook(
            "batch_complete",
            {
                "success": successful_products,
                "failures": len(failures),
                "processing_time": total_time,
            },
        )

        self._progress_hook = previous_hook

        return result_payload

    def get_metrics(self) -> ScrapeMetrics:
        """Get current scraping metrics"""
        return self.metrics

    def _emit_progress_hook(self, event: str, payload: Dict[str, Any]) -> None:
        if not self._progress_hook:
            return
        try:
            self._progress_hook(event, payload)
        except Exception as exc:  # pragma: no cover - debug aid only
            self.logger.debug("Progress hook error: %s", exc)
