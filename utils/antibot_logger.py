"""
Enhanced Anti-Bot Logger Module

Comprehensive logging system for anti-bot activity tracking with structured
logging, performance metrics, and comprehensive event monitoring.
"""

import json
import logging
import time
import os
import threading
from datetime import datetime, timedelta
from collections import defaultdict, deque
from dataclasses import dataclass, asdict
from typing import Dict, Any, List, Optional, Union, Tuple
from pathlib import Path
import gzip
import statistics

from utils.logger import setup_logger


@dataclass
class CaptchaEvent:
    """CAPTCHA event data structure"""

    timestamp: float
    captcha_type: str
    url: str
    detection_method: str
    solve_attempt: bool
    solve_result: Optional[Dict[str, Any]]
    solve_time: Optional[float]
    proxy_used: Optional[str]
    user_agent: Optional[str]
    session_id: Optional[str]


@dataclass
class UserAgentEvent:
    """User-Agent rotation event data structure"""

    timestamp: float
    old_ua: str
    new_ua: str
    reason: str
    trigger_event: Optional[str]
    domain: Optional[str]
    effectiveness_score: Optional[float]
    rotation_strategy: str


@dataclass
class ProxyEvent:
    """Proxy rotation event data structure"""

    timestamp: float
    old_proxy: Optional[str]
    new_proxy: Optional[str]
    reason: str
    trigger_event: Optional[str]
    health_score: Optional[float]
    response_time: Optional[float]
    success_rate: Optional[float]
    geographic_region: Optional[str]


@dataclass
class ComplianceEvent:
    """Robots.txt compliance event data structure"""

    timestamp: float
    url: str
    robots_url: str
    allowed: bool
    crawl_delay: float
    user_agent: str
    rule_matched: Optional[str]
    compliance_level: str


@dataclass
class RateLimitEvent:
    """Rate limiting event data structure"""

    timestamp: float
    url: str
    status_code: int
    retry_after: int
    action_taken: str
    backoff_applied: float
    request_count: int
    time_window: float
    recovery_time: Optional[float]


@dataclass
class PerformanceMetrics:
    """Performance metrics data structure"""

    timestamp: float
    request_count: int
    success_rate: float
    average_response_time: float
    proxy_rotation_count: int
    captcha_encounter_count: int
    rate_limit_count: int
    blocked_request_count: int
    session_duration: float


