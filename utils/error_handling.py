import json
import logging
import os
import time
import traceback
from types import TracebackType
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Dict, List, Optional, Any, Callable
from functools import wraps
import psutil
import threading
from collections import defaultdict, deque
import random

from .logger import setup_logger


# Custom Exception Classes
class ScraperError(Exception):
    """Base exception for all scraper errors"""

    def __init__(self, message: str, context: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.context = context or {}


class ParsingError(ScraperError):
    """Errors during HTML/data parsing"""

    pass


class ExtractionError(ScraperError):
    """Errors during data extraction"""

    pass


class ValidationError(ScraperError):
    """Data validation failures"""

    pass


class ConfigurationError(ScraperError):
    """Configuration-related errors"""

    pass


class NetworkError(ScraperError):
    """Network and connectivity issues"""

    pass


@dataclass
class ErrorContext:
    """Captures comprehensive error details"""

    url: Optional[str] = None
    selector: Optional[str] = None
    html_snippet: Optional[str] = None
    stack_trace: Optional[str] = None
    timestamp: Optional[datetime] = None
    system_memory: Optional[float] = None
    system_cpu: Optional[float] = None
    execution_time: Optional[float] = None
    additional_data: Optional[Dict[str, Any]] = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()
        if self.system_memory is None:
            self.system_memory = psutil.virtual_memory().percent
        if self.system_cpu is None:
            self.system_cpu = psutil.cpu_percent(interval=1)

    def __enter__(self) -> "ErrorContext":
        return self

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc_value: Optional[BaseException],
        traceback_obj: Optional[TracebackType],
    ) -> bool:
        return False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str, indent=2)


class StructuredLogger:
    """Enhanced logger with JSON formatting and performance metrics"""

    def __init__(self, name: str = "scraper", log_file: str = "data/logs/scraper.log"):
        self.logger = setup_logger(name, logging.INFO, log_file, True)
        self.performance_metrics = defaultdict(list)
        self.error_counts = defaultdict(int)
        self._lock = threading.Lock()

    def log_error(
        self,
        error: Exception,
        context: Optional[ErrorContext] = None,
        level: str = "ERROR",
    ):
        """Log error with structured context"""
        log_data = {
            "timestamp": datetime.now().isoformat(),
            "level": level,
            "error_type": type(error).__name__,
            "error_message": str(error),
            "context": context.to_dict() if context else {},
            "stack_trace": traceback.format_exc(),
        }

        with self._lock:
            self.error_counts[type(error).__name__] += 1

        self.logger.error(json.dumps(log_data, default=str))

    def log_performance(
        self, operation: str, duration: float, metadata: Optional[Dict[str, Any]] = None
    ):
        """Log performance metrics"""
        log_data = {
            "timestamp": datetime.now().isoformat(),
            "operation": operation,
            "duration_ms": duration * 1000,
            "metadata": metadata or {},
        }

        with self._lock:
            self.performance_metrics[operation].append(duration)

        self.logger.info(json.dumps(log_data, default=str))

    def get_error_stats(self) -> Dict[str, int]:
        """Get error statistics"""
        with self._lock:
            return dict(self.error_counts)

    def get_performance_stats(self) -> Dict[str, List[float]]:
        """Get performance statistics"""
        with self._lock:
            return dict(self.performance_metrics)

    # Compatibility helpers -------------------------------------------------

    def error(self, message: str, *args, **kwargs) -> None:
        self.logger.error(message, *args, **kwargs)

    def warning(self, message: str, *args, **kwargs) -> None:
        self.logger.warning(message, *args, **kwargs)

    def info(self, message: str, *args, **kwargs) -> None:
        self.logger.info(message, *args, **kwargs)

    def debug(self, message: str, *args, **kwargs) -> None:
        self.logger.debug(message, *args, **kwargs)


class RetryManager:
    """Manages retry logic with exponential backoff and circuit breaker"""

    def __init__(
        self,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        backoff_factor: float = 2.0,
        jitter: bool = True,
    ):
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.backoff_factor = backoff_factor
        self.jitter = jitter
        self.failure_counts = defaultdict(int)
        self.last_failure_time = defaultdict(float)
        self.circuit_open = defaultdict(bool)
        self.circuit_timeout = 300  # 5 minutes

    def _calculate_delay(self, attempt: int) -> float:
        """Calculate delay with exponential backoff and optional jitter"""
        delay = min(self.base_delay * (self.backoff_factor**attempt), self.max_delay)
        if self.jitter:
            delay *= 0.5 + random.random() * 0.5  # Add 50% jitter
        return delay

    def _is_circuit_open(self, key: str) -> bool:
        """Check if circuit breaker is open"""
        if self.circuit_open[key]:
            if time.time() - self.last_failure_time[key] > self.circuit_timeout:
                self.circuit_open[key] = False
                self.failure_counts[key] = 0
            else:
                return True
        return False

    def retry(self, func: Callable, *args, key: str = "default", **kwargs):
        """Execute function with retry logic"""
        if self._is_circuit_open(key):
            raise NetworkError(f"Circuit breaker open for {key}")

        last_exception = Exception("Unknown error")
        for attempt in range(self.max_retries + 1):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                last_exception = e
                self.failure_counts[key] += 1

                if attempt == self.max_retries:
                    self.circuit_open[key] = True
                    self.last_failure_time[key] = time.time()
                    break

                delay = self._calculate_delay(attempt)
                time.sleep(delay)

        raise last_exception

    def get_failure_stats(self) -> Dict[str, int]:
        """Get failure statistics"""
        return dict(self.failure_counts)


