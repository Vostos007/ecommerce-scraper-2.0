"""
Advanced exponential backoff system with jitter and intelligent retry logic.
Provides error-specific strategies and circuit breaker patterns.
"""

import asyncio
import random
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from enum import Enum
from utils.logger import get_logger

logger = get_logger(__name__)


class ErrorType(Enum):
    """Types of errors for specific retry strategies."""

    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    CAPTCHA = "captcha"
    BLOCKED = "blocked"
    NETWORK = "network"
    HTTP_5XX = "http_5xx"
    HTTP_4XX = "http_4xx"
    PROXY_ERROR = "proxy_error"
    AUTHENTICATION = "authentication"
    UNKNOWN = "unknown"


@dataclass
class RetryState:
    """State tracking for retry attempts."""

    identifier: str
    attempt_count: int = 0
    first_failure: Optional[datetime] = None
    last_failure: Optional[datetime] = None
    last_success: Optional[datetime] = None
    failure_types: List[str] = field(default_factory=list)
    total_delay: float = 0.0
    circuit_open: bool = False
    circuit_open_until: Optional[datetime] = None
    success_count: int = 0
    consecutive_failures: int = 0

    @property
    def success_rate(self) -> float:
        """Calculate success rate."""
        total_attempts = self.success_count + self.consecutive_failures
        if total_attempts == 0:
            return 1.0
        return self.success_count / total_attempts

    @property
    def is_circuit_open(self) -> bool:
        """Check if circuit breaker is open."""
        if not self.circuit_open:
            return False
        if self.circuit_open_until and datetime.now() > self.circuit_open_until:
            self.circuit_open = False
            self.circuit_open_until = None
            return False
        return True


