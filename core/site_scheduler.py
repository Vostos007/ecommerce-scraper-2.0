"""Site scheduling coordinator for intelligent delays and circuit breakers."""

from __future__ import annotations

import random
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Deque, Dict, Optional, Tuple

from core.exponential_backoff import ExponentialBackoff
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class DomainStats:
    """Aggregated statistics for a domain."""

    success: int = 0
    failures: int = 0
    total: int = 0
    skipped: int = 0
    last_duration: Optional[float] = None
    last_updated: float = field(default_factory=time.time)
    track_performance_window: bool = False
    window_seconds: Optional[float] = None
    recent_outcomes: Deque[Tuple[float, bool]] = field(default_factory=deque)

    def _prune_window(self, now: float) -> None:
        if not self.track_performance_window or not self.window_seconds:
            return
        cutoff = now - self.window_seconds
        while self.recent_outcomes and self.recent_outcomes[0][0] < cutoff:
            self.recent_outcomes.popleft()

    def record_result(self, success: bool, duration: Optional[float] = None) -> None:
        now = time.time()
        self.total += 1
        self.last_updated = now
        self.last_duration = duration

        if success:
            self.success += 1
        else:
            self.failures += 1

        if self.track_performance_window and self.window_seconds:
            self.recent_outcomes.append((now, success))
            self._prune_window(now)

    def record_skip(self) -> None:
        now = time.time()
        self.skipped += 1
        self.last_updated = now

    @property
    def success_rate(self) -> float:
        if self.track_performance_window and self.window_seconds:
            self._prune_window(time.time())
            if not self.recent_outcomes:
                return 1.0
            successes = sum(1 for _, outcome in self.recent_outcomes if outcome)
            return successes / len(self.recent_outcomes)
        if self.total == 0:
            return 1.0
        return self.success / self.total