class AntiBotLogger:
    """
    Comprehensive anti-bot activity logging system with structured logging,
    real-time metrics aggregation, and performance analysis.
    """

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize the anti-bot logger.

        Args:
            config: Configuration dictionary containing logging settings
        """
        self.config = config
        self.enabled = config.get("enabled", True)
        self.log_level = config.get("log_level", "INFO")
        self.structured_format = config.get("structured_format", True)
        self.include_performance_metrics = config.get(
            "include_performance_metrics", True
        )

        # Read log destinations with backward compatibility
        log_destinations = config.get("log_destinations", {})
        self.file_path = log_destinations.get("file", {}).get(
            "path", config.get("file_path", "logs/antibot.log")
        )
        self.json_file_path = log_destinations.get("json_file", {}).get(
            "path", config.get("json_file_path", "logs/antibot_structured.jsonl")
        )
        console_config = log_destinations.get("console", {})
        self.console_enabled = console_config.get(
            "enabled", config.get("console_output", False)
        )

        # Directory setup (for backward compatibility)
        self.log_dir = Path(config.get("log_directory", "data/logs/antibot"))
        self.log_dir.mkdir(parents=True, exist_ok=True)

        # Specialized loggers
        self.captcha_logger = self._setup_specialized_logger("captcha")
        self.ua_logger = self._setup_specialized_logger("user_agent")
        self.proxy_logger = self._setup_specialized_logger("proxy")
        self.compliance_logger = self._setup_specialized_logger("compliance")
        self.performance_logger = self._setup_specialized_logger("performance")
        self.general_logger = self._setup_specialized_logger("general")

        # Metrics aggregation with backward compatibility
        self.metrics_buffer: List[Dict[str, Any]] = []
        metrics_agg = config.get("metrics_aggregation", {})
        self.metrics_flush_interval = metrics_agg.get(
            "flush_interval_seconds", config.get("metrics_flush_interval", 60)
        )
        self._last_flush = time.time()
        self._buffer_lock = threading.Lock()

        # Performance tracking
        self.session_start_time = time.time()
        self.request_counter = 0
        self.success_counter = 0
        self.captcha_counter = 0
        self.rate_limit_counter = 0
        self.proxy_rotation_counter = 0
        self.blocked_request_counter = 0

        # Event aggregation windows
        self.event_windows = {
            "captcha": deque(maxlen=100),
            "user_agent": deque(maxlen=100),
            "proxy": deque(maxlen=100),
            "compliance": deque(maxlen=100),
            "rate_limit": deque(maxlen=100),
        }

        # Response time tracking
        self.response_times = deque(maxlen=1000)

        # Alert thresholds with backward compatibility
        alert_thresholds_nested = metrics_agg.get("alert_thresholds", {})
        if alert_thresholds_nested:
            self.alert_thresholds = {
                "captcha_rate_per_hour": alert_thresholds_nested.get(
                    "captcha_rate_percent", 10
                ),
                "proxy_failure_rate": alert_thresholds_nested.get(
                    "block_rate_percent", 5
                )
                / 100.0,  # Convert percent to ratio
                "rate_limit_rate_per_hour": alert_thresholds_nested.get(
                    "error_rate_percent", 15
                ),
                "average_response_time_ms": 5000,  # Keep default, not in settings
                "success_rate_minimum": 0.8,  # Keep default, not in settings
            }
        else:
            self.alert_thresholds = config.get(
                "alert_thresholds",
                {
                    "captcha_rate_per_hour": 10,
                    "proxy_failure_rate": 0.3,
                    "rate_limit_rate_per_hour": 5,
                    "average_response_time_ms": 5000,
                    "success_rate_minimum": 0.8,
                },
            )

        # Enable background metrics flushing if configured
        if config.get("enable_background_flushing", True):
            self._start_background_flushing()

    def _setup_specialized_logger(self, logger_type: str) -> logging.Logger:
        """Setup specialized logger for specific event types"""
        # Use appropriate log file path based on structured format
        if self.structured_format:
            log_file_path = self.json_file_path
        else:
            log_file_path = self.file_path

        # Ensure directory exists
        log_file_dir = Path(log_file_path).parent
        log_file_dir.mkdir(parents=True, exist_ok=True)

        logger_name = f"antibot.{logger_type}"

        logger = setup_logger(
            name=logger_name,
            level=getattr(logging, self.log_level),
            log_file=log_file_path,
            console=self.console_enabled,
        )

        # Add JSON formatter for structured logging
        if self.structured_format:
            json_formatter = logging.Formatter(
                "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
            )

            # Replace file handler formatter
            for handler in logger.handlers:
                if isinstance(handler, logging.FileHandler):
                    handler.setFormatter(json_formatter)

        return logger

    def _start_background_flushing(self) -> None:
        """Start background thread for periodic metrics flushing"""

        def flush_worker():
            while True:
                time.sleep(self.metrics_flush_interval)
                try:
                    self._flush_metrics_buffer()
                except Exception as e:
                    self.general_logger.error(
                        f"Error in background metrics flushing: {e}"
                    )

        flush_thread = threading.Thread(target=flush_worker, daemon=True)
        flush_thread.start()

    def log_captcha_encounter(
        self, captcha_type: str, url: str, solve_result: Dict[str, Any]
    ) -> None:
        """
        Log CAPTCHA encounter event with comprehensive details.

        Args:
            captcha_type: Type of CAPTCHA encountered (recaptcha, cloudflare, etc.)
            url: URL where CAPTCHA was encountered
            solve_result: Dictionary containing solve attempt results
        """
        if not self.enabled:
            return

        event = CaptchaEvent(
            timestamp=time.time(),
            captcha_type=captcha_type,
            url=url,
            detection_method=solve_result.get("detection_method", "unknown"),
            solve_attempt=solve_result.get("solve_attempted", False),
            solve_result=solve_result,
            solve_time=solve_result.get("solve_time"),
            proxy_used=solve_result.get("proxy_used"),
            user_agent=solve_result.get("user_agent"),
            session_id=solve_result.get("session_id"),
        )

        # Log structured event
        event_data = asdict(event)
        if self.structured_format:
            self.captcha_logger.info(json.dumps(event_data))
        else:
            self.captcha_logger.info(
                f"CAPTCHA {captcha_type} encountered at {url} - "
                f"Solve attempt: {event.solve_attempt}, "
                f"Result: {solve_result.get('success', False)}"
            )

        # Update counters and aggregation
        self.captcha_counter += 1
        self.event_windows["captcha"].append(event_data)

        # Add to metrics buffer
        self._add_to_metrics_buffer("captcha_event", event_data)

        # Check for alert conditions
        self._check_captcha_alerts()

    def log_user_agent_rotation(
        self, old_ua: str, new_ua: str, reason: str, **kwargs
    ) -> None:
        """
        Log user-agent rotation event.

        Args:
            old_ua: Previous user agent string
            new_ua: New user agent string
            reason: Reason for rotation
            **kwargs: Additional context (domain, effectiveness_score, etc.)
        """
        if not self.enabled:
            return

        event = UserAgentEvent(
            timestamp=time.time(),
            old_ua=old_ua,
            new_ua=new_ua,
            reason=reason,
            trigger_event=kwargs.get("trigger_event"),
            domain=kwargs.get("domain"),
            effectiveness_score=kwargs.get("effectiveness_score"),
            rotation_strategy=kwargs.get("rotation_strategy", "automatic"),
        )

        # Log structured event
        event_data = asdict(event)
        if self.structured_format:
            self.ua_logger.info(json.dumps(event_data))
        else:
            self.ua_logger.info(
                f"User-Agent rotated - Reason: {reason}, "
                f"Domain: {kwargs.get('domain', 'unknown')}"
            )

        # Update aggregation
        self.event_windows["user_agent"].append(event_data)
        self._add_to_metrics_buffer("user_agent_event", event_data)

    def log_proxy_rotation(
        self, old_proxy: Optional[str], new_proxy: Optional[str], reason: str, **kwargs
    ) -> None:
        """
        Log proxy rotation event.

        Args:
            old_proxy: Previous proxy URL
            new_proxy: New proxy URL
            reason: Reason for rotation
            **kwargs: Additional context (health_score, response_time, etc.)
        """
        if not self.enabled:
            return

        event = ProxyEvent(
            timestamp=time.time(),
            old_proxy=old_proxy,
            new_proxy=new_proxy,
            reason=reason,
            trigger_event=kwargs.get("trigger_event"),
            health_score=kwargs.get("health_score"),
            response_time=kwargs.get("response_time"),
            success_rate=kwargs.get("success_rate"),
            geographic_region=kwargs.get("geographic_region"),
        )

        # Log structured event
        event_data = asdict(event)
        if self.structured_format:
            self.proxy_logger.info(json.dumps(event_data))
        else:
            self.proxy_logger.info(
                f"Proxy rotated - Reason: {reason}, "
                f"Health: {kwargs.get('health_score', 'unknown')}"
            )

        # Update counters and aggregation
        self.proxy_rotation_counter += 1
        self.event_windows["proxy"].append(event_data)
        self._add_to_metrics_buffer("proxy_event", event_data)

        # Check for alert conditions
        self._check_proxy_alerts()

    def log_robots_compliance(
        self, url: str, allowed: bool, crawl_delay: float, **kwargs
    ) -> None:
        """
        Log robots.txt compliance check.

        Args:
            url: URL being checked
            allowed: Whether access is allowed
            crawl_delay: Applied crawl delay in seconds
            **kwargs: Additional context (user_agent, rule_matched, etc.)
        """
        if not self.enabled:
            return

        event = ComplianceEvent(
            timestamp=time.time(),
            url=url,
            robots_url=kwargs.get("robots_url", f"{url}/robots.txt"),
            allowed=allowed,
            crawl_delay=crawl_delay,
            user_agent=kwargs.get("user_agent", "unknown"),
            rule_matched=kwargs.get("rule_matched"),
            compliance_level=kwargs.get("compliance_level", "strict"),
        )

        # Log structured event
        event_data = asdict(event)
        if self.structured_format:
            self.compliance_logger.info(json.dumps(event_data))
        else:
            self.compliance_logger.info(
                f"Robots compliance check - URL: {url}, "
                f"Allowed: {allowed}, Crawl delay: {crawl_delay}s"
            )

        # Update aggregation
        self.event_windows["compliance"].append(event_data)
        self._add_to_metrics_buffer("compliance_event", event_data)

    def log_rate_limit_encounter(
        self, url: str, retry_after: int, action_taken: str, **kwargs
    ) -> None:
        """
        Log rate limiting encounter.

        Args:
            url: URL that triggered rate limiting
            retry_after: Retry-After header value in seconds
            action_taken: Action taken in response
            **kwargs: Additional context (status_code, backoff_applied, etc.)
        """
        if not self.enabled:
            return

        event = RateLimitEvent(
            timestamp=time.time(),
            url=url,
            status_code=kwargs.get("status_code", 429),
            retry_after=retry_after,
            action_taken=action_taken,
            backoff_applied=kwargs.get("backoff_applied", retry_after),
            request_count=kwargs.get("request_count", 1),
            time_window=kwargs.get("time_window", 60.0),
            recovery_time=kwargs.get("recovery_time"),
        )

        # Log structured event
        event_data = asdict(event)
        if self.structured_format:
            self.performance_logger.warning(json.dumps(event_data))
        else:
            self.performance_logger.warning(
                f"Rate limit encountered - URL: {url}, "
                f"Retry after: {retry_after}s, Action: {action_taken}"
            )

        # Update counters and aggregation
        self.rate_limit_counter += 1
        self.event_windows["rate_limit"].append(event_data)
        self._add_to_metrics_buffer("rate_limit_event", event_data)

        # Check for alert conditions
        self._check_rate_limit_alerts()

    def log_anti_bot_event(
        self, event_type: str, event_data: Dict[str, Any], level: str = "INFO"
    ) -> None:
        """
        Unified anti-bot event logging with structured data.

        Args:
            event_type: Type of anti-bot event
            event_data: Event data dictionary
            level: Log level (INFO, WARNING, ERROR)
        """
        if not self.enabled:
            return

        # Add timestamp and event type
        enriched_data = {
            "timestamp": time.time(),
            "event_type": event_type,
            "session_id": getattr(self, "session_id", None),
            **event_data,
        }

        # Select appropriate logger
        logger = self.general_logger
        if event_type.startswith("captcha"):
            logger = self.captcha_logger
        elif event_type.startswith("proxy"):
            logger = self.proxy_logger
        elif event_type.startswith("user_agent"):
            logger = self.ua_logger
        elif event_type.startswith("compliance"):
            logger = self.compliance_logger

        # Log with appropriate level
        log_method = getattr(logger, level.lower(), logger.info)

        if self.structured_format:
            log_method(json.dumps(enriched_data))
        else:
            log_method(f"{event_type}: {event_data}")

        # Add to metrics buffer
        self._add_to_metrics_buffer(event_type, enriched_data)

    def log_scraping_session_summary(self, session_stats: Dict[str, Any]) -> None:
        """
        Log comprehensive session summary.

        Args:
            session_stats: Session statistics dictionary
        """
        if not self.enabled:
            return

        session_duration = time.time() - self.session_start_time

        summary = {
            "session_start": self.session_start_time,
            "session_duration": session_duration,
            "total_requests": self.request_counter,
            "successful_requests": self.success_counter,
            "captcha_encounters": self.captcha_counter,
            "rate_limit_encounters": self.rate_limit_counter,
            "proxy_rotations": self.proxy_rotation_counter,
            "blocked_requests": self.blocked_request_counter,
            "success_rate": self.success_counter / max(self.request_counter, 1),
            "average_response_time": (
                statistics.mean(self.response_times) if self.response_times else 0
            ),
            "requests_per_minute": (
                (self.request_counter / session_duration) * 60
                if session_duration > 0
                else 0
            ),
            **session_stats,
        }

        if self.structured_format:
            self.performance_logger.info(json.dumps(summary))
        else:
            self.performance_logger.info(
                f"Session Summary - Duration: {session_duration:.2f}s, "
                f"Requests: {self.request_counter}, "
                f"Success Rate: {summary['success_rate']:.2%}"
            )

    def generate_anti_bot_report(self, time_period: str = "24h") -> Dict[str, Any]:
        """
        Generate comprehensive anti-bot activity report.

        Args:
            time_period: Time period for report ('1h', '24h', '7d', '30d')

        Returns:
            Comprehensive report dictionary
        """
        # Parse time period
        period_seconds = self._parse_time_period(time_period)
        cutoff_time = time.time() - period_seconds

        # Filter events by time period
        filtered_events = {}
        for event_type, events in self.event_windows.items():
            filtered_events[event_type] = [
                event for event in events if event.get("timestamp", 0) > cutoff_time
            ]

        # Generate report
        report = {
            "report_generated": datetime.now().isoformat(),
            "time_period": time_period,
            "period_start": datetime.fromtimestamp(cutoff_time).isoformat(),
            "period_end": datetime.now().isoformat(),
            "summary": self._generate_summary_statistics(filtered_events),
            "event_analysis": self._analyze_events(filtered_events),
            "performance_metrics": self._calculate_performance_metrics(filtered_events),
            "alert_analysis": self._analyze_alerts(filtered_events),
            "recommendations": self._generate_recommendations(filtered_events),
        }

        return report

    def export_logs(
        self, export_path: str, format: str = "json", time_period: str = "24h"
    ) -> bool:
        """
        Export logs to file.

        Args:
            export_path: Path to export file
            format: Export format ('json', 'csv', 'txt')
            time_period: Time period to export

        Returns:
            True if export successful
        """
        try:
            if format == "json":
                report = self.generate_anti_bot_report(time_period)
                with open(export_path, "w") as f:
                    json.dump(report, f, indent=2)
            elif format == "csv":
                self._export_to_csv(export_path, time_period)
            elif format == "txt":
                self._export_to_text(export_path, time_period)
            else:
                raise ValueError(f"Unsupported export format: {format}")

            self.general_logger.info(
                f"Logs exported to {export_path} in {format} format"
            )
            return True

        except Exception as e:
            self.general_logger.error(f"Failed to export logs: {e}")
            return False

    def track_request_performance(self, response_time: float, success: bool) -> None:
        """
        Track request performance metrics.

        Args:
            response_time: Request response time in seconds
            success: Whether request was successful
        """
        self.request_counter += 1
        if success:
            self.success_counter += 1

        self.response_times.append(response_time)

        # Check if metrics buffer should be flushed
        if time.time() - self._last_flush > self.metrics_flush_interval:
            self._flush_metrics_buffer()

    def set_session_id(self, session_id: str) -> None:
        """Set session ID for event correlation"""
        self.session_id = session_id

    def _add_to_metrics_buffer(self, event_type: str, data: Dict[str, Any]) -> None:
        """Add event to metrics buffer"""
        with self._buffer_lock:
            self.metrics_buffer.append(
                {"event_type": event_type, "timestamp": time.time(), "data": data}
            )

    def _flush_metrics_buffer(self) -> None:
        """Flush metrics buffer to storage"""
        if not self.include_performance_metrics:
            return

        with self._buffer_lock:
            if not self.metrics_buffer:
                return

            # Calculate aggregated metrics
            metrics = self._calculate_buffer_metrics()

            # Log aggregated metrics
            if self.structured_format:
                self.performance_logger.info(json.dumps(metrics))

            # Clear buffer
            self.metrics_buffer.clear()
            self._last_flush = time.time()

    def _calculate_buffer_metrics(self) -> Dict[str, Any]:
        """Calculate aggregated metrics from buffer"""
        event_counts = defaultdict(int)
        for item in self.metrics_buffer:
            event_counts[item["event_type"]] += 1

        return {
            "timestamp": time.time(),
            "buffer_size": len(self.metrics_buffer),
            "event_counts": dict(event_counts),
            "flush_interval": self.metrics_flush_interval,
            "session_duration": time.time() - self.session_start_time,
            "performance_summary": {
                "total_requests": self.request_counter,
                "success_rate": self.success_counter / max(self.request_counter, 1),
                "captcha_rate": self.captcha_counter / max(self.request_counter, 1),
                "rate_limit_rate": self.rate_limit_counter
                / max(self.request_counter, 1),
                "proxy_rotation_rate": self.proxy_rotation_counter
                / max(self.request_counter, 1),
            },
        }

    def _check_captcha_alerts(self) -> None:
        """Check for CAPTCHA-related alerts"""
        threshold = self.alert_thresholds.get("captcha_rate_per_hour", 10)
        recent_captchas = [
            event
            for event in self.event_windows["captcha"]
            if event.get("timestamp", 0) > time.time() - 3600
        ]

        if len(recent_captchas) > threshold:
            self.general_logger.warning(
                f"CAPTCHA alert: {len(recent_captchas)} encounters in last hour "
                f"(threshold: {threshold})"
            )

    def _check_proxy_alerts(self) -> None:
        """Check for proxy-related alerts"""
        if self.proxy_rotation_counter > 0:
            rotation_rate = self.proxy_rotation_counter / max(self.request_counter, 1)
            threshold = self.alert_thresholds.get("proxy_failure_rate", 0.3)

            if rotation_rate > threshold:
                self.general_logger.warning(
                    f"Proxy alert: High rotation rate {rotation_rate:.2%} "
                    f"(threshold: {threshold:.2%})"
                )

    def _check_rate_limit_alerts(self) -> None:
        """Check for rate limiting alerts"""
        threshold = self.alert_thresholds.get("rate_limit_rate_per_hour", 5)
        recent_rate_limits = [
            event
            for event in self.event_windows["rate_limit"]
            if event.get("timestamp", 0) > time.time() - 3600
        ]

        if len(recent_rate_limits) > threshold:
            self.general_logger.warning(
                f"Rate limit alert: {len(recent_rate_limits)} encounters in last hour "
                f"(threshold: {threshold})"
            )

    def _parse_time_period(self, period: str) -> int:
        """Parse time period string to seconds"""
        periods = {"1h": 3600, "24h": 86400, "7d": 604800, "30d": 2592000}
        return periods.get(period, 86400)

    def _generate_summary_statistics(self, events: Dict[str, List]) -> Dict[str, Any]:
        """Generate summary statistics from events"""
        return {
            "total_events": sum(len(event_list) for event_list in events.values()),
            "captcha_events": len(events.get("captcha", [])),
            "proxy_events": len(events.get("proxy", [])),
            "user_agent_events": len(events.get("user_agent", [])),
            "compliance_events": len(events.get("compliance", [])),
            "rate_limit_events": len(events.get("rate_limit", [])),
            "session_performance": {
                "total_requests": self.request_counter,
                "success_rate": self.success_counter / max(self.request_counter, 1),
                "average_response_time": (
                    statistics.mean(self.response_times) if self.response_times else 0
                ),
            },
        }

    def _analyze_events(self, events: Dict[str, List]) -> Dict[str, Any]:
        """Analyze event patterns and trends"""
        analysis = {}

        for event_type, event_list in events.items():
            if not event_list:
                continue

            # Time distribution analysis
            timestamps = [event.get("timestamp", 0) for event in event_list]
            if timestamps:
                analysis[event_type] = {
                    "count": len(event_list),
                    "frequency_per_hour": len(event_list)
                    / max((max(timestamps) - min(timestamps)) / 3600, 1),
                    "most_recent": max(timestamps),
                    "oldest": min(timestamps),
                }

        return analysis

    def _calculate_performance_metrics(self, events: Dict[str, List]) -> Dict[str, Any]:
        """Calculate performance metrics"""
        return {
            "response_time_stats": {
                "mean": (
                    statistics.mean(self.response_times) if self.response_times else 0
                ),
                "median": (
                    statistics.median(self.response_times) if self.response_times else 0
                ),
                "p95": (
                    statistics.quantiles(self.response_times, n=20)[18]
                    if len(self.response_times) > 20
                    else 0
                ),
                "p99": (
                    statistics.quantiles(self.response_times, n=100)[98]
                    if len(self.response_times) > 100
                    else 0
                ),
            },
            "efficiency_metrics": {
                "requests_per_minute": (
                    self.request_counter
                    / max((time.time() - self.session_start_time) / 60, 1)
                ),
                "success_ratio": self.success_counter / max(self.request_counter, 1),
                "captcha_ratio": self.captcha_counter / max(self.request_counter, 1),
                "proxy_rotation_ratio": self.proxy_rotation_counter
                / max(self.request_counter, 1),
            },
        }

    def _analyze_alerts(self, events: Dict[str, List]) -> List[Dict[str, Any]]:
        """Analyze alert conditions"""
        alerts = []

        # Check each alert threshold
        for threshold_name, threshold_value in self.alert_thresholds.items():
            if threshold_name == "captcha_rate_per_hour":
                captcha_count = len(events.get("captcha", []))
                if captcha_count > threshold_value:
                    alerts.append(
                        {
                            "type": "captcha_threshold_exceeded",
                            "severity": "high",
                            "value": captcha_count,
                            "threshold": threshold_value,
                            "description": f"CAPTCHA encounters ({captcha_count}) exceed threshold ({threshold_value})",
                        }
                    )

        return alerts

    def _generate_recommendations(self, events: Dict[str, List]) -> List[str]:
        """Generate recommendations based on event analysis"""
        recommendations = []

        # Analyze CAPTCHA patterns
        captcha_events = events.get("captcha", [])
        if len(captcha_events) > 5:
            recommendations.append(
                "Consider implementing more sophisticated stealth techniques"
            )
            recommendations.append("Review user-agent rotation strategy")

        # Analyze proxy health
        proxy_events = events.get("proxy", [])
        if len(proxy_events) > self.request_counter * 0.2:
            recommendations.append("Improve proxy pool quality and health monitoring")
            recommendations.append("Consider implementing proxy geographic rotation")

        # Analyze rate limiting
        rate_limit_events = events.get("rate_limit", [])
        if len(rate_limit_events) > 3:
            recommendations.append("Implement more conservative request rate limiting")
            recommendations.append(
                "Add intelligent request spacing based on target behavior"
            )

        return recommendations

    def _export_to_csv(self, export_path: str, time_period: str) -> None:
        """Export events to CSV format"""
        import csv

        period_seconds = self._parse_time_period(time_period)
        cutoff_time = time.time() - period_seconds

        with open(export_path, "w", newline="") as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(["timestamp", "event_type", "event_data"])

            for event_type, events in self.event_windows.items():
                for event in events:
                    if event.get("timestamp", 0) > cutoff_time:
                        writer.writerow(
                            [
                                datetime.fromtimestamp(
                                    event.get("timestamp", 0)
                                ).isoformat(),
                                event_type,
                                json.dumps(event),
                            ]
                        )

    def _export_to_text(self, export_path: str, time_period: str) -> None:
        """Export events to text format"""
        report = self.generate_anti_bot_report(time_period)

        with open(export_path, "w") as f:
            f.write("ANTI-BOT ACTIVITY REPORT\n")
            f.write("=" * 50 + "\n\n")

            f.write(f"Report Period: {time_period}\n")
            f.write(f"Generated: {report['report_generated']}\n\n")

            f.write("SUMMARY STATISTICS\n")
            f.write("-" * 20 + "\n")
            for key, value in report["summary"].items():
                f.write(f"{key}: {value}\n")

            f.write("\nRECOMMENDATIONS\n")
            f.write("-" * 15 + "\n")
            for rec in report["recommendations"]:
                f.write(f"- {rec}\n")
