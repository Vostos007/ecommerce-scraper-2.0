import asyncio
import aiohttp
import inspect
import random
from typing import List, Dict, Optional, Tuple, Any
from contextlib import asynccontextmanager
from fake_useragent import UserAgent
from curl_cffi import requests as curl_requests
from core.proxy_rotator import ProxyRotator
import time
import logging
import json
from dataclasses import dataclass, field
from collections import deque
from utils.system_monitor import SystemMonitor
from datetime import datetime, timedelta
import aiolimiter


@dataclass
class ScrapeMetrics:
    success_count: int = 0
    total_count: int = 0
    total_time: float = 0.0
    response_times: List[float] = field(default_factory=list)
    connection_reuses: int = 0  # Add connection reuse statistics
    dns_times: List[float] = field(default_factory=list)  # Track DNS resolution times
    rate_limit_encounters: int = 0  # Monitor rate limit encounters and recovery
    resource_checks: Dict[str, List[float]] = field(
        default_factory=lambda: {"cpu": [], "available_memory": []}
    )  # Include resource usage correlation with performance

    @property
    def success_rate(self) -> float:
        return (
            (self.success_count / self.total_count * 100) if self.total_count > 0 else 0
        )

    @property
    def avg_response_time(self) -> float:
        return (
            sum(self.response_times) / len(self.response_times)
            if self.response_times
            else 0
        )

    @property
    def avg_dns_time(self) -> float:
        return sum(self.dns_times) / len(self.dns_times) if self.dns_times else 0

    @property
    def avg_cpu_usage(self) -> float:
        return (
            sum(self.resource_checks["cpu"]) / len(self.resource_checks["cpu"])
            if self.resource_checks["cpu"]
            else 0
        )

    @property
    def avg_memory_available(self) -> float:
        return (
            sum(self.resource_checks["available_memory"])
            / len(self.resource_checks["available_memory"])
            if self.resource_checks["available_memory"]
            else 0
        )


