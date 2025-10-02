"""
Comprehensive anti-bot logging system for tracking and analyzing scraping activities.

This module provides production-grade anti-bot logging capabilities with:
- Detailed request/response tracking and analysis
- Performance metrics and success rate monitoring
- CAPTCHA detection and solving statistics
- User-agent rotation effectiveness analysis
- Proxy performance and failure tracking
- Robots.txt compliance monitoring
- Real-time alerting and notifications
"""

import json
import time
import logging
from typing import Dict, Any, Optional
from datetime import datetime, timedelta
from urllib.parse import urlparse
import asyncio
from pathlib import Path
import statistics

logger = logging.getLogger(__name__)


class AntiBotLogger:
    """
    Comprehensive anti-bot logging system for tracking scraping activities.

    Features:
    - Request/response lifecycle tracking
    - Performance metrics and analysis
    - CAPTCHA detection and solving statistics
    - User-agent effectiveness monitoring
    - Proxy performance tracking
    - Robots.txt compliance logging
    - Real-time alerting and reporting
    """

    def __init__(self, config: Dict[str, Any]):
        """Initialize AntiBotLogger with configuration."""
        self.config = config
        self.enabled = config.get("enabled", True)
        self.log_level = config.get("log_level", "INFO").upper()

        # Handle nested log_destinations from settings.json while maintaining backward compatibility
        log_destinations = config.get("log_destinations", {})

        # File logging configuration
        file_config = log_destinations.get("file", {})
        self.log_to_file = file_config.get("enabled", config.get("log_to_file", True))

        # Console logging configuration
        console_config = log_destinations.get("console", {})
        self.log_to_console = console_config.get(
            "enabled", config.get("log_to_console", True)
        )
        self.colored_output = console_config.get("colored_output", True)

        # JSON file logging configuration
        json_config = log_destinations.get("json_file", {})
        self.json_logging_enabled = json_config.get("enabled", True)
        self.json_log_path = json_config.get("path", "logs/antibot_structured.jsonl")

        # Logging destinations - handle both new nested and old flat structure
        self.logging_config = config.get("logging_destinations", log_destinations)

        # Set base log directory from various possible locations
        base_log_path = (
            file_config.get("path", "")
            or self.logging_config.get("base_directory", "")
            or "logs/antibot"
        )
        # Extract directory from file path if it's a full path
        if "/" in base_log_path and base_log_path.endswith(".log"):
            base_log_path = str(Path(base_log_path).parent)
        elif not base_log_path.endswith("/antibot"):
            base_log_path = "logs/antibot"

        self.base_log_dir = Path(base_log_path)
        self.base_log_dir.mkdir(parents=True, exist_ok=True)

        # Log rotation and retention - handle nested file config
        self.rotation_config = config.get("log_rotation", {})
        self.max_log_size_mb = file_config.get(
            "max_size_mb"
        ) or self.rotation_config.get("max_size_mb", 100)
        self.retention_days = self.rotation_config.get("retention_days", 30)
        self.compress_old_logs = self.rotation_config.get("compress_old_logs", True)
        self.backup_count = file_config.get("backup_count", 5)

        # Performance tracking
        self.performance_config = config.get("performance_tracking", {})
        self.track_response_times = self.performance_config.get(
            "track_response_times", True
        )
        self.track_success_rates = self.performance_config.get(
            "track_success_rates", True
        )
        self.track_error_patterns = self.performance_config.get(
            "track_error_patterns", True
        )

        # Metrics aggregation configuration - handle nested structure
        self.metrics_config = config.get("metrics_aggregation", {})
        self.metrics_enabled = self.metrics_config.get("enabled", True)
        self.flush_interval_seconds = self.metrics_config.get(
            "flush_interval_seconds", 60
        )
        self.include_trends = self.metrics_config.get("include_trends", True)

        # Alerting configuration - handle nested alert_thresholds in metrics_aggregation
        self.alerting_config = config.get("alerting", {})
        nested_alert_thresholds = self.metrics_config.get("alert_thresholds", {})

        # Combine alerting configs with nested taking precedence
        self.enable_alerts = self.alerting_config.get("enabled", True)
        legacy_thresholds = self.alerting_config.get("thresholds", {})
        self.alert_thresholds = {**legacy_thresholds, **nested_alert_thresholds}

        # Set threshold values with nested structure taking precedence
        self.failure_rate_threshold = (
            nested_alert_thresholds.get("error_rate_percent")
            or nested_alert_thresholds.get("failure_rate_percent")
            or legacy_thresholds.get("failure_rate_percent", 50.0)
        )
        self.captcha_rate_threshold = nested_alert_thresholds.get(
            "captcha_rate_percent"
        ) or legacy_thresholds.get("captcha_rate_percent", 20.0)
        self.block_rate_threshold = nested_alert_thresholds.get(
            "block_rate_percent"
        ) or legacy_thresholds.get("block_rate_percent", 5.0)
        self.slow_response_threshold = legacy_thresholds.get(
            "slow_response_seconds", 10.0
        )

        # Metrics storage
        self.request_metrics = {
            "total_requests": 0,
            "successful_requests": 0,
            "failed_requests": 0,
            "blocked_requests": 0,
            "captcha_requests": 0,
            "robots_blocked_requests": 0,
            "total_response_time": 0.0,
            "avg_response_time": 0.0,
            "session_start_time": time.time(),
        }

        # Component-specific metrics
        self.component_metrics = {
            "captcha_solver": {
                "detection_count": 0,
                "solve_attempts": 0,
                "solve_successes": 0,
                "solve_failures": 0,
                "total_solve_time": 0.0,
                "avg_solve_time": 0.0,
                "total_cost": 0.0,
            },
            "user_agent_rotator": {
                "rotations": 0,
                "unique_agents_used": set(),
                "effectiveness_scores": [],
                "domain_preferences": {},
            },
            "robots_checker": {
                "compliance_checks": 0,
                "allowed_requests": 0,
                "blocked_requests": 0,
                "crawl_delays_applied": 0,
                "total_delay_time": 0.0,
            },
            "proxy_rotator": {
                "rotations": 0,
                "unique_proxies_used": set(),
                "proxy_failures": {},
                "proxy_response_times": {},
                "burned_proxies": set(),
            },
        }

        # Recent activity logs for analysis
        self.recent_requests = []
        self.recent_errors = []
        self.recent_captchas = []
        self.max_recent_items = config.get("max_recent_items", 1000)

        # File handlers
        self.log_files = {}
        self._setup_log_files()

        # Background tasks - defer initialization
        self.cleanup_task = None
        self.metrics_task = None
        self._background_tasks_started = False

    async def start(self) -> None:
        """Start the AntiBotLogger and initialize background tasks."""
        if not self._background_tasks_started and self.enabled:
            try:
                self.cleanup_task = asyncio.create_task(self._background_cleanup())
                if self.metrics_enabled:
                    self.metrics_task = asyncio.create_task(
                        self._background_metrics_flush()
                    )
                self._background_tasks_started = True
            except Exception as e:
                logger.error(f"Error starting AntiBotLogger background tasks: {e}")

    async def log_request_start(
        self,
        url: str,
        method: str = "GET",
        user_agent: str = None,
        proxy: Optional[str] = None,
        request_id: str = None,
    ) -> str:
        """
        Log the start of a request with comprehensive tracking.

        Args:
            url: Request URL
            method: HTTP method
            user_agent: User agent used
            proxy: Proxy used
            request_id: Optional request ID for tracking

        Returns:
            Request ID for tracking throughout lifecycle
        """
        if not self.enabled:
            return request_id or str(time.time())

        request_id = request_id or f"req_{int(time.time() * 1000)}"
        start_time = time.time()

        request_data = {
            "request_id": request_id,
            "timestamp": datetime.now().isoformat(),
            "url": url,
            "domain": urlparse(url).netloc,
            "method": method,
            "user_agent": (
                user_agent[:100] + "..."
                if user_agent and len(user_agent) > 100
                else user_agent
            ),
            "proxy": proxy or "unknown",
            "start_time": start_time,
            "status": "started",
        }

        # Log to file
        if self.log_to_file:
            await self._write_to_log("requests", request_data)

        # Update metrics
        self.request_metrics["total_requests"] += 1

        # Store for analysis
        self.recent_requests.append(request_data)
        if len(self.recent_requests) > self.max_recent_items:
            self.recent_requests.pop(0)

        logger.debug(f"Request started: {request_id} - {method} {url}")
        return request_id

    async def log_request_complete(
        self,
        request_id: str,
        status_code: int,
        response_time: float,
        content_length: int = None,
        error: Optional[str] = None,
        blocked: bool = False,
        captcha_detected: bool = False,
    ) -> None:
        """
        Log request completion with comprehensive metrics.

        Args:
            request_id: Request ID from log_request_start
            status_code: HTTP status code
            response_time: Response time in seconds
            content_length: Response content length
            error: Error message if failed
            blocked: Whether request was blocked
            captcha_detected: Whether CAPTCHA was detected
        """
        if not self.enabled:
            return

        completion_data = {
            "request_id": request_id,
            "timestamp": datetime.now().isoformat(),
            "status_code": status_code,
            "response_time": response_time,
            "content_length": content_length,
            "error": error or "",
            "blocked": blocked,
            "captcha_detected": captcha_detected,
            "status": "completed",
        }

        # Update metrics
        self.request_metrics["total_response_time"] += response_time

        if 200 <= status_code < 300 and not error and not blocked:
            self.request_metrics["successful_requests"] += 1
        else:
            self.request_metrics["failed_requests"] += 1

        if blocked:
            self.request_metrics["blocked_requests"] += 1

        if captcha_detected:
            self.request_metrics["captcha_requests"] += 1

        # Calculate average response time
        total_requests = self.request_metrics["total_requests"]
        if total_requests > 0:
            self.request_metrics["avg_response_time"] = (
                self.request_metrics["total_response_time"] / total_requests
            )

        # Log to file
        if self.log_to_file:
            await self._write_to_log("requests", completion_data)

        # Check for alerts
        await self._check_alerts(completion_data)

        logger.debug(
            f"Request completed: {request_id} - {status_code} in {response_time:.2f}s"
        )

    async def log_captcha_detection(
        self,
        url: str,
        captcha_type: str,
        site_key: Optional[str] = None,
        action: Optional[str] = None,
        confidence: Optional[float] = None,
    ) -> None:
        """
        Log CAPTCHA detection with detailed information.

        Args:
            url: URL where CAPTCHA was detected
            captcha_type: Type of CAPTCHA (recaptcha_v2, recaptcha_v3, hcaptcha, image)
            site_key: CAPTCHA site key if available
            action: reCAPTCHA v3 action if applicable
            confidence: Detection confidence score
        """
        if not self.enabled:
            return

        captcha_data = {
            "timestamp": datetime.now().isoformat(),
            "url": url,
            "domain": urlparse(url).netloc,
            "captcha_type": captcha_type,
            "site_key": site_key or "unknown",
            "action": action or "unknown",
            "confidence": confidence if confidence is not None else 0.0,
            "event_type": "captcha_detected",
        }

        # Update component metrics
        self.component_metrics["captcha_solver"]["detection_count"] += 1

        # Log to file
        if self.log_to_file:
            await self._write_to_log("captcha", captcha_data)

        # Store for analysis
        self.recent_captchas.append(captcha_data)
        if len(self.recent_captchas) > self.max_recent_items:
            self.recent_captchas.pop(0)

        logger.info(f"CAPTCHA detected: {captcha_type} on {urlparse(url).netloc}")

    async def log_captcha_solve_attempt(
        self,
        captcha_type: str,
        solve_time: float,
        success: bool,
        cost: Optional[float] = None,
        error: Optional[str] = None,
    ) -> None:
        """
        Log CAPTCHA solving attempt with results.

        Args:
            captcha_type: Type of CAPTCHA solved
            solve_time: Time taken to solve in seconds
            success: Whether solving was successful
            cost: Cost of solving in USD
            error: Error message if failed
        """
        if not self.enabled:
            return

        solve_data = {
            "timestamp": datetime.now().isoformat(),
            "captcha_type": captcha_type,
            "solve_time": solve_time,
            "success": success,
            "cost": cost if cost is not None else 0.0,
            "error": error or "",
            "event_type": "captcha_solve_attempt",
        }

        # Update component metrics
        captcha_metrics = self.component_metrics["captcha_solver"]
        captcha_metrics["solve_attempts"] += 1
        captcha_metrics["total_solve_time"] += solve_time

        if success:
            captcha_metrics["solve_successes"] += 1
        else:
            captcha_metrics["solve_failures"] += 1

        if cost is not None:
            captcha_metrics["total_cost"] += cost

        # Calculate average solve time
        if captcha_metrics["solve_attempts"] > 0:
            captcha_metrics["avg_solve_time"] = (
                captcha_metrics["total_solve_time"] / captcha_metrics["solve_attempts"]
            )

        # Log to file
        if self.log_to_file:
            await self._write_to_log("captcha", solve_data)

        logger.info(
            f"CAPTCHA solve attempt: {captcha_type} - {'SUCCESS' if success else 'FAILED'} in {solve_time:.2f}s"
        )

    async def log_user_agent_rotation(
        self,
        old_ua: str,
        new_ua: str,
        domain: str = None,
        strategy: str = None,
        effectiveness_score: float = None,
    ) -> None:
        """
        Log user agent rotation with effectiveness tracking.

        Args:
            old_ua: Previous user agent
            new_ua: New user agent
            domain: Domain for rotation
            strategy: Rotation strategy used
            effectiveness_score: Effectiveness score of the rotation
        """
        if not self.enabled:
            return

        rotation_data = {
            "timestamp": datetime.now().isoformat(),
            "old_user_agent": (
                old_ua[:100] + "..." if old_ua and len(old_ua) > 100 else old_ua
            ),
            "new_user_agent": (
                new_ua[:100] + "..." if new_ua and len(new_ua) > 100 else new_ua
            ),
            "domain": domain,
            "strategy": strategy,
            "effectiveness_score": effectiveness_score,
            "event_type": "user_agent_rotation",
        }

        # Update component metrics
        ua_metrics = self.component_metrics["user_agent_rotator"]
        ua_metrics["rotations"] += 1
        ua_metrics["unique_agents_used"].add(new_ua)

        if effectiveness_score is not None:
            ua_metrics["effectiveness_scores"].append(effectiveness_score)

        if domain:
            if domain not in ua_metrics["domain_preferences"]:
                ua_metrics["domain_preferences"][domain] = []
            ua_metrics["domain_preferences"][domain].append(new_ua)

        # Log to file
        if self.log_to_file:
            await self._write_to_log("user_agents", rotation_data)

        logger.debug(f"User agent rotated for {domain or 'global'}: {strategy}")

    async def log_robots_compliance_check(
        self, url: str, user_agent: str, allowed: bool, crawl_delay: float, reason: str
    ) -> None:
        """
        Log robots.txt compliance check results.

        Args:
            url: URL that was checked
            user_agent: User agent used for check
            allowed: Whether URL is allowed
            crawl_delay: Required crawl delay
            reason: Reason for the decision
        """
        if not self.enabled:
            return

        compliance_data = {
            "timestamp": datetime.now().isoformat(),
            "url": url,
            "domain": urlparse(url).netloc,
            "user_agent": user_agent,
            "allowed": allowed,
            "crawl_delay": crawl_delay,
            "reason": reason,
            "event_type": "robots_compliance_check",
        }

        # Update component metrics
        robots_metrics = self.component_metrics["robots_checker"]
        robots_metrics["compliance_checks"] += 1

        if allowed:
            robots_metrics["allowed_requests"] += 1
        else:
            robots_metrics["blocked_requests"] += 1

        if crawl_delay > 0:
            robots_metrics["crawl_delays_applied"] += 1
            robots_metrics["total_delay_time"] += crawl_delay

        # Log to file
        if self.log_to_file:
            await self._write_to_log("robots", compliance_data)

        logger.debug(
            f"Robots compliance check: {url} - {'ALLOWED' if allowed else 'BLOCKED'}"
        )

    async def log_proxy_rotation(
        self,
        old_proxy: str,
        new_proxy: str,
        reason: str,
        performance_score: float = None,
    ) -> None:
        """
        Log proxy rotation with performance tracking.

        Args:
            old_proxy: Previous proxy
            new_proxy: New proxy
            reason: Reason for rotation
            performance_score: Performance score of the old proxy
        """
        if not self.enabled:
            return

        rotation_data = {
            "timestamp": datetime.now().isoformat(),
            "old_proxy": old_proxy,
            "new_proxy": new_proxy,
            "reason": reason,
            "performance_score": performance_score,
            "event_type": "proxy_rotation",
        }

        # Update component metrics
        proxy_metrics = self.component_metrics["proxy_rotator"]
        proxy_metrics["rotations"] += 1
        proxy_metrics["unique_proxies_used"].add(new_proxy)

        if performance_score is not None and old_proxy:
            if old_proxy not in proxy_metrics["proxy_response_times"]:
                proxy_metrics["proxy_response_times"][old_proxy] = []
            proxy_metrics["proxy_response_times"][old_proxy].append(performance_score)

        # Log to file
        if self.log_to_file:
            await self._write_to_log("proxies", rotation_data)

        logger.debug(f"Proxy rotated: {reason}")

    async def log_proxy_failure(
        self, proxy: str, error_type: str, error_message: str, burned: bool = False
    ) -> None:
        """
        Log proxy failure with categorization.

        Args:
            proxy: Failed proxy
            error_type: Type of error (timeout, blocked, network, etc.)
            error_message: Detailed error message
            burned: Whether proxy should be considered burned
        """
        if not self.enabled:
            return

        failure_data = {
            "timestamp": datetime.now().isoformat(),
            "proxy": proxy,
            "error_type": error_type,
            "error_message": error_message,
            "burned": burned,
            "event_type": "proxy_failure",
        }

        # Update component metrics
        proxy_metrics = self.component_metrics["proxy_rotator"]

        if proxy not in proxy_metrics["proxy_failures"]:
            proxy_metrics["proxy_failures"][proxy] = {}

        if error_type not in proxy_metrics["proxy_failures"][proxy]:
            proxy_metrics["proxy_failures"][proxy][error_type] = 0

        proxy_metrics["proxy_failures"][proxy][error_type] += 1

        if burned:
            proxy_metrics["burned_proxies"].add(proxy)

        # Log to file
        if self.log_to_file:
            await self._write_to_log("proxies", failure_data)

        # Store for analysis
        self.recent_errors.append(failure_data)
        if len(self.recent_errors) > self.max_recent_items:
            self.recent_errors.pop(0)

        logger.warning(
            f"Proxy failure: {proxy} - {error_type} ({'BURNED' if burned else 'RETRY'})"
        )

    def get_comprehensive_statistics(self) -> Dict[str, Any]:
        """
        Get comprehensive anti-bot logging statistics.

        Returns:
            Dictionary with detailed statistics across all components
        """
        current_time = time.time()
        session_duration = current_time - self.request_metrics["session_start_time"]

        # Calculate rates
        total_requests = self.request_metrics["total_requests"]
        success_rate = 0.0
        failure_rate = 0.0
        captcha_rate = 0.0

        if total_requests > 0:
            success_rate = (
                self.request_metrics["successful_requests"] / total_requests
            ) * 100
            failure_rate = (
                self.request_metrics["failed_requests"] / total_requests
            ) * 100
            captcha_rate = (
                self.request_metrics["captcha_requests"] / total_requests
            ) * 100

        # CAPTCHA solver stats
        captcha_solve_rate = 0.0
        captcha_metrics = self.component_metrics["captcha_solver"]
        if captcha_metrics["solve_attempts"] > 0:
            captcha_solve_rate = (
                captcha_metrics["solve_successes"] / captcha_metrics["solve_attempts"]
            ) * 100

        # User agent effectiveness
        ua_metrics = self.component_metrics["user_agent_rotator"]
        avg_ua_effectiveness = 0.0
        if ua_metrics["effectiveness_scores"]:
            avg_ua_effectiveness = statistics.mean(ua_metrics["effectiveness_scores"])

        # Proxy performance
        proxy_metrics = self.component_metrics["proxy_rotator"]
        total_proxy_failures = sum(
            sum(failures.values())
            for failures in proxy_metrics["proxy_failures"].values()
        )

        return {
            "session_info": {
                "duration_seconds": session_duration,
                "duration_formatted": self._format_duration(session_duration),
                "enabled": self.enabled,
                "log_level": self.log_level,
            },
            "request_statistics": {
                "total_requests": total_requests,
                "successful_requests": self.request_metrics["successful_requests"],
                "failed_requests": self.request_metrics["failed_requests"],
                "blocked_requests": self.request_metrics["blocked_requests"],
                "captcha_requests": self.request_metrics["captcha_requests"],
                "robots_blocked_requests": self.request_metrics[
                    "robots_blocked_requests"
                ],
                "success_rate_percent": round(success_rate, 2),
                "failure_rate_percent": round(failure_rate, 2),
                "captcha_rate_percent": round(captcha_rate, 2),
                "avg_response_time_seconds": round(
                    self.request_metrics["avg_response_time"], 3
                ),
            },
            "captcha_solver_stats": {
                "detections": captcha_metrics["detection_count"],
                "solve_attempts": captcha_metrics["solve_attempts"],
                "solve_successes": captcha_metrics["solve_successes"],
                "solve_failures": captcha_metrics["solve_failures"],
                "solve_rate_percent": round(captcha_solve_rate, 2),
                "avg_solve_time_seconds": round(captcha_metrics["avg_solve_time"], 2),
                "total_cost_usd": round(captcha_metrics["total_cost"], 4),
            },
            "user_agent_stats": {
                "total_rotations": ua_metrics["rotations"],
                "unique_agents_used": len(ua_metrics["unique_agents_used"]),
                "avg_effectiveness_score": round(avg_ua_effectiveness, 3),
                "domains_with_preferences": len(ua_metrics["domain_preferences"]),
            },
            "robots_compliance_stats": {
                "total_checks": self.component_metrics["robots_checker"][
                    "compliance_checks"
                ],
                "allowed_requests": self.component_metrics["robots_checker"][
                    "allowed_requests"
                ],
                "blocked_requests": self.component_metrics["robots_checker"][
                    "blocked_requests"
                ],
                "crawl_delays_applied": self.component_metrics["robots_checker"][
                    "crawl_delays_applied"
                ],
                "total_delay_time_seconds": round(
                    self.component_metrics["robots_checker"]["total_delay_time"], 2
                ),
            },
            "proxy_stats": {
                "total_rotations": proxy_metrics["rotations"],
                "unique_proxies_used": len(proxy_metrics["unique_proxies_used"]),
                "total_failures": total_proxy_failures,
                "burned_proxies": len(proxy_metrics["burned_proxies"]),
                "failure_breakdown": proxy_metrics["proxy_failures"],
            },
        }

    # Private helper methods

    def _setup_log_files(self) -> None:
        """Setup log file handlers for different log types."""
        if not self.log_to_file and not self.json_logging_enabled:
            return

        log_types = [
            "requests",
            "captcha",
            "user_agents",
            "robots",
            "proxies",
            "alerts",
            "metrics",
        ]

        for log_type in log_types:
            if self.log_to_file:
                log_file_path = self.base_log_dir / f"{log_type}.jsonl"
                self.log_files[log_type] = log_file_path

        # Setup structured JSON logging if enabled
        if self.json_logging_enabled:
            self.structured_log_path = Path(self.json_log_path)
            self.structured_log_path.parent.mkdir(parents=True, exist_ok=True)

    async def _write_to_log(self, log_type: str, data: Dict[str, Any]) -> None:
        """Write data to appropriate log file."""
        try:
            # Convert sets to lists for JSON serialization
            serializable_data = self._make_json_serializable(data)

            # Write to specific log file if file logging is enabled
            if self.log_to_file and log_type in self.log_files:
                log_file_path = self.log_files[log_type]
                with open(log_file_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(serializable_data) + "\n")

            # Write to structured JSON log if enabled
            if self.json_logging_enabled and hasattr(self, "structured_log_path"):
                enriched_data = {**serializable_data, "log_type": log_type}
                with open(self.structured_log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(enriched_data) + "\n")

        except Exception as e:
            logger.error(f"Error writing to log file {log_type}: {e}")

    def _make_json_serializable(self, obj: Any) -> Any:
        """Convert object to JSON-serializable format."""
        if isinstance(obj, set):
            return list(obj)
        elif isinstance(obj, dict):
            return {k: self._make_json_serializable(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self._make_json_serializable(item) for item in obj]
        else:
            return obj

    async def _check_alerts(self, request_data: Dict[str, Any]) -> None:
        """Check if any alerts should be triggered."""
        if not self.enable_alerts:
            return

        try:
            total_requests = self.request_metrics["total_requests"]

            # Check failure rate
            if total_requests >= 10:  # Minimum requests for meaningful stats
                failure_rate = (
                    self.request_metrics["failed_requests"] / total_requests
                ) * 100
                if failure_rate > self.failure_rate_threshold:
                    await self._trigger_alert(
                        "high_failure_rate",
                        {
                            "current_rate": failure_rate,
                            "threshold": self.failure_rate_threshold,
                            "total_requests": total_requests,
                        },
                    )

            # Check block rate
            if total_requests >= 5:
                block_rate = (
                    self.request_metrics["blocked_requests"] / total_requests
                ) * 100
                if block_rate > self.block_rate_threshold:
                    await self._trigger_alert(
                        "high_block_rate",
                        {
                            "current_rate": block_rate,
                            "threshold": self.block_rate_threshold,
                            "total_requests": total_requests,
                        },
                    )

            # Check CAPTCHA rate
            if total_requests >= 5:
                captcha_rate = (
                    self.request_metrics["captcha_requests"] / total_requests
                ) * 100
                if captcha_rate > self.captcha_rate_threshold:
                    await self._trigger_alert(
                        "high_captcha_rate",
                        {
                            "current_rate": captcha_rate,
                            "threshold": self.captcha_rate_threshold,
                            "total_requests": total_requests,
                        },
                    )

            # Check slow response
            response_time = request_data.get("response_time", 0)
            if response_time > self.slow_response_threshold:
                await self._trigger_alert(
                    "slow_response",
                    {
                        "response_time": response_time,
                        "threshold": self.slow_response_threshold,
                        "url": request_data.get("url", "unknown"),
                    },
                )

        except Exception as e:
            logger.error(f"Error checking alerts: {e}")

    async def _trigger_alert(self, alert_type: str, alert_data: Dict[str, Any]) -> None:
        """Trigger an alert with specified data."""
        alert_info = {
            "timestamp": datetime.now().isoformat(),
            "alert_type": alert_type,
            "alert_data": alert_data,
            "event_type": "alert",
        }

        # Log alert
        if self.log_to_file:
            await self._write_to_log("alerts", alert_info)

        logger.warning(f"ALERT: {alert_type} - {alert_data}")

    async def _background_metrics_flush(self) -> None:
        """Background task for metrics aggregation and flushing."""
        try:
            while True:
                await asyncio.sleep(self.flush_interval_seconds)

                # Flush aggregated metrics
                if self.metrics_enabled:
                    metrics_data = {
                        "timestamp": datetime.now().isoformat(),
                        "event_type": "metrics_flush",
                        "flush_interval": self.flush_interval_seconds,
                        "aggregated_metrics": self.get_comprehensive_statistics(),
                    }

                    # Log aggregated metrics
                    if self.log_to_file:
                        await self._write_to_log("metrics", metrics_data)

        except asyncio.CancelledError:
            logger.debug("Background metrics flush task cancelled")
        except Exception as e:
            logger.error(f"Error in background metrics flush: {e}")

    async def _background_cleanup(self) -> None:
        """Background task for log cleanup and maintenance."""
        try:
            while True:
                await asyncio.sleep(3600)  # Run every hour

                # Clean old log files
                await self._cleanup_old_logs()

                # Rotate large log files
                await self._rotate_large_logs()

        except asyncio.CancelledError:
            logger.debug("Background cleanup task cancelled")
        except Exception as e:
            logger.error(f"Error in background cleanup: {e}")

    async def _cleanup_old_logs(self) -> None:
        """Clean up old log files based on retention policy."""
        try:
            cutoff_time = datetime.now() - timedelta(days=self.retention_days)

            for log_file in self.base_log_dir.glob("*.jsonl*"):
                if log_file.stat().st_mtime < cutoff_time.timestamp():
                    log_file.unlink()
                    logger.debug(f"Deleted old log file: {log_file}")

        except Exception as e:
            logger.error(f"Error cleaning old logs: {e}")

    async def _rotate_large_logs(self) -> None:
        """Rotate log files that exceed size limits."""
        try:
            max_size_bytes = self.max_log_size_mb * 1024 * 1024

            for log_type, log_file_path in self.log_files.items():
                if (
                    log_file_path.exists()
                    and log_file_path.stat().st_size > max_size_bytes
                ):
                    # Rotate the file
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    rotated_path = log_file_path.with_suffix(f".{timestamp}.jsonl")
                    log_file_path.rename(rotated_path)

                    logger.info(f"Rotated log file: {log_file_path} -> {rotated_path}")

        except Exception as e:
            logger.error(f"Error rotating logs: {e}")

    def _format_duration(self, seconds: float) -> str:
        """Format duration in human-readable format."""
        if seconds < 60:
            return f"{seconds:.1f}s"
        elif seconds < 3600:
            return f"{seconds/60:.1f}m"
        else:
            return f"{seconds/3600:.1f}h"

    async def cleanup(self) -> None:
        """Cleanup resources and stop background tasks."""
        try:
            # Cancel cleanup task
            if self.cleanup_task and not self.cleanup_task.done():
                self.cleanup_task.cancel()
                try:
                    await self.cleanup_task
                except asyncio.CancelledError:
                    pass  # Expected when cancelling

            # Cancel metrics task
            if self.metrics_task and not self.metrics_task.done():
                self.metrics_task.cancel()
                try:
                    await self.metrics_task
                except asyncio.CancelledError:
                    pass  # Expected when cancelling

            logger.info("AntiBotLogger cleanup completed")

        except Exception as e:
            logger.error(f"Error during AntiBotLogger cleanup: {e}")