class DebugHelper:
    """Debugging utilities for scraper development"""

    def __init__(self):
        self.snapshots = []
        self.memory_usage = deque(maxlen=100)
        self._lock = threading.Lock()

    def capture_html_snapshot(
        self, url: str, html: str, error_context: Optional[ErrorContext] = None
    ) -> str:
        """Capture HTML snapshot on error"""
        timestamp = datetime.now().isoformat()
        snapshot = {
            "timestamp": timestamp,
            "url": url,
            "html_length": len(html),
            "error_context": error_context.to_dict() if error_context else {},
            "html_preview": html[:1000] + "..." if len(html) > 1000 else html,
        }

        with self._lock:
            self.snapshots.append(snapshot)

        # Save to file
        snapshot_file = f"data/debug/snapshot_{int(time.time())}.json"
        os.makedirs(os.path.dirname(snapshot_file), exist_ok=True)
        with open(snapshot_file, "w") as f:
            json.dump(snapshot, f, indent=2)

        return snapshot_file

    def validate_selector(self, html: str, selector: str) -> Dict[str, Any]:
        """Validate CSS selector against HTML"""
        try:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(html, "html.parser")
            elements = soup.select(selector)
            return {
                "valid": True,
                "matches": len(elements),
                "sample_text": elements[0].get_text()[:100] if elements else None,
            }
        except Exception as e:
            return {"valid": False, "error": str(e)}

    def track_memory_usage(self):
        """Track memory usage over time"""
        memory_percent = psutil.virtual_memory().percent
        with self._lock:
            self.memory_usage.append((datetime.now(), memory_percent))

    def get_memory_stats(self) -> List[tuple]:
        """Get memory usage statistics"""
        with self._lock:
            return list(self.memory_usage)

    def profile_function(self, func: Callable) -> Callable:
        """Decorator to profile function performance"""

        @wraps(func)
        def wrapper(*args, **kwargs):
            start_time = time.time()
            start_memory = psutil.virtual_memory().used

            try:
                result = func(*args, **kwargs)
                return result
            finally:
                end_time = time.time()
                end_memory = psutil.virtual_memory().used

                duration = end_time - start_time
                memory_delta = end_memory - start_memory

                print(
                    f"Function {func.__name__}: {duration:.4f}s, Memory delta: {memory_delta} bytes"
                )

        return wrapper


class ErrorReporter:
    """Error aggregation and reporting system"""

    def __init__(self):
        self.errors = defaultdict(list)
        self.error_stats = defaultdict(int)
        self.alert_thresholds = {
            "NetworkError": 10,
            "ParsingError": 5,
            "ExtractionError": 5,
        }
        self._lock = threading.Lock()

    def report_error(self, error: Exception, context: Optional[ErrorContext] = None):
        """Report an error for aggregation"""
        error_type = type(error).__name__
        timestamp = datetime.now()

        error_record = {
            "timestamp": timestamp,
            "error_type": error_type,
            "message": str(error),
            "context": context.to_dict() if context else {},
        }

        with self._lock:
            self.errors[error_type].append(error_record)
            self.error_stats[error_type] += 1

            # Check alert thresholds
            if self.error_stats[error_type] >= self.alert_thresholds.get(
                error_type, float("inf")
            ):
                self._trigger_alert(error_type)

    def _trigger_alert(self, error_type: str):
        """Trigger alert for high error rates"""
        print(
            f"ALERT: High error rate for {error_type}: {self.error_stats[error_type]} errors"
        )

    def generate_report(self) -> Dict[str, Any]:
        """Generate error report"""
        with self._lock:
            report = {
                "generated_at": datetime.now().isoformat(),
                "total_errors": sum(self.error_stats.values()),
                "error_types": dict(self.error_stats),
                "recent_errors": {},
            }

            # Get recent errors (last 10 per type)
            for error_type, error_list in self.errors.items():
                report["recent_errors"][error_type] = error_list[-10:]

            return report

    def get_error_trends(self) -> Dict[str, int]:
        """Analyze error trends over time"""
        # Simple trend analysis - could be enhanced
        with self._lock:
            return dict(self.error_stats)

    def clear_old_errors(self, days: int = 7):
        """Clear errors older than specified days"""
        cutoff_time = datetime.now().timestamp() - (days * 24 * 60 * 60)

        with self._lock:
            for error_type in list(self.errors.keys()):
                self.errors[error_type] = [
                    error
                    for error in self.errors[error_type]
                    if error["timestamp"].timestamp() > cutoff_time
                ]
                if not self.errors[error_type]:
                    del self.errors[error_type]
                    if error_type in self.error_stats:
                        del self.error_stats[error_type]


# Global instances for easy access
structured_logger = StructuredLogger()
retry_manager = RetryManager()
debug_helper = DebugHelper()
error_reporter = ErrorReporter()
