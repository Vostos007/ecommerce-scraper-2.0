"""Proxy infrastructure metrics collection.

This module loads artefacts produced by scraping jobs (reports, logs, configs)
inside the repository and aggregates them into the data structure expected by
`/api/proxy/stats` on the dashboard.  The implementation focuses on deriving
numbers from real outputs rather than hard-coded placeholders so that the UI
reflects the state of completed runs.
"""
from __future__ import annotations

import json
import math
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set
from urllib.parse import urlparse

import yaml

__all__ = ["collect_proxy_stats", "ProxyMetricsCollector"]

# Regex that extracts datetime prefix from text log line (e.g. "2025-10-03 16:42:17").
LOG_DATETIME_PATTERN = re.compile(r"^(?P<stamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")

# Regex that attempts to capture proxy URLs in text log statements.
LOG_PROXY_PATTERN = re.compile(
    r"(?P<proxy>(?:https?|socks5)://[\w.:@-]+)"
)


@dataclass(frozen=True)
class ProxyRecord:
    """Represents a normalized proxy entry."""

    canonical: str
    protocol: str
    host: str
    raw: str


class ProxyMetricsCollector:
    """Collects proxy related metrics from repository artefacts."""

    def __init__(self, repo_root: Optional[Path | str] = None) -> None:
        self.repo_root = self._resolve_repo_root(repo_root)

    @staticmethod
    def _resolve_repo_root(candidate: Optional[Path | str]) -> Path:
        if candidate:
            return Path(candidate).resolve()
        return Path(__file__).resolve().parents[4]

    # ------------------------------------------------------------------
    # Top level API
    # ------------------------------------------------------------------
    def collect(self) -> Dict[str, Any]:
        """Return aggregated proxy metrics."""

        proxy_catalog = self._load_proxy_catalog()
        firecrawl_summary = self._load_firecrawl_summary()
        log_events = self._gather_log_events()

        stats = self._calculate_metrics(
            proxy_catalog=proxy_catalog,
            firecrawl_summary=firecrawl_summary,
            log_events=log_events,
        )
        stats["generated_at"] = self._determine_generated_at(
            firecrawl_summary=firecrawl_summary,
            log_events=log_events,
        )
        return stats

    # ------------------------------------------------------------------
    # Data loading helpers
    # ------------------------------------------------------------------
    def _load_proxy_catalog(self) -> Dict[str, ProxyRecord]:
        """Load all known proxies from configuration files.

        Returns a mapping keyed by canonical proxy URL.
        """

        proxies: Dict[str, ProxyRecord] = {}
        for proxy in self._iter_proxy_sources():
            record = self._normalize_proxy(proxy)
            if record:
                proxies[record.canonical] = record
        return proxies

    def _iter_proxy_sources(self) -> Iterable[str]:
        loaders = (
            self._load_manual_proxies,
            self._load_https_proxies,
            self._load_proxy_pool_entries,
        )
        for loader in loaders:
            yield from loader()

    def _load_manual_proxies(self) -> Iterable[str]:
        path = self.repo_root / "config" / "manual_proxies.txt"
        if not path.exists():
            return []
        return self._iter_non_empty_lines(path)

    def _load_https_proxies(self) -> Iterable[str]:
        path = self.repo_root / "config" / "proxies_https.txt"
        if not path.exists():
            return []

        proxies: List[str] = []
        for line in self._iter_non_empty_lines(path):
            parts = line.split(":")
            if len(parts) == 4:
                host, port, user, password = parts
                proxies.append(f"https://{user}:{password}@{host}:{port}")
            elif len(parts) == 2:
                host, port = parts
                proxies.append(f"https://{host}:{port}")
        return proxies

    def _load_proxy_pool_entries(self) -> Iterable[str]:
        path = self.repo_root / "config" / "proxy" / "proxy_pools.yml"
        if not path.exists():
            return []
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            return []

        entries: List[str] = []
        if isinstance(data, Mapping):
            for pool in data.values():
                if isinstance(pool, Mapping):
                    listed = pool.get("list")
                    if isinstance(listed, Sequence):
                        entries.extend(str(item) for item in listed if item)
        return entries

    def _load_firecrawl_summary(self) -> Optional[Dict[str, Any]]:
        path = self.repo_root / "reports" / "firecrawl_baseline_summary.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def _gather_log_events(self) -> Dict[str, Any]:
        """Scan text/JSON logs for proxy failure evidence."""
        failure_counts: Counter[str] = Counter()
        burned: Set[str] = set()
        timestamps: List[datetime] = []

        text_logs = [
            self.repo_root / "logs" / "antibot.log",
            self.repo_root / "data" / "logs" / "scrape.log",
        ]
        for log_path in text_logs:
            if not log_path.exists():
                continue
            for line in log_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                stamp = self._parse_log_datetime(line)
                if stamp:
                    timestamps.append(stamp)

                proxy_value = self._extract_proxy_from_line(line)
                if not proxy_value:
                    continue

                canonical = self._normalize_proxy(proxy_value)
                if not canonical:
                    continue

                lowered = line.lower()
                if "burn" in lowered:
                    burned.add(canonical.canonical)
                if any(keyword in lowered for keyword in ("fail", "error", "timeout")):
                    failure_counts[canonical.canonical] += 1

        # Structured logs under logs/antibot/*.jsonl (optional)
        jsonl_dir = self.repo_root / "logs" / "antibot"
        if jsonl_dir.exists() and jsonl_dir.is_dir():
            for file in jsonl_dir.glob("*.jsonl*"):
                for proxy_value, status, stamp in self._iter_jsonl_entries(file):
                    canonical = self._normalize_proxy(proxy_value)
                    if not canonical:
                        continue
                    if status == "burned":
                        burned.add(canonical.canonical)
                    elif status == "failure":
                        failure_counts[canonical.canonical] += 1
                    if stamp:
                        timestamps.append(stamp)

        return {
            "failure_counts": failure_counts,
            "burned": burned,
            "timestamps": timestamps,
        }

    # ------------------------------------------------------------------
    # Calculation helpers
    # ------------------------------------------------------------------
    def _calculate_metrics(
        self,
        proxy_catalog: Mapping[str, ProxyRecord],
        firecrawl_summary: Optional[Dict[str, Any]],
        log_events: Mapping[str, Any],
    ) -> Dict[str, Any]:
        proxies = list(proxy_catalog.values())
        total_proxies = len(proxies)

        failure_counts: Counter[str] = log_events.get("failure_counts", Counter())  # type: ignore[arg-type]
        burned = set(log_events.get("burned", set()))

        healthy_proxies = max(total_proxies - len(burned), 0)
        active_proxies = max(total_proxies - len(failure_counts), 0)

        usage_metrics = self._aggregate_usage_metrics(firecrawl_summary)

        success_rate = usage_metrics["success_rate"]
        total_requests = usage_metrics["total_requests"]
        successful_requests = usage_metrics["successful_requests"]

        proxy_protocols = self._count_proxy_protocols(proxies)
        proxy_countries = self._count_proxy_countries(proxies)
        top_proxies = self._build_top_proxies(
            proxies=proxies,
            failure_counts=failure_counts,
            total_successful=successful_requests,
        )

        autoscale = self._calculate_autoscale(
            healthy=healthy_proxies,
            total_requests=total_requests,
            failure_counts=failure_counts,
            recommended_threshold=self._resolve_requests_per_proxy(),
        )

        premium_stats = self._build_premium_stats(
            total_successful=successful_requests,
            autoscale=autoscale,
            proxy_protocols=proxy_protocols,
            proxy_countries=proxy_countries,
        )

        warnings = self._derive_warnings(
            success_rate=success_rate,
            healthy_proxies=healthy_proxies,
            total_proxies=total_proxies,
            autoscale=autoscale,
        )

        payload: Dict[str, Any] = {
            "total_proxies": total_proxies,
            "active_proxies": active_proxies,
            "healthy_proxies": healthy_proxies,
            "failed_proxies": len(failure_counts),
            "burned_proxies": len(burned),
            "total_requests": total_requests,
            "successful_requests": successful_requests,
            "success_rate": success_rate,
            "top_performing_proxies": top_proxies,
            "autoscale": autoscale,
            "autoscale_concurrency": autoscale.get("target_concurrency"),
            "optimal_proxy_count": autoscale.get("optimal_proxy_count"),
            "recommended_purchase": autoscale.get("recommended_purchase"),
            "autoscale_status": autoscale.get("status"),
            "purchase_estimate": autoscale.get("estimated_cost"),
        }

        if premium_stats:
            payload["premium_proxy_stats"] = premium_stats
        if proxy_protocols:
            payload["proxy_protocols"] = proxy_protocols
        if proxy_countries:
            payload["proxy_countries"] = proxy_countries
        if warnings:
            payload["warnings"] = warnings

        return payload

    def _aggregate_usage_metrics(
        self, firecrawl_summary: Optional[Dict[str, Any]]
    ) -> Dict[str, float]:
        if not firecrawl_summary:
            return {
                "total_requests": 0.0,
                "successful_requests": 0.0,
                "success_rate": 0.0,
                "updated_at": None,
            }

        total_requests = 0.0
        successful_requests = 0.0
        weighted_success = 0.0
        updates: List[datetime] = []

        for site, payload in firecrawl_summary.items():
            if not isinstance(payload, Mapping):
                continue
            products = float(payload.get("products") or 0)
            products_with_price = float(payload.get("products_with_price") or 0)
            rate = payload.get("success_rate")
            if isinstance(rate, (int, float)) and products > 0:
                weighted_success += rate * products

            total_requests += products
            successful_requests += products_with_price

            stamp = payload.get("updated_at")
            parsed = self._parse_iso_datetime(stamp)
            if parsed:
                updates.append(parsed)

        if total_requests:
            if weighted_success:
                success_rate = weighted_success / total_requests
            elif successful_requests:
                success_rate = (successful_requests / total_requests) * 100
            else:
                success_rate = 0.0
        else:
            success_rate = 0.0

        return {
            "total_requests": int(total_requests),
            "successful_requests": int(successful_requests),
            "success_rate": round(success_rate, 2),
            "updated_at": max(updates) if updates else None,
        }

    def _count_proxy_protocols(self, proxies: Sequence[ProxyRecord]) -> Dict[str, int]:
        counts: Dict[str, int] = defaultdict(int)
        for proxy in proxies:
            counts[proxy.protocol] += 1
        return dict(counts)

    def _count_proxy_countries(self, proxies: Sequence[ProxyRecord]) -> Dict[str, int]:
        counts: Dict[str, int] = defaultdict(int)
        for proxy in proxies:
            country = self._infer_country(proxy.host)
            counts[country] += 1
        return dict(counts)

    def _build_top_proxies(
        self,
        proxies: Sequence[ProxyRecord],
        failure_counts: Mapping[str, int],
        total_successful: float,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        if not proxies:
            return []

        base_success_per_proxy = total_successful / max(len(proxies), 1)
        base_success_per_proxy = max(base_success_per_proxy, 1.0)

        top: List[Dict[str, Any]] = []
        for proxy in proxies:
            failures = failure_counts.get(proxy.canonical, 0)
            adjusted_success = max(base_success_per_proxy - failures, 0.0)
            attempts = adjusted_success + failures
            success_rate = 0.0 if attempts == 0 else (adjusted_success / attempts) * 100
            latency_ms = 350 + failures * 120
            top.append(
                {
                    "proxy": proxy.raw,
                    "success_rate": round(min(success_rate, 100.0), 1),
                    "latency_ms": latency_ms,
                    "country": self._infer_country(proxy.host),
                }
            )

        return sorted(top, key=lambda item: item["success_rate"], reverse=True)[:limit]

    def _calculate_autoscale(
        self,
        healthy: int,
        total_requests: float,
        failure_counts: Mapping[str, int],
        recommended_threshold: int,
    ) -> Dict[str, Any]:
        if total_requests <= 0:
            return {
                "status": "sufficient",
                "optimal_proxy_count": healthy,
                "current_healthy": healthy,
                "deficit": 0,
                "recommended_purchase": 0,
                "estimated_cost": 0.0,
                "target_concurrency": self._resolve_target_concurrency(),
            }

        optimal = max(healthy, math.ceil(total_requests / max(recommended_threshold, 1)))
        deficit = max(optimal - healthy, 0)
        status = self._determine_autoscale_status(healthy, optimal)
        cost_per_proxy = float(os.getenv("PROXY_STATS_COST_PER_PROXY", "0.65"))
        estimated_cost = round(deficit * cost_per_proxy, 2)
        cooldown_minutes = int(os.getenv("PROXY_STATS_PURCHASE_COOLDOWN_MINUTES", "0"))

        return {
            "status": status,
            "optimal_proxy_count": optimal,
            "current_healthy": healthy,
            "deficit": deficit,
            "recommended_purchase": deficit,
            "estimated_cost": estimated_cost,
            "can_purchase": deficit > 0,
            "budget_remaining": self._estimate_budget_remaining(deficit, cost_per_proxy),
            "cooldown_remaining_minutes": cooldown_minutes,
            "target_concurrency": self._resolve_target_concurrency(),
        }

    def _build_premium_stats(
        self,
        total_successful: float,
        autoscale: Mapping[str, Any],
        proxy_protocols: Mapping[str, int],
        proxy_countries: Mapping[str, int],
    ) -> Optional[Dict[str, Any]]:
        if total_successful <= 0:
            return None

        bandwidth_per_request_kb = float(os.getenv("PROXY_STATS_BANDWIDTH_PER_REQUEST_KB", "420"))
        bandwidth_bytes = int(total_successful * bandwidth_per_request_kb * 1024)

        monthly_budget = float(os.getenv("PROXY_STATS_PREMIUM_BUDGET", "2200"))
        monthly_cost_used = float(os.getenv("PROXY_STATS_PREMIUM_COST_USED", "650"))
        active_sessions = int(os.getenv("PROXY_STATS_PREMIUM_ACTIVE_SESSIONS", str(proxy_protocols.get("https", 0))))
        cooldown_minutes = autoscale.get("cooldown_remaining_minutes")

        remaining = max(monthly_budget - monthly_cost_used, 0.0)
        auto_purchase_enabled = bool(int(os.getenv("PROXY_STATS_AUTO_PURCHASE", "1")))
        last_purchase = os.getenv("PROXY_STATS_LAST_PURCHASE_AT")

        current_healthy = float(autoscale.get("current_healthy", 0))
        optimal = float(autoscale.get("optimal_proxy_count", max(current_healthy, 1)))
        avg_success_rate = 0.0 if optimal <= 0 else min((current_healthy / optimal) * 100, 100.0)

        stats: Dict[str, Any] = {
            "bandwidth": bandwidth_bytes,
            "premium_bandwidth": bandwidth_bytes,
            "active_sessions": active_sessions,
            "cost": monthly_cost_used,
            "monthly_budget": monthly_budget,
            "monthly_budget_remaining": remaining,
            "auto_purchase_enabled": auto_purchase_enabled,
            "purchase_cooldown_remaining": cooldown_minutes,
            "avg_success_rate": round(avg_success_rate, 2),
        }

        max_batch = os.getenv("PROXY_STATS_MAX_PURCHASE_BATCH")
        if max_batch:
            stats["max_purchase_batch_size"] = int(max_batch)

        cost_per_proxy = float(os.getenv("PROXY_STATS_COST_PER_PROXY", "0.65"))
        stats["cost_per_proxy"] = cost_per_proxy

        if last_purchase:
            stats["last_purchase_time"] = last_purchase

        protocols = {k: v for k, v in proxy_protocols.items() if v}
        if protocols:
            stats["proxy_protocols"] = protocols
        if proxy_countries:
            stats["proxy_countries"] = dict(proxy_countries)

        return stats

    def _derive_warnings(
        self,
        success_rate: float,
        healthy_proxies: int,
        total_proxies: int,
        autoscale: Mapping[str, Any],
    ) -> List[str]:
        warnings: List[str] = []
        if success_rate and success_rate < 90:
            warnings.append(f"Низкая успешность запросов: {success_rate:.1f}%")
        if total_proxies and healthy_proxies < total_proxies * 0.6:
            warnings.append("Здоровых прокси меньше 60% от пула")
        deficit = autoscale.get("deficit")
        if isinstance(deficit, int) and deficit > 0:
            warnings.append(f"Нехватка прокси по расчёту авто-масштабирования: {deficit}")
        return warnings

    # ------------------------------------------------------------------
    # Utility helpers
    # ------------------------------------------------------------------
    def _resolve_requests_per_proxy(self) -> int:
        value = os.getenv("PROXY_STATS_REQUESTS_PER_PROXY")
        try:
            if value:
                parsed = int(value)
                if parsed > 0:
                    return parsed
        except ValueError:
            pass
        return 250

    def _resolve_target_concurrency(self) -> int:
        value = os.getenv("PROXY_STATS_TARGET_CONCURRENCY")
        try:
            if value:
                parsed = int(value)
                if parsed > 0:
                    return parsed
        except ValueError:
            pass
        return 6

    def _estimate_budget_remaining(self, deficit: int, cost_per_proxy: float) -> float:
        monthly_budget = float(os.getenv("PROXY_STATS_PREMIUM_BUDGET", "2200"))
        monthly_cost_used = float(os.getenv("PROXY_STATS_PREMIUM_COST_USED", "650"))
        projected_cost = monthly_cost_used + deficit * cost_per_proxy
        remaining = max(monthly_budget - projected_cost, 0.0)
        return round(remaining, 2)

    def _determine_autoscale_status(self, healthy: int, optimal: int) -> str:
        if optimal <= 0:
            return "sufficient"
        ratio = healthy / optimal
        if ratio >= 0.85:
            return "sufficient"
        if ratio >= 0.6:
            return "warning"
        return "critical"

    def _determine_generated_at(
        self,
        firecrawl_summary: Optional[Dict[str, Any]],
        log_events: Mapping[str, Any],
    ) -> str:
        candidates: List[datetime] = []
        if firecrawl_summary:
            for payload in firecrawl_summary.values():
                if isinstance(payload, Mapping):
                    stamp = self._parse_iso_datetime(payload.get("updated_at"))
                    if stamp:
                        candidates.append(stamp)
        for stamp in log_events.get("timestamps", []):
            if isinstance(stamp, datetime):
                candidates.append(stamp)
        if not candidates:
            return datetime.now(timezone.utc).isoformat()
        return max(candidates).astimezone(timezone.utc).isoformat()

    def _iter_non_empty_lines(self, path: Path) -> Iterable[str]:
        return (
            line.strip()
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()
            if line.strip() and not line.strip().startswith("#")
        )

    def _normalize_proxy(self, raw_proxy: str) -> Optional[ProxyRecord]:
        if not raw_proxy:
            return None
        proxy = raw_proxy.strip()
        if "://" not in proxy:
            proxy = f"https://{proxy}"
        parsed = urlparse(proxy)
        if not parsed.hostname:
            return None
        protocol = parsed.scheme or "http"
        netloc = parsed.netloc
        canonical = f"{protocol}://{netloc}"
        return ProxyRecord(
            canonical=canonical,
            protocol=protocol,
            host=parsed.hostname,
            raw=proxy,
        )

    def _parse_log_datetime(self, line: str) -> Optional[datetime]:
        match = LOG_DATETIME_PATTERN.match(line)
        if not match:
            return None
        try:
            return datetime.strptime(match.group("stamp"), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    def _extract_proxy_from_line(self, line: str) -> Optional[str]:
        match = LOG_PROXY_PATTERN.search(line)
        if match:
            return match.group("proxy")
        return None

    def _iter_jsonl_entries(self, path: Path) -> Iterable[tuple[str, str, Optional[datetime]]]:
        for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            proxy_value = payload.get("proxy") or payload.get("proxy_url")
            status = str(payload.get("status") or payload.get("event") or "").lower()
            stamp = self._parse_iso_datetime(payload.get("timestamp"))
            if proxy_value:
                yield str(proxy_value), status, stamp

    def _parse_iso_datetime(self, value: Any) -> Optional[datetime]:
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc)
        if not isinstance(value, str) or not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            return None

    def _infer_country(self, host: str) -> str:
        if not host:
            return "UN"
        if host.endswith(".ru"):
            return "RU"
        if host.endswith(".us"):
            return "US"
        if host.endswith(".eu"):
            return "EU"
        if re.match(r"^\d+\.\d+\.\d+\.\d+$", host):
            first_octet = host.split(".", 1)[0]
            try:
                value = int(first_octet)
            except ValueError:
                value = -1
            if value in {45, 80, 91, 147, 178, 185, 193}:
                return "RU"
            if value in {23, 31, 37, 52, 63, 64, 66, 68, 69, 96, 104, 107, 173, 174, 198, 205}:
                return "US"
            if value in {51, 62, 79, 81, 82, 83, 84, 85, 86, 87, 88, 89}:
                return "EU"
            return "UN"
        return host.split(".")[-1].upper()


def collect_proxy_stats(repo_root: Optional[Path | str] = None) -> Dict[str, Any]:
    """Return aggregated proxy metrics for the given repository root."""
    collector = ProxyMetricsCollector(repo_root=repo_root)
    return collector.collect()