class FastHeadlessScraper:
    def __init__(
        self,
        proxy_rotator: Optional[ProxyRotator] = None,
        max_concurrency: int = 30,
        rate_limit: int = 10,  # requests per second
        retry_attempts: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 10.0,
    ):
        self.proxy_rotator = proxy_rotator
        self.max_concurrency = max_concurrency
        self.rate_limit = rate_limit
        self.retry_attempts = retry_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay

        self._dynamic_semaphore = asyncio.Semaphore(max_concurrency)
        # _dynamic_semaphore that can be resized without breaking active requests
        self.rate_limiter = aiolimiter.AsyncLimiter(rate_limit, time_period=1)
        self.ua = UserAgent()
        self.session = None
        self.metrics = ScrapeMetrics()
        self.logger = logging.getLogger(__name__)
        self.verify_ssl = True
        self._rate_limit_detector = (
            deque()
        )  # _rate_limit_detector that monitors error patterns
        self._request_count_since_check = 0
        self._last_resource_check = 0
        self._dns_cache_ttl = 300
        self._connection_pool_size = 100
        self._keep_alive_timeout = 60
        self._request_timeout = 30
        self._requests_per_check = 50
        self._seconds_between_checks = 10
        self._cpu_threshold = 85
        self._memory_threshold = 500
        self._rate_limit_threshold = 5
        self._rate_limit_window = 60
        self._backoff_multiplier = 2.0
        self._min_request_delay = 0.1
        self._max_request_delay = 10.0
        self._domain_last_request = {}
        self._min_domain_delay = 0.1  # Simple per-domain rate limiting

        try:
            with open("config/settings.json", "r") as f:
                config = json.load(f)
                if "fast_scraper" in config:
                    fs_config = config["fast_scraper"]
                    self._dns_cache_ttl = fs_config.get("dns_cache_ttl", 300)
                    self._connection_pool_size = fs_config.get(
                        "connection_pool_size", 100
                    )
                    self._keep_alive_timeout = fs_config.get("keep_alive_timeout", 60)
                    self._request_timeout = fs_config.get(
                        "request_timeout", self._request_timeout
                    )
                if "resource_monitoring" in config:
                    rm_config = config["resource_monitoring"]
                    self._cpu_threshold = rm_config.get("cpu_threshold", 85)
                    self._memory_threshold = rm_config.get("memory_threshold_mb", 500)
                    self._requests_per_check = rm_config.get("requests_per_check", 50)
                    self._seconds_between_checks = rm_config.get(
                        "check_interval_seconds", 10
                    )
                if "rate_limit_protection" in config:
                    rlp_config = config["rate_limit_protection"]
                    self._rate_limit_threshold = rlp_config.get("max_429_per_minute", 5)
                    self._backoff_multiplier = rlp_config.get("backoff_multiplier", 2.0)
                    self._min_request_delay = rlp_config.get("min_request_delay", 0.1)
                    self._max_request_delay = rlp_config.get("max_request_delay", 10.0)
                if "fast_scraper" in config and "verify_ssl" in config["fast_scraper"]:
                    self.verify_ssl = config["fast_scraper"]["verify_ssl"]
        except FileNotFoundError:
            self.verify_ssl = True

    def adjust_concurrency(self, new_limit: int):
        """Update semaphore limits at runtime without breaking active requests."""
        if new_limit > 0:
            self.max_concurrency = new_limit
            # For asyncio.Semaphore, we can't resize directly, so recreate
            self._dynamic_semaphore = asyncio.Semaphore(new_limit)
            self.logger.info(f"Concurrency adjusted to {new_limit}")

    def get_current_concurrency(self) -> int:
        """Return active concurrent requests."""
        return (
            self.max_concurrency
        )  # Simplified, as Semaphore doesn't track active count directly

    def _check_rate_limits(self) -> bool:
        """Check if rate limit threshold exceeded in sliding window."""
        now = time.time()
        window_start = now - self._rate_limit_window
        self._rate_limit_detector = deque(
            [t for t in self._rate_limit_detector if t > window_start]
        )
        exceeded = len(self._rate_limit_detector) >= self._rate_limit_threshold
        if exceeded:
            self.logger.warning(
                f"Rate limit threshold exceeded: {len(self._rate_limit_detector)} errors in {self._rate_limit_window}s"
            )
            # Reduce concurrency
            new_limit = max(1, self.max_concurrency // 2)
            self.adjust_concurrency(new_limit)
            # Increase delays
            self.base_delay = min(
                self.max_delay, self.base_delay * self._backoff_multiplier
            )
        return not exceeded

    def _log_rate_limit_error(self):
        self._rate_limit_detector.append(time.time())
        self.metrics.rate_limit_encounters += 1

    async def _check_resources(self):
        """Check system resources and scale concurrency."""
        if self._request_count_since_check < self._requests_per_check:
            self._request_count_since_check += 1
            return
        self._request_count_since_check = 0

        now = time.time()
        if now - self._last_resource_check < self._seconds_between_checks:
            return
        self._last_resource_check = now

        cpu = SystemMonitor.get_cpu_usage()
        memory = SystemMonitor.get_memory_info()
        self.metrics.resource_checks["cpu"].append(cpu)
        self.metrics.resource_checks["available_memory"].append(memory)  # Available MB

        if cpu > self._cpu_threshold or memory < self._memory_threshold:
            self.logger.warning(
                f"High resource usage: CPU {cpu}%, Memory {memory}MB available"
            )
            new_limit = max(1, self.max_concurrency // 2)
            self.adjust_concurrency(new_limit)
        elif cpu < self._cpu_threshold * 0.7 and memory > self._memory_threshold * 1.5:
            # Scale back up gradually
            new_limit = min(self.max_concurrency * 2, 100)  # Cap at 100
            self.adjust_concurrency(new_limit)
            self.logger.info("Resources recovered, scaling up concurrency")

    def _exponential_backoff(self, attempt: int) -> float:
        """Exponential backoff with jitter."""
        base = self.base_delay * (self._backoff_multiplier**attempt)
        jitter = random.uniform(0, 1)
        delay = min(base + jitter, self._max_request_delay)
        return max(delay, self._min_request_delay)

    def _extract_domain(self, url: str) -> str:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        return parsed.netloc

    # Import and use utils.system_monitor.SystemMonitor (already imported at top)
    # Check system resources every N requests (configurable)
    # Automatically scale down concurrency when CPU/memory thresholds exceeded
    # Scale back up gradually when resources recover

    async def __aenter__(self):
        connector = aiohttp.TCPConnector(
            limit=self._connection_pool_size,
            limit_per_host=50,
            ttl_dns_cache=self._dns_cache_ttl,
            enable_cleanup_closed=True,
            force_close=False,
            keepalive_timeout=self._keep_alive_timeout,
        )
        timeout = aiohttp.ClientTimeout(total=30, connect=10)

        headers = {
            "User-Agent": self.ua.random,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }

        self.session = aiohttp.ClientSession(
            connector=connector, timeout=timeout, headers=headers
        )
        # Connection reuse tracking starts here
        self.metrics.connection_reuses = 0
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            close = getattr(self.session, "close", None)
            if callable(close):
                maybe_awaitable = close()
                if inspect.isawaitable(maybe_awaitable):
                    await maybe_awaitable

    def _get_proxy(self) -> Optional[str]:
        if self.proxy_rotator:
            # get_next_proxy() returns an iterator that yields exactly one proxy string
            return next(self.proxy_rotator.get_next_proxy())
        return None

    async def _get_headers(self) -> Dict[str, str]:
        return {
            "User-Agent": self.ua.random,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }

    async def fetch_url(
        self,
        url: str,
        semaphore: Optional[asyncio.Semaphore] = None,
        use_proxy: bool = True,
        use_curl: bool = False,
    ) -> Optional[Tuple[str, float, Dict]]:
        """
        Fetch single URL with rate limiting and proxy rotation.
        Returns (html, response_time, status) or None on failure.
        """
        if semaphore is None:
            semaphore = self._dynamic_semaphore

        # Resource check every N requests (configurable)
        await self._check_resources()

        # Rate limit protection: Track 429/403 errors in sliding window (last 60 seconds)
        # When threshold exceeded, automatically increase delays and reduce concurrency

        # Rate limit check
        if not self._check_rate_limits():
            await asyncio.sleep(self._exponential_backoff(0))

        start_time = time.time()
        dns_start = time.time()

        # Simple per-domain rate limiting
        domain = self._extract_domain(url)
        now = time.time()
        if domain in self._domain_last_request:
            elapsed = now - self._domain_last_request[domain]
            if elapsed < self._min_domain_delay:
                await asyncio.sleep(self._min_domain_delay - elapsed)

        for attempt in range(self.retry_attempts):
            try:
                async with semaphore:
                    proxy = None
                    if use_proxy and self.proxy_rotator:
                        proxy = self._get_proxy()
                        # Basic proxy validation
                        if proxy:
                            if not (
                                proxy.startswith("http://")
                                or proxy.startswith("https://")
                            ):
                                self.logger.warning(
                                    f"Invalid proxy format: {proxy} (missing scheme)"
                                )
                                proxy = None
                            else:
                                # Check for host:port
                                parsed = (
                                    proxy.split("://")[1] if "://" in proxy else proxy
                                )
                                if ":" not in parsed:
                                    self.logger.warning(
                                        f"Invalid proxy format: {proxy} (missing port)"
                                    )
                                    proxy = None
                                else:
                                    self.logger.debug(f"Using proxy: {proxy}")

                    headers = await self._get_headers()

                    async with self.rate_limiter:
                        if use_curl:
                            # Use curl_cffi for better TLS fingerprinting
                            def curl_get():
                                return curl_requests.get(
                                    url,
                                    proxies=(
                                        {"http": proxy, "https": proxy}
                                        if proxy
                                        else None
                                    ),
                                    headers=headers,
                                    impersonate="chrome110",
                                )

                            resp = await asyncio.to_thread(curl_get)
                            html = resp.text
                            status = resp.status_code
                        else:
                            # Use aiohttp
                            if self.session is None:
                                self.logger.error("Session not initialized")
                                return None
                            ssl_context = False if not self.verify_ssl else None

                            async def _perform_request() -> Tuple[int, str]:
                                async with self.session.get(
                                    url,
                                    proxy=proxy,
                                    headers=headers,
                                    allow_redirects=True,
                                    ssl=ssl_context,
                                ) as resp:
                                    status_local = resp.status
                                    html_local = await resp.text()
                                    try:
                                        if (
                                            resp.connection
                                            and hasattr(resp.connection, "keep_alive")
                                            and resp.connection.keep_alive
                                        ):
                                            self.metrics.connection_reuses += 1
                                    except Exception:
                                        pass
                                    return status_local, html_local

                            request_coro = _perform_request()
                            try:
                                status, html = await asyncio.wait_for(
                                    request_coro, timeout=self._request_timeout
                                )
                            except asyncio.TimeoutError:
                                request_coro.close()
                                raise

                    dns_time = time.time() - dns_start
                    self.metrics.dns_times.append(dns_time)

                    response_time = time.time() - start_time
                    self.metrics.total_count += 1
                    self.metrics.response_times.append(response_time)

                    if status == 200:
                        self.metrics.success_count += 1
                        self.metrics.total_time += response_time
                        self._domain_last_request[domain] = time.time()
                        return html, response_time, {"status": status}

                    # Handle redirects and errors
                    if status in [301, 302]:
                        pass  # Followed by allow_redirects
                    if status in [403, 429]:
                        self._log_rate_limit_error()
                        self.logger.warning(
                            f"HTTP {status} for {url}, attempt {attempt + 1}"
                        )
                        # Implement exponential backoff with jitter for rate-limited requests
                        if attempt < self.retry_attempts - 1:
                            delay = self._exponential_backoff(attempt)
                            await asyncio.sleep(delay)
                            continue
                    else:
                        self.logger.error(f"Failed to fetch {url}: HTTP {status}")
                        return None

            except asyncio.TimeoutError:
                self.logger.error(
                    f"Request timeout while fetching {url} after {attempt + 1} attempts"
                )
                raise
            except Exception as e:
                self.logger.error(
                    f"Exception fetching {url} (attempt {attempt + 1}): {e}"
                )
                if attempt < self.retry_attempts - 1:
                    delay = self._exponential_backoff(attempt)
                    await asyncio.sleep(delay)
                    continue
                return None

        return None

    async def fetch_urls_batch(
        self, urls: List[str], batch_size: int = 10, use_proxy: bool = True
    ) -> Dict[str, Optional[Tuple[str, float, Dict]]]:
        """
        Process URLs in batches with configurable concurrency.
        """
        results = {}
        semaphore = asyncio.Semaphore(batch_size)

        async def fetch_single(url):
            result = await self.fetch_url(url, semaphore, use_proxy)
            results[url] = result

        tasks = [fetch_single(url) for url in urls]
        await asyncio.gather(*tasks, return_exceptions=True)

        return results

    def is_js_heavy(self, html: str) -> bool:
        """
        Detect if site is JavaScript heavy by checking for common indicators.
        """
        js_indicators = [
            "<script",
            "react",
            "angular",
            "vue",
            "document.getElementById",
            "window.addEventListener",
            "fetch(",
            "XMLHttpRequest",
        ]

        html_lower = html.lower()
        js_count = sum(1 for indicator in js_indicators if indicator in html_lower)

        # If more than 3 indicators or large script tags
        script_tags = html_lower.count("<script")
        return js_count > 3 or script_tags > 5

    def get_metrics(self) -> ScrapeMetrics:
        return self.metrics

    def reset_metrics(self):
        self.metrics = ScrapeMetrics()

    async def scrape_products(self, base_url: str, product_urls: List[str], email: str) -> Dict[str, Any]:
        """
        Main scraping method compatible with ScraperEngine interface with variation parsing
        """
        import time
        from typing import Any
        
        start_time = time.time()
        successful_products = 0
        variations_found = 0
        collected_products: List[Dict[str, Any]] = []
        failures: Dict[str, str] = {}

        self.logger.info(f"Starting aiohttp scraping of {len(product_urls)} URLs with variation parsing")

        try:
            # Initialize required components for parsers
            from parsers.product_parser import ProductParser
            from parsers.variation.api import extract_variations
            from core.antibot_manager import AntibotManager
            from core.sitemap_analyzer import SitemapAnalyzer
            
            antibot = AntibotManager("config/settings.json")
            analyzer = SitemapAnalyzer(antibot, base_url)
            
            # Initialize parsers with required dependencies
            product_parser = ProductParser(antibot, analyzer, html="")
            extract = extract_variations

            # Use existing batch fetch functionality
            results = await self.fetch_urls_batch(product_urls)

            # Process results with variation parsing - results is a dict
            for product_url, result in results.items():
                if result:
                    html, response_time, metadata = result
                    
                    try:
                        # Parse product data
                        product_data = product_parser.parse_product_page(html, product_url)
                        
                        if product_data:
                            price_value = product_data.get('price')
                            if price_value is None:
                                self.logger.debug(
                                    f"Skipping product {product_url} due to missing price"
                                )
                                failures[product_url] = 'missing_price'
                                continue

                            # Extract variations using the enhanced variation parser
                            variations = extract(
                                source=urlparse(product_url).netloc,
                                html=html,
                                url=product_url,
                                antibot=antibot,
                            )

                            if not variations:
                                failures[product_url] = 'no_variations'
                                continue

                            successful_products += 1
                            variations_found += len(variations)
                            product_data['variations'] = variations
                            collected_products.append(product_data)
                            self.logger.debug(
                                f"Successfully parsed product: {product_data.get('title', 'Unknown')}"
                            )
                        else:
                            self.logger.warning(
                                f"Failed to parse product data from {product_url}"
                            )
                            failures[product_url] = 'parse_failed'
                            
                    except Exception as e:
                        self.logger.error(f"Error parsing product {product_url}: {e}")
                        failures[product_url] = str(e)

        except Exception as e:
            self.logger.error(f"Error initializing parsers: {e}")

        total_time = time.time() - start_time

        return {
            "success": successful_products > 0,
            "scraped_products": successful_products,
            "variations": variations_found,
            "total_urls": len(product_urls),
            "processing_time": total_time,
            "success_rate": self.metrics.success_rate,
            "avg_response_time": self.metrics.avg_response_time,
            "method": "aiohttp_with_variations",
            "products": collected_products,
            "failures": failures,
        }


# Example usage (for reference, not executed)
async def main():
    # proxy_rotator = ProxyRotator([])  # Assume initialized, commented to avoid import issues
    scraper = FastHeadlessScraper()
    async with scraper:
        urls = ["https://example.com", "https://httpbin.org"]
        results = await scraper.fetch_urls_batch(urls)
        print(results)
        metrics = scraper.get_metrics()
        print(f"Success rate: {metrics.success_rate}%")
        print(f"Avg response time: {metrics.avg_response_time:.2f}s")


# Backwards compatibility for legacy imports
FastScraper = FastHeadlessScraper
