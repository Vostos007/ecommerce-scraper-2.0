"""Proxy policy orchestration for multi-step scraping pipeline."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

try:  # Optional dependency guard for environments missing PyYAML.
    import yaml
except ImportError as exc:  # pragma: no cover - installation issue surface
    raise RuntimeError(
        "PyYAML is required for proxy policy configuration loading."
    ) from exc


MB = 1024 * 1024


def _utcnow() -> dt.datetime:
    """Return timezone-naive UTC timestamp for internal bookkeeping."""

    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


@dataclass
class BudgetStatus:
    """Result of a budget consumption attempt."""

    allowed: bool
    should_throttle: bool
    blocked: bool
    reason: str = ""
    usage_ratio: float = 0.0

    @classmethod
    def allowed_status(cls, usage_ratio: float = 0.0) -> "BudgetStatus":
        return cls(True, False, False, "", usage_ratio)

    @classmethod
    def blocked_status(cls, reason: str, usage_ratio: float = 1.0) -> "BudgetStatus":
        return cls(False, True, True, reason, usage_ratio)


@dataclass
class _DailyBucket:
    """Daily rolling budget with soft/hard limits."""

    name: str
    soft_limit_bytes: int
    hard_limit_bytes: int
    throttle_ratio: float = 0.8
    window_start: dt.date = field(init=False, default_factory=lambda: _utcnow().date())
    usage_bytes: int = 0

    def _ensure_window(self, now: dt.datetime) -> None:
        current_day = now.date()
        if current_day != self.window_start:
            self.window_start = current_day
            self.usage_bytes = 0

    def consume(self, bytes_used: int, now: dt.datetime) -> BudgetStatus:
        self._ensure_window(now)

        if self.hard_limit_bytes and self.usage_bytes >= self.hard_limit_bytes:
            return BudgetStatus.blocked_status(
                reason=f"{self.name} hard limit exhausted",
                usage_ratio=1.0,
            )

        self.usage_bytes += bytes_used

        usage_ratio = (
            self.usage_bytes / self.soft_limit_bytes
            if self.soft_limit_bytes
            else 0.0
        )

        blocked = self.hard_limit_bytes and self.usage_bytes > self.hard_limit_bytes
        if blocked:
            return BudgetStatus(
                allowed=False,
                should_throttle=True,
                blocked=True,
                reason=f"{self.name} hard limit reached",
                usage_ratio=1.0,
            )

        should_throttle = self.soft_limit_bytes and usage_ratio >= self.throttle_ratio
        if should_throttle:
            return BudgetStatus(
                allowed=True,
                should_throttle=True,
                blocked=False,
                reason=f"{self.name} soft limit threshold crossed",
                usage_ratio=min(usage_ratio, 1.0),
            )

        return BudgetStatus.allowed_status(usage_ratio)


class ResidentialBurstController:
    """Orchestrates burst usage of residential proxies with cooldown and quotas."""

    def __init__(self, config: Dict[str, Any], site_budget_mb: int = 0):
        burst_cfg = config.get("burst", {})
        self.max_consecutive = int(burst_cfg.get("max_consecutive_requests", 5))
        self.cooldown_seconds = int(burst_cfg.get("cooldown_after_burst_sec", 1800))
        self.daily_burst_allowance = int(burst_cfg.get("daily_burst_allowance", 100))
        self.adaptive_cooldown = bool(burst_cfg.get("adaptive_cooldown", False))
        self.cooldown_multiplier = float(burst_cfg.get("cooldown_multiplier", 1.0))
        self.success_cooldown_reset = bool(
            burst_cfg.get("success_cooldown_reset", False)
        )

        self.provider_daily_cap_bytes = int(config.get("daily_mb_cap", 0)) * MB
        self.default_site_cap_bytes = int(site_budget_mb) * MB

        self._state: Dict[str, Dict[str, Any]] = {}
        self._provider_usage = _DailyBucket(
            name="residential provider",
            soft_limit_bytes=self.provider_daily_cap_bytes or 0,
            hard_limit_bytes=self.provider_daily_cap_bytes or 0,
        )

    def _get_state(self, domain: str) -> Dict[str, Any]:
        if domain not in self._state:
            self._state[domain] = {
                "requests_in_burst": 0,
                "cooldown_until": dt.datetime.min.replace(tzinfo=None),
                "current_cooldown": self.cooldown_seconds,
                "burst_count": 0,
                "daily_usage": _DailyBucket(
                    name=f"residential {domain} budget",
                    soft_limit_bytes=self.default_site_cap_bytes,
                    hard_limit_bytes=self.default_site_cap_bytes,
                ),
                "blocked": False,
                "last_budget_status": BudgetStatus.allowed_status(),
            }
        return self._state[domain]

    def update_site_budget(self, domain: str, megabytes: Optional[int]) -> None:
        state = self._get_state(domain)
        bytes_cap = (int(megabytes) * MB) if megabytes else 0
        state["daily_usage"].soft_limit_bytes = bytes_cap
        state["daily_usage"].hard_limit_bytes = bytes_cap

    def can_start_request(self, domain: str, now: dt.datetime) -> bool:
        state = self._get_state(domain)
        if state["blocked"]:
            return False

        if state["last_budget_status"].blocked:
            return False

        if now < state["cooldown_until"]:
            return False

        if self.provider_daily_cap_bytes:
            provider_status = self._provider_usage.consume(0, now)
            if provider_status.blocked:
                return False

        site_bucket: _DailyBucket = state["daily_usage"]
        site_bucket.consume(0, now)
        if site_bucket.hard_limit_bytes and site_bucket.usage_bytes >= site_bucket.hard_limit_bytes:
            return False

        if self.daily_burst_allowance and state["requests_in_burst"] >= self.daily_burst_allowance:
            return False

        return True

    def record_request(
        self,
        domain: str,
        bytes_used: int,
        now: Optional[dt.datetime] = None,
    ) -> None:
        state = self._get_state(domain)
        now = now or _utcnow()
        state["requests_in_burst"] += 1
        state["daily_usage"].usage_bytes += bytes_used
        self._provider_usage.usage_bytes += bytes_used
        if state["requests_in_burst"] >= self.max_consecutive:
            state["burst_count"] += 1
            if self.adaptive_cooldown and state["burst_count"] > 1:
                state["current_cooldown"] = int(
                    max(
                        self.cooldown_seconds,
                        state["current_cooldown"] * self.cooldown_multiplier,
                    )
                )
            else:
                state["current_cooldown"] = self.cooldown_seconds
            state["cooldown_until"] = now + dt.timedelta(seconds=state["current_cooldown"])

    def record_success(self, domain: str) -> None:
        state = self._get_state(domain)
        if self.success_cooldown_reset:
            state["requests_in_burst"] = 0
            state["current_cooldown"] = self.cooldown_seconds
            state["cooldown_until"] = dt.datetime.min.replace(tzinfo=None)
            state["burst_count"] = 0

    def update_budget_status(self, domain: str, status: BudgetStatus) -> None:
        state = self._get_state(domain)
        state["last_budget_status"] = status
        if status.blocked:
            state["blocked"] = True

    def lift_budget_block(self, domain: str) -> None:
        state = self._get_state(domain)
        state["blocked"] = False
        state["last_budget_status"] = BudgetStatus.allowed_status()

    def snapshot(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {}
        for domain, state in self._state.items():
            bucket: _DailyBucket = state["daily_usage"]
            data[domain] = {
                "requests_in_burst": state["requests_in_burst"],
                "cooldown_until": state["cooldown_until"].isoformat()
                if state["cooldown_until"] != dt.datetime.min.replace(tzinfo=None)
                else None,
                "blocked": state["blocked"],
                "usage_mb": round(bucket.usage_bytes / MB, 2),
                "limit_mb": round(bucket.hard_limit_bytes / MB, 2)
                if bucket.hard_limit_bytes
                else None,
            }
        return data


class TrafficBudgetManager:
    """Manages consumption against global/site/residential traffic budgets."""

    def __init__(
        self,
        global_config: Dict[str, Any],
        site_profiles: Dict[str, Any],
        residential_tracker: Optional[ResidentialBurstController] = None,
    ) -> None:
        self.global_bucket = _DailyBucket(
            name="global",
            soft_limit_bytes=int(global_config.get("global_soft_mb_per_day", 0)) * MB,
            hard_limit_bytes=int(global_config.get("global_hard_mb_per_day", 0)) * MB,
        )
        self.residential_global_bucket = _DailyBucket(
            name="global residential",
            soft_limit_bytes=int(global_config.get("residential_soft_mb_total", 0)) * MB,
            hard_limit_bytes=int(global_config.get("residential_hard_mb_total", 0)) * MB,
        )
        self.site_buckets: Dict[str, _DailyBucket] = {}
        self.site_residential_buckets: Dict[str, _DailyBucket] = {}
        self.residential_tracker = residential_tracker

        for domain, profile in site_profiles.items():
            budget = profile.get("budget", {}) if isinstance(profile, dict) else {}
            soft = int(budget.get("soft_mb_per_day", 0)) * MB
            hard = int(budget.get("hard_mb_per_day", 0)) * MB
            self.site_buckets[domain] = _DailyBucket(
                name=f"{domain}",
                soft_limit_bytes=soft,
                hard_limit_bytes=hard,
            )
            residential_mb = budget.get("residential_mb_per_day")
            if residential_mb:
                self.site_residential_buckets[domain] = _DailyBucket(
                    name=f"{domain} residential",
                    soft_limit_bytes=int(residential_mb) * MB,
                    hard_limit_bytes=int(residential_mb) * MB,
                )

            if self.residential_tracker and residential_mb:
                self.residential_tracker.update_site_budget(domain, residential_mb)

    def consume(
        self,
        site: str,
        bytes_used: int,
        proxy_type: str,
        now: Optional[dt.datetime] = None,
    ) -> BudgetStatus:
        now = now or _utcnow()

        statuses: List[BudgetStatus] = []

        statuses.append(self.global_bucket.consume(bytes_used, now))

        site_bucket = self.site_buckets.get(site)
        if site_bucket:
            statuses.append(site_bucket.consume(bytes_used, now))

        if proxy_type == "residential":
            statuses.append(
                self.residential_global_bucket.consume(bytes_used, now)
            )
            site_res = self.site_residential_buckets.get(site)
            if site_res:
                statuses.append(site_res.consume(bytes_used, now))

        allowed = all(status.allowed for status in statuses)
        blocked = any(status.blocked for status in statuses)
        should_throttle = any(status.should_throttle for status in statuses)

        reason = ", ".join(filter(None, (status.reason for status in statuses)))
        if blocked and not reason:
            reason = "hard limit reached"
        if should_throttle and not reason:
            reason = "soft limit threshold crossed"

        merged = BudgetStatus(
            allowed=allowed,
            should_throttle=should_throttle,
            blocked=blocked,
            reason=reason,
            usage_ratio=max((status.usage_ratio for status in statuses), default=0.0),
        )

        if (
            self.residential_tracker
            and proxy_type == "residential"
            and site
        ):
            self.residential_tracker.update_budget_status(site, merged)

        return merged

    def snapshot(self) -> Dict[str, Any]:
        def _bucket_summary(bucket: Optional[_DailyBucket]) -> Optional[Dict[str, Any]]:
            if not bucket:
                return None
            return {
                "usage_mb": round(bucket.usage_bytes / MB, 2),
                "soft_mb": round(bucket.soft_limit_bytes / MB, 2)
                if bucket.soft_limit_bytes
                else None,
                "hard_mb": round(bucket.hard_limit_bytes / MB, 2)
                if bucket.hard_limit_bytes
                else None,
                "window_start": bucket.window_start.isoformat(),
            }

        sites = {
            domain: _bucket_summary(bucket)
            for domain, bucket in self.site_buckets.items()
        }
        residential_sites = {
            domain: _bucket_summary(bucket)
            for domain, bucket in self.site_residential_buckets.items()
        }

        return {
            "global": _bucket_summary(self.global_bucket),
            "global_residential": _bucket_summary(self.residential_global_bucket),
            "sites": sites,
            "residential_sites": residential_sites,
        }


class ProxyFlowState:
    """Mutable state for a domain-specific fetch flow."""

    def __init__(self, controller: "ProxyFlowController", domain: str):
        self._controller = controller
        self.domain = domain
        profile = controller.site_profiles.get(domain, {})
        sequence = list(profile.get("fetch_policy", {}).get("sequence", []))
        if not sequence:
            sequence = ["direct", "datacenter_proxy", "antibot", "flaresolverr"]
        self.initial_sequence = sequence
        self.remaining_steps: List[str] = sequence[:]
        self.current_step = self.remaining_steps.pop(0)
        self.failure_counters: Dict[str, int] = {}

    def record_outcome(self, outcome: str) -> None:
        counter_key = f"{self.current_step}:{outcome}"
        self.failure_counters[counter_key] = self.failure_counters.get(counter_key, 0) + 1
        advance = False

        if outcome == "success":
            self.remaining_steps.clear()
            return

        if outcome == "connect_timeout" and self.failure_counters[counter_key] >= 2:
            advance = True
        elif outcome in {"http_403", "http_429", "http_503", "captcha"}:
            advance = True
        elif outcome in {"proxy_unavailable", "fatal_error"}:
            advance = True

        if advance and self.remaining_steps:
            self.current_step = self.remaining_steps.pop(0)
        elif advance and not self.remaining_steps:
            self.current_step = "unavailable"


class ProxyFlowController:
    """Coordinates step escalation, budgets, and residential gating."""

    def __init__(
        self,
        global_config: Dict[str, Any],
        proxy_configs: Dict[str, Any],
        site_profiles: Dict[str, Any],
        residential_config: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.site_profiles = site_profiles
        self.residential_controller = ResidentialBurstController(
            residential_config or {},
            site_budget_mb=0,
        )
        self.budget_manager = TrafficBudgetManager(
            global_config=global_config.get("traffic_budget", {}),
            site_profiles=site_profiles,
            residential_tracker=self.residential_controller,
        )
        self.proxy_configs = proxy_configs
        self._states: Dict[str, ProxyFlowState] = {}

    def start_flow(self, domain: str) -> ProxyFlowState:
        if domain not in self._states:
            state = ProxyFlowState(self, domain)
            state.remaining_steps = [
                step
                for step in state.remaining_steps
                if self._step_allowed(domain, step)
            ]
            if state.current_step == "residential_burst" and not self._step_allowed(
                domain, "residential_burst"
            ):
                if state.remaining_steps:
                    state.current_step = state.remaining_steps.pop(0)
            self._states[domain] = state
        return self._states[domain]

    def _step_allowed(self, domain: str, step: str) -> bool:
        profile = self.site_profiles.get(domain, {})
        if step == "residential_burst":
            if not profile.get("allow_residential", False):
                return False
        return True

    def can_use_residential(self, domain: str, now: Optional[dt.datetime] = None) -> bool:
        now = now or _utcnow()
        return self.residential_controller.can_start_request(domain, now)

    def record_residential_request(
        self,
        domain: str,
        bytes_used: int,
        now: Optional[dt.datetime] = None,
    ) -> BudgetStatus:
        now = now or _utcnow()
        status = self.budget_manager.consume(
            site=domain,
            bytes_used=bytes_used,
            proxy_type="residential",
            now=now,
        )
        if status.allowed:
            self.residential_controller.record_request(
                domain, bytes_used, now=now
            )
        return status

    def snapshot(self) -> Dict[str, Any]:
        return {
            "traffic_budget": self.budget_manager.snapshot(),
            "residential": self.residential_controller.snapshot(),
        }


__all__ = [
    "BudgetStatus",
    "ResidentialBurstController",
    "TrafficBudgetManager",
    "ProxyFlowController",
    "ProxyFlowState",
    "load_proxy_policy_files",
    "build_proxy_controller",
    "load_residential_proxy_list",
]


def _load_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
        if not isinstance(data, dict):
            raise ValueError(f"Configuration {path} must be a mapping")
        return data


def load_proxy_policy_files(base_dir: str) -> Dict[str, Any]:
    base = Path(base_dir)
    global_cfg = _load_yaml(base / "global.yml")
    proxy_cfg = _load_yaml(base / "proxy_pools.yml")

    site_profiles: Dict[str, Any] = {}
    site_dir = base / "site_profiles"
    if site_dir.exists():
        for file in site_dir.glob("*.yml"):
            if file.name.startswith("_"):
                continue
            profile = _load_yaml(file)
            domain = profile.get("domain") or file.stem
            site_profiles[domain] = profile

    return {
        "global": global_cfg,
        "proxies": proxy_cfg,
        "sites": site_profiles,
    }


def load_residential_proxy_list(
    path: Path, scheme: str = "http"
) -> List[str]:
    """Load residential proxy URLs from a text file.

    Each line is expected in the format host:port:user:password.
    Empty lines and comments (#) are ignored.
    """

    proxies: List[str] = []
    if not path.exists():
        return proxies

    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue

            parts = line.split(":")
            if len(parts) == 4:
                host, port, username, password = parts
            elif len(parts) == 3:
                host, port, username = parts
                password = ""
            else:
                # Malformed line; skip silently for resilience
                continue

            auth = f"{username}:{password}@" if username else ""
            proxy_url = f"{scheme}://{auth}{host}:{port}"
            proxies.append(proxy_url)

    return proxies


def build_proxy_controller(base_dir: str) -> ProxyFlowController:
    cfg = load_proxy_policy_files(base_dir)
    residential_cfg = cfg["proxies"].get("residential", {}) if cfg["proxies"] else {}
    controller = ProxyFlowController(
        global_config=cfg.get("global", {}),
        proxy_configs=cfg.get("proxies", {}),
        site_profiles=cfg.get("sites", {}),
        residential_config=residential_cfg,
    )
    return controller