class SiteScheduler:
    """Coordinates intelligent site scheduling and circuit breaker logic."""

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self.config = config or {}
        self.inter_site_delay = float(self.config.get("inter_site_delay_seconds", 0.0))
        self.jitter_factor = float(self.config.get("jitter_factor", 0.0))
        domain_cfg = self.config.get("domain_rate_limit", {}) or {}
        self.min_domain_interval = float(domain_cfg.get("min_interval_seconds", 0.0))

        perf_cfg = self.config.get("performance_monitoring", {}) or {}
        self.track_performance = bool(perf_cfg.get("track_performance_metrics", False))
        self.auto_adjust_delays = bool(perf_cfg.get("auto_adjust_delays", False))

        circuit_cfg = self.config.get("circuit_breaker", {}) or {}
        self.health_check_interval = float(circuit_cfg.get("health_check_interval", 0.0) or 0.0)
        backoff_config = {
            "enabled": circuit_cfg.get("enabled", True),
            "circuit_breaker_enabled": circuit_cfg.get("enabled", True),
            "circuit_failure_threshold": circuit_cfg.get("circuit_failure_threshold", 3),
            "circuit_timeout_seconds": circuit_cfg.get("circuit_timeout_seconds", 900),
            "circuit_recovery_attempts": circuit_cfg.get("recovery_attempts", 1),
            "base_delay_seconds": max(1.0, self.inter_site_delay or 1.0),
            "max_delay_seconds": max(60.0, self.inter_site_delay * 10 or 60.0),
        }

        self.backoff = ExponentialBackoff(backoff_config)

        alias_cfg_raw = self.config.get("domain_aliasing")
        alias_cfg = alias_cfg_raw if isinstance(alias_cfg_raw, dict) else {}
        self.domain_alias_map = {
            key.strip().lower(): value.strip().lower()
            for key, value in (alias_cfg.get("aliases") or {}).items()
            if isinstance(key, str) and isinstance(value, str)
        }
        self.collapse_subdomains = bool(alias_cfg.get("collapse_subdomains", False))

        self.domain_last_run: Dict[str, float] = {}
        self.domain_stats: Dict[str, DomainStats] = {}
        self.performance_window_minutes = int(perf_cfg.get("performance_window_minutes", 60))
        if self.performance_window_minutes > 0:
            self.performance_window_seconds = float(self.performance_window_minutes * 60)
        else:
            self.performance_window_seconds = None

        self._health_cache: Dict[str, Tuple[bool, Optional[datetime]]] = {}
        self._last_health_check: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # Domain helpers
    # ------------------------------------------------------------------
    def normalize_domain(self, domain: str) -> str:
        normalized = (domain or "").strip().lower()
        if not normalized:
            return normalized

        alias = self.domain_alias_map.get(normalized)
        if alias:
            normalized = alias

        if self.collapse_subdomains:
            parts = normalized.split(".")
            if len(parts) > 2:
                normalized = ".".join(parts[-2:])

        alias = self.domain_alias_map.get(normalized)
        if alias:
            normalized = alias

        return normalized

    def _get_stats(self, domain: str) -> DomainStats:
        stats = self.domain_stats.get(domain)
        if stats is None:
            stats = DomainStats(
                track_performance_window=self.track_performance and bool(self.performance_window_seconds),
                window_seconds=self.performance_window_seconds,
            )
            self.domain_stats[domain] = stats
        return stats

    def _invalidate_health_cache(self, domain: str) -> None:
        self._health_cache.pop(domain, None)
        self._last_health_check.pop(domain, None)

    # ------------------------------------------------------------------
    # Delay calculations
    # ------------------------------------------------------------------
    def calculate_domain_delay(self, domain: str, now: Optional[float] = None) -> float:
        domain_key = self.normalize_domain(domain)
        now = now or time.time()
        delay = max(0.0, self.inter_site_delay)

        last_run = self.domain_last_run.get(domain_key)
        if last_run is not None:
            elapsed = now - last_run
            required_gap = max(0.0, self.min_domain_interval - elapsed)
            delay = max(delay, required_gap)

        if self.auto_adjust_delays:
            stats = self.domain_stats.get(domain_key)
            if stats:
                success_rate = stats.success_rate
                if success_rate < 0.5:
                    delay *= 1.5
                elif success_rate > 0.9:
                    delay *= 0.75

        if delay > 0 and self.jitter_factor > 0:
            jitter = delay * self.jitter_factor
            delay += random.uniform(-jitter, jitter)

        return max(0.0, delay)

    # ------------------------------------------------------------------
    # Circuit breaker helpers
    # ------------------------------------------------------------------
    def is_domain_healthy(self, domain: str) -> Tuple[bool, Optional[datetime]]:
        domain_key = self.normalize_domain(domain)

        if self.health_check_interval > 0:
            last_check = self._last_health_check.get(domain_key)
            if last_check is not None and (time.time() - last_check) < self.health_check_interval:
                cached = self._health_cache.get(domain_key)
                if cached is not None:
                    return cached

        healthy = self.backoff.is_identifier_healthy(domain_key)
        state = self.backoff.retry_states.get(domain_key)
        open_until = None

        if state and state.is_circuit_open:
            open_until = state.circuit_open_until
            healthy = False
        elif not healthy and state and state.consecutive_failures >= self.backoff.circuit_failure_threshold:
            state.circuit_open = True
            timeout_seconds = getattr(self.backoff, "circuit_timeout", 0) or 0
            if timeout_seconds <= 0:
                timeout_seconds = float(
                    self.config.get("circuit_breaker", {})
                    .get("circuit_timeout_seconds", 0)
                    or 0
                )
            open_until = datetime.now() + timedelta(seconds=timeout_seconds or 0)
            state.circuit_open_until = open_until
            healthy = False
        elif not healthy and state and state.circuit_open_until:
            open_until = state.circuit_open_until

        if self.health_check_interval > 0:
            self._last_health_check[domain_key] = time.time()
            self._health_cache[domain_key] = (healthy, open_until)

        return healthy, open_until

    def record_site_result(
        self,
        domain: str,
        success: bool,
        error_type: Optional[str] = None,
        duration: Optional[float] = None,
    ) -> None:
        domain_key = self.normalize_domain(domain)
        stats = self._get_stats(domain_key)
        stats.record_result(success, duration)

        if success:
            self.backoff.track_success(domain_key, duration or 0.0)
        else:
            self.backoff.track_failure(domain_key, error_type or "unknown", duration or 0.0)

        self._invalidate_health_cache(domain_key)

    def record_site_skip(self, domain: str) -> None:
        domain_key = self.normalize_domain(domain)
        stats = self._get_stats(domain_key)
        stats.record_skip()
        self._invalidate_health_cache(domain_key)

    # ------------------------------------------------------------------
    # Scheduling helpers
    # ------------------------------------------------------------------
    def mark_domain_started(self, domain: str, timestamp: Optional[float] = None) -> None:
        domain_key = self.normalize_domain(domain)
        self.domain_last_run[domain_key] = timestamp or time.time()
        self._invalidate_health_cache(domain_key)

    def schedule_domain(self, domain: str) -> Tuple[float, bool, Optional[datetime]]:
        domain_key = self.normalize_domain(domain)
        healthy, open_until = self.is_domain_healthy(domain_key)
        delay = 0.0
        if healthy:
            delay = self.calculate_domain_delay(domain_key)
        return delay, healthy, open_until

    # ------------------------------------------------------------------
    # Concurrency helpers
    # ------------------------------------------------------------------
    def calculate_optimal_concurrency(
        self,
        total_sites: int,
        unique_domains: int,
        open_circuits: int = 0,
        cpu_count: Optional[int] = None,
    ) -> int:
        max_configured = int(self.config.get("max_concurrency", 0) or 0)
        adaptive = bool(self.config.get("adaptive_concurrency", False))

        from os import cpu_count as os_cpu_count

        cpu_count = cpu_count or os_cpu_count() or 2
        baseline = max(1, min(total_sites, cpu_count * 2))
        if max_configured:
            baseline = min(baseline, max_configured)

        if adaptive and open_circuits:
            reduction_factor = max(0.3, 1 - (open_circuits / max(1, unique_domains)))
            baseline = max(1, int(baseline * reduction_factor))

        return max(1, min(total_sites, baseline))

    # ------------------------------------------------------------------
    # Statistics and monitoring
    # ------------------------------------------------------------------
    def get_statistics(self) -> Dict[str, Any]:
        active_circuits = sum(
            1
            for state in self.backoff.retry_states.values()
            if state.is_circuit_open
        )
        total_domains = len(self.domain_stats)
        avg_success = 1.0
        if total_domains:
            avg_success = sum(stats.success_rate for stats in self.domain_stats.values()) / total_domains

        return {
            "tracked_domains": total_domains,
            "active_circuits": active_circuits,
            "average_success_rate": avg_success,
            "inter_site_delay": self.inter_site_delay,
            "min_domain_interval": self.min_domain_interval,
            "skipped_domains": sum(stats.skipped for stats in self.domain_stats.values()),
        }


__all__ = ["SiteScheduler", "DomainStats"]