class ExponentialBackoff:
    """Advanced exponential backoff with intelligent retry strategies."""

    def __init__(self, config: Dict):
        self.config = config
        self.enabled = config.get("enabled", True)
        self.base_delay = config.get("base_delay_seconds", 1.0)
        self.max_delay = config.get("max_delay_seconds", 300.0)
        self.multiplier = config.get("multiplier", 2.0)
        self.jitter = config.get("jitter", True)
        self.max_attempts = config.get("max_attempts", 5)

        # Error-specific strategies
        self.error_strategies = config.get("error_specific_strategies", {})
        self._setup_default_strategies()

        # Circuit breaker configuration
        self.circuit_breaker_enabled = config.get("circuit_breaker_enabled", True)
        self.circuit_failure_threshold = config.get("circuit_failure_threshold", 5)
        self.circuit_timeout = config.get("circuit_timeout_seconds", 60)
        self.circuit_recovery_attempts = config.get("circuit_recovery_attempts", 3)

        # State tracking
        self.retry_states: Dict[str, RetryState] = {}
        self.global_stats = {
            "total_retries": 0,
            "total_delays": 0.0,
            "circuits_opened": 0,
            "successful_recoveries": 0,
        }

        logger.info(
            f"ExponentialBackoff initialized: base={self.base_delay}s, max={self.max_delay}s"
        )

    def _setup_default_strategies(self):
        """Setup default error-specific retry strategies."""
        defaults = {
            "timeout": {"max_attempts": 3, "multiplier": 1.5, "base_delay": 2.0},
            "rate_limit": {"max_attempts": 5, "multiplier": 3.0, "base_delay": 10.0},
            "captcha": {"max_attempts": 2, "multiplier": 5.0, "base_delay": 30.0},
            "blocked": {"max_attempts": 1, "multiplier": 1.0, "base_delay": 0.0},
            "network": {"max_attempts": 4, "multiplier": 2.0, "base_delay": 1.0},
            "http_5xx": {"max_attempts": 3, "multiplier": 2.0, "base_delay": 5.0},
            "http_4xx": {"max_attempts": 1, "multiplier": 1.0, "base_delay": 0.0},
            "proxy_error": {"max_attempts": 2, "multiplier": 1.5, "base_delay": 3.0},
            "authentication": {"max_attempts": 1, "multiplier": 1.0, "base_delay": 0.0},
        }

        # Merge with user-provided strategies
        for error_type, strategy in defaults.items():
            if error_type not in self.error_strategies:
                self.error_strategies[error_type] = strategy
            else:
                # Merge defaults with user config
                merged = strategy.copy()
                merged.update(self.error_strategies[error_type])
                self.error_strategies[error_type] = merged

    def calculate_delay(
        self, attempt: int, identifier: str = None, error_type: str = None
    ) -> float:
        """
        Calculate delay with exponential backoff and jitter.

        Args:
            attempt: Current attempt number (0-based)
            identifier: Unique identifier for tracking state
            error_type: Type of error for specific strategy

        Returns:
            Delay in seconds
        """
        if not self.enabled:
            return 0.0

        # Get error-specific strategy
        strategy = self._get_error_strategy(error_type)
        base_delay = strategy.get("base_delay", self.base_delay)
        multiplier = strategy.get("multiplier", self.multiplier)
        max_delay = strategy.get("max_delay", self.max_delay)

        # Calculate exponential delay
        delay = min(base_delay * (multiplier**attempt), max_delay)

        # Add jitter to prevent thundering herd
        if self.jitter and delay > 0:
            jitter_factor = 0.1 + (random.random() * 0.4)  # 10-50% jitter
            delay *= 1 + jitter_factor

        # Adaptive delay based on historical performance
        if identifier and identifier in self.retry_states:
            state = self.retry_states[identifier]
            # Increase delay for consistently failing identifiers
            if state.success_rate < 0.3:
                delay *= 1.5
            elif state.success_rate > 0.8:
                delay *= 0.8

        logger.debug(f"Calculated delay for attempt {attempt}: {delay:.2f}s")
        return delay

    def should_retry(
        self,
        identifier: str,
        attempt: int,
        error_type: str,
        max_attempts: Optional[int] = None,
    ) -> bool:
        """
        Intelligent retry decision based on error type and history.

        Args:
            identifier: Unique identifier for tracking state
            attempt: Current attempt number (0-based)
            error_type: Type of error
            max_attempts: Override max attempts

        Returns:
            True if should retry
        """
        if not self.enabled:
            return False

        # Get retry state
        state = self._get_retry_state(identifier)

        # Check circuit breaker
        if self.circuit_breaker_enabled and state.is_circuit_open:
            logger.debug(f"Circuit breaker open for {identifier}, no retry")
            return False

        # Get error-specific strategy
        strategy = self._get_error_strategy(error_type)
        max_retries = max_attempts or strategy.get("max_attempts", self.max_attempts)

        # Basic attempt limit check
        if attempt >= max_retries:
            logger.debug(
                f"Max attempts reached for {identifier}: {attempt}/{max_retries}"
            )
            return False

        # Error-specific logic
        error_enum = self._parse_error_type(error_type)

        if error_enum == ErrorType.BLOCKED:
            # Don't retry blocked requests - need proxy rotation
            return False

        if error_enum == ErrorType.CAPTCHA:
            # Limited retries for CAPTCHA - need different approach
            return attempt < 2

        if error_enum == ErrorType.AUTHENTICATION:
            # Don't retry auth errors - need credential refresh
            return False

        if error_enum == ErrorType.RATE_LIMIT:
            # Allow more retries for rate limiting with longer delays
            return attempt < max(5, max_retries)

        # Check consecutive failure threshold for circuit breaker
        if (
            self.circuit_breaker_enabled
            and state.consecutive_failures >= self.circuit_failure_threshold
        ):
            self._open_circuit_breaker(identifier)
            return False

        return True

    def track_failure(
        self, identifier: str, error_type: str, response_time: float = 0.0
    ) -> None:
        """
        Track failure for identifier with error type.

        Args:
            identifier: Unique identifier
            error_type: Type of error
            response_time: Response time for the failed request
        """
        state = self._get_retry_state(identifier)

        now = datetime.now()
        if state.first_failure is None:
            state.first_failure = now

        state.last_failure = now
        state.attempt_count += 1
        state.consecutive_failures += 1
        state.failure_types.append(f"{now.isoformat()}:{error_type}")

        # Keep only recent failure types
        state.failure_types = state.failure_types[-20:]

        # Update global stats
        self.global_stats["total_retries"] += 1

        logger.debug(
            f"Tracked failure for {identifier}: {error_type} (attempt {state.attempt_count})"
        )

    def track_success(self, identifier: str, response_time: float = 0.0) -> None:
        """
        Track successful request for identifier.

        Args:
            identifier: Unique identifier
            response_time: Response time for the successful request
        """
        state = self._get_retry_state(identifier)

        state.last_success = datetime.now()
        state.success_count += 1
        state.consecutive_failures = 0

        # Close circuit breaker if open
        if state.circuit_open:
            state.circuit_open = False
            state.circuit_open_until = None
            self.global_stats["successful_recoveries"] += 1
            logger.info(
                f"Circuit breaker closed for {identifier} after successful request"
            )

        logger.debug(f"Tracked success for {identifier}")

    def reset_backoff(self, identifier: str) -> None:
        """
        Reset backoff state for identifier.

        Args:
            identifier: Unique identifier to reset
        """
        if identifier in self.retry_states:
            old_state = self.retry_states[identifier]
            # Keep success history but reset failure state
            new_state = RetryState(identifier=identifier)
            new_state.success_count = old_state.success_count
            new_state.last_success = old_state.last_success
            self.retry_states[identifier] = new_state

            logger.debug(f"Reset backoff state for {identifier}")

    async def wait_with_backoff(
        self, identifier: str, attempt: int, error_type: str
    ) -> float:
        """
        Calculate delay and wait asynchronously.

        Args:
            identifier: Unique identifier
            attempt: Current attempt number
            error_type: Type of error

        Returns:
            Actual delay time waited
        """
        delay = self.calculate_delay(attempt, identifier, error_type)

        if delay > 0:
            state = self._get_retry_state(identifier)
            state.total_delay += delay
            self.global_stats["total_delays"] += delay

            logger.debug(f"Waiting {delay:.2f}s before retry for {identifier}")
            await asyncio.sleep(delay)

        return delay

    def get_retry_statistics(self, identifier: str) -> Dict[str, Any]:
        """
        Get retry statistics for identifier.

        Args:
            identifier: Unique identifier

        Returns:
            Dictionary with retry statistics
        """
        if identifier not in self.retry_states:
            return {"error": "No retry data for identifier"}

        state = self.retry_states[identifier]

        return {
            "identifier": identifier,
            "attempt_count": state.attempt_count,
            "success_count": state.success_count,
            "consecutive_failures": state.consecutive_failures,
            "success_rate": state.success_rate,
            "total_delay": state.total_delay,
            "first_failure": (
                state.first_failure.isoformat() if state.first_failure else None
            ),
            "last_failure": (
                state.last_failure.isoformat() if state.last_failure else None
            ),
            "last_success": (
                state.last_success.isoformat() if state.last_success else None
            ),
            "circuit_open": state.circuit_open,
            "circuit_open_until": (
                state.circuit_open_until.isoformat()
                if state.circuit_open_until
                else None
            ),
            "recent_failure_types": state.failure_types[-10:],  # Last 10 failures
            "avg_delay_per_retry": state.total_delay / max(1, state.attempt_count),
        }

    def get_global_statistics(self) -> Dict[str, Any]:
        """Get global retry statistics."""
        active_circuits = sum(
            1 for state in self.retry_states.values() if state.is_circuit_open
        )
        total_identifiers = len(self.retry_states)

        return {
            "total_retries": self.global_stats["total_retries"],
            "total_delays": self.global_stats["total_delays"],
            "circuits_opened": self.global_stats["circuits_opened"],
            "successful_recoveries": self.global_stats["successful_recoveries"],
            "active_circuits": active_circuits,
            "total_identifiers": total_identifiers,
            "avg_delay_per_retry": (
                self.global_stats["total_delays"]
                / max(1, self.global_stats["total_retries"])
            ),
        }

    def cleanup_old_states(self, max_age_hours: int = 24) -> int:
        """
        Clean up old retry states.

        Args:
            max_age_hours: Maximum age in hours to keep states

        Returns:
            Number of states cleaned up
        """
        cutoff_time = datetime.now() - timedelta(hours=max_age_hours)
        states_to_remove = []

        for identifier, state in self.retry_states.items():
            # Remove if no recent activity and not in circuit breaker state
            last_activity = state.last_success or state.last_failure
            if (
                last_activity
                and last_activity < cutoff_time
                and not state.is_circuit_open
            ):
                states_to_remove.append(identifier)

        for identifier in states_to_remove:
            del self.retry_states[identifier]

        if states_to_remove:
            logger.info(f"Cleaned up {len(states_to_remove)} old retry states")

        return len(states_to_remove)

    def _get_retry_state(self, identifier: str) -> RetryState:
        """Get or create retry state for identifier."""
        if identifier not in self.retry_states:
            self.retry_states[identifier] = RetryState(identifier=identifier)
        return self.retry_states[identifier]

    def _get_error_strategy(self, error_type: Optional[str]) -> Dict[str, Any]:
        """Get error-specific strategy configuration."""
        if not error_type:
            return {
                "max_attempts": self.max_attempts,
                "multiplier": self.multiplier,
                "base_delay": self.base_delay,
                "max_delay": self.max_delay,
            }

        error_enum = self._parse_error_type(error_type)
        strategy_key = error_enum.value

        return self.error_strategies.get(
            strategy_key,
            {
                "max_attempts": self.max_attempts,
                "multiplier": self.multiplier,
                "base_delay": self.base_delay,
                "max_delay": self.max_delay,
            },
        )

    def _parse_error_type(self, error_type: str) -> ErrorType:
        """Parse error type string into ErrorType enum."""
        error_type_lower = error_type.lower()
        normalized = error_type_lower.replace("_", " ")

        # Map common error patterns to types
        if "timeout" in normalized or "timed out" in normalized:
            return ErrorType.TIMEOUT
        elif "rate limit" in normalized or "429" in error_type_lower:
            return ErrorType.RATE_LIMIT
        elif "captcha" in normalized or "recaptcha" in normalized:
            return ErrorType.CAPTCHA
        elif "blocked" in normalized or "access denied" in normalized:
            return ErrorType.BLOCKED
        elif "network" in normalized or "connection" in normalized:
            return ErrorType.NETWORK
        elif "5" in error_type_lower and any(
            code in error_type_lower for code in ["500", "502", "503", "504"]
        ):
            return ErrorType.HTTP_5XX
        elif "4" in error_type_lower and any(
            code in error_type_lower for code in ["400", "401", "403", "404"]
        ):
            return ErrorType.HTTP_4XX
        elif "proxy" in normalized:
            return ErrorType.PROXY_ERROR
        elif "auth" in normalized or "unauthorized" in normalized:
            return ErrorType.AUTHENTICATION
        else:
            return ErrorType.UNKNOWN

    def _open_circuit_breaker(self, identifier: str) -> None:
        """Open circuit breaker for identifier."""
        state = self._get_retry_state(identifier)

        if not state.circuit_open:
            state.circuit_open = True
            state.circuit_open_until = datetime.now() + timedelta(
                seconds=self.circuit_timeout
            )
            self.global_stats["circuits_opened"] += 1

            logger.warning(
                f"Circuit breaker opened for {identifier} after {state.consecutive_failures} failures"
            )

    def is_identifier_healthy(self, identifier: str) -> bool:
        """
        Check if identifier is considered healthy for requests.

        Args:
            identifier: Unique identifier to check

        Returns:
            True if healthy, False if circuit is open or too many recent failures
        """
        if identifier not in self.retry_states:
            return True  # Unknown identifiers are considered healthy

        state = self.retry_states[identifier]

        # Check circuit breaker
        if state.is_circuit_open:
            return False

        # Check recent failure rate
        if state.consecutive_failures >= self.circuit_failure_threshold:
            return False

        # Check success rate
        if state.success_rate < 0.2 and state.attempt_count > 5:
            return False

        return True

    def get_healthy_identifiers(self, identifiers: List[str]) -> List[str]:
        """
        Filter list of identifiers to only healthy ones.

        Args:
            identifiers: List of identifiers to filter

        Returns:
            List of healthy identifiers
        """
        return [
            identifier
            for identifier in identifiers
            if self.is_identifier_healthy(identifier)
        ]

    def force_circuit_breaker_reset(self, identifier: str) -> bool:
        """
        Force reset circuit breaker for identifier.

        Args:
            identifier: Identifier to reset

        Returns:
            True if circuit breaker was reset
        """
        if identifier in self.retry_states:
            state = self.retry_states[identifier]
            if state.circuit_open:
                state.circuit_open = False
                state.circuit_open_until = None
                state.consecutive_failures = 0
                logger.info(f"Force reset circuit breaker for {identifier}")
                return True

        return False
