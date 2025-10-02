#!/usr/bin/env python3
"""Output proxy statistics as JSON for Next.js dashboard."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.proxy_rotator import ProxyRotator

ROOT = Path(__file__).resolve().parents[1]
PROXIES_TXT = ROOT / "config" / "proxies_https.txt"
MANUAL_PROXIES = ROOT / "config" / "manual_proxies.txt"

DEFAULT_TARGET_CONCURRENCY = 6


def load_proxies() -> List[str]:
    proxies: List[str] = []
    for source in (PROXIES_TXT, MANUAL_PROXIES):
        if not source.exists():
            continue
        for line in source.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            proxies.append(stripped)
    return proxies


def resolve_target_concurrency(stats: Dict[str, Any]) -> int:
    """Determine realistic concurrency target for autoscale heuristics."""

    env_value = os.getenv("PROXY_STATS_TARGET_CONCURRENCY")
    if env_value:
        try:
            value = int(env_value)
            if value > 0:
                return value
        except ValueError:
            pass

    healthy = int(stats.get("healthy_proxies", 0) or 0)
    total = int(stats.get("total_proxies", 0) or 0)

    if healthy <= 0 and total > 0:
        healthy = total

    if healthy <= 0:
        return DEFAULT_TARGET_CONCURRENCY

    # ограничиваемся реальным пулом, но не меньше 2
    return max(2, min(healthy, DEFAULT_TARGET_CONCURRENCY))


async def gather_stats() -> Dict[str, Any]:
    proxies = load_proxies()
    rotator = ProxyRotator(
        proxies,
        {
            "autoscale": {
                "enabled": True,
                "default_concurrency": 6,
                "safety_factor": 1.5,
                "target_success_rate": 0.85,
                "min_proxy_count": 4,
                "max_proxy_count": 100,
            }
        },
    )
    stop_error: Optional[Exception] = None
    try:
        try:
            await rotator.start()
        except Exception:  # noqa: BLE001
            # Background monitoring не критичен для сбора статистики
            pass
        stats = await rotator.get_proxy_statistics()

        concurrency = resolve_target_concurrency(stats)

        if getattr(rotator, "autoscale_enabled", True):
            try:
                autoscale = await rotator.get_autoscale_recommendations(concurrency)
                stats["autoscale"] = autoscale
                stats["optimal_proxy_count"] = autoscale.get("optimal_proxy_count")
                stats["recommended_purchase"] = autoscale.get(
                    "recommended_purchase"
                )
                stats["autoscale_status"] = autoscale.get("status")
                stats["purchase_estimate"] = autoscale.get("estimated_cost")
                stats["autoscale_concurrency"] = concurrency
            except Exception as autoscale_exc:  # noqa: BLE001
                stats.setdefault("warnings", []).append(
                    f"Не удалось рассчитать авто-масштабирование: {autoscale_exc}"
                )
    finally:
        stop_method = getattr(rotator, "stop", None)
        if callable(stop_method):
            try:
                result = stop_method()
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:  # noqa: BLE001
                stop_error = exc

    if stop_error:
        raise stop_error

    if not isinstance(stats, dict):
        raise RuntimeError("proxy statistics response is not a dictionary")

    warnings: List[str] = []

    healthy = int(stats.get("healthy_proxies", 0))
    total = int(stats.get("total_proxies", 0))
    burned = int(stats.get("burned_proxies", 0))
    total_requests = int(stats.get("total_requests", 0))
    successful_requests = int(stats.get("successful_requests", 0))
    success_rate = (
        (successful_requests / max(1, total_requests)) * 100.0
        if total_requests
        else None
    )
    stats["success_rate"] = success_rate

    if healthy < max(1, total // 4):
        warnings.append(
            f"Мало рабочих прокси: {healthy} из {total} в строю"
        )
    if burned > max(0, total * 0.2):
        warnings.append(
            f"Высокий процент сгоревших прокси ({burned} шт.)"
        )
    if success_rate is not None and success_rate < 70.0:
        warnings.append(
            f"Низкий success rate: {success_rate:.1f}%"
        )

    stats.setdefault("total_proxies", 0)
    stats.setdefault("total_requests", 0)
    if stats.get("success_rate") is None:
        stats.pop("success_rate", None)

    premium_raw = stats.get("premium_proxy_stats")
    premium: Dict[str, Any] | None = None
    if isinstance(premium_raw, dict):
        premium = {}

        def as_float(value: Any) -> Optional[float]:
            if value is None:
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        def as_int(value: Any) -> Optional[int]:
            if value is None:
                return None
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        bandwidth_gb = as_float(premium_raw.get("total_traffic_gb")) or 0.0
        premium["bandwidth"] = max(bandwidth_gb, 0.0) * 1024 * 1024 * 1024

        active_sessions = as_int(premium_raw.get("active_proxies"))
        if active_sessions is not None:
            premium["active_sessions"] = max(active_sessions, 0)

        estimated_cost = as_float(premium_raw.get("estimated_traffic_cost"))
        monthly_cost_used = as_float(premium_raw.get("monthly_cost_used")) or 0.0
        premium["cost"] = estimated_cost if estimated_cost is not None else monthly_cost_used

        monthly_budget_remaining = as_float(
            premium_raw.get("monthly_budget_remaining")
        )
        if monthly_budget_remaining is not None:
            premium["monthly_budget_remaining"] = max(
                monthly_budget_remaining, 0.0
            )

        monthly_budget: Optional[float] = None
        if monthly_budget_remaining is not None:
            monthly_budget = monthly_cost_used + monthly_budget_remaining
        elif monthly_cost_used:
            monthly_budget = monthly_cost_used
        if monthly_budget is not None:
            premium["monthly_budget"] = max(monthly_budget, 0.0)

        proxy_countries = premium_raw.get("proxy_countries")
        if isinstance(proxy_countries, dict) and proxy_countries:
            premium["proxy_countries"] = {
                str(country): max(as_int(count) or 0, 0)
                for country, count in proxy_countries.items()
            }

        proxy_protocols = premium_raw.get("proxy_protocols")
        if isinstance(proxy_protocols, dict) and proxy_protocols:
            premium["proxy_protocols"] = {
                str(protocol): max(as_int(count) or 0, 0)
                for protocol, count in proxy_protocols.items()
            }

        avg_response_time = as_float(premium_raw.get("avg_response_time"))
        if avg_response_time is not None:
            premium["avg_response_time"] = max(avg_response_time, 0.0)

        raw_rate = premium_raw.get("avg_success_rate")
        avg_rate = as_float(raw_rate)
        if avg_rate is not None:
            premium["avg_success_rate"] = max(avg_rate * 100.0, 0.0)

        premium["auto_purchase_enabled"] = bool(
            premium_raw.get("auto_purchase_enabled", False)
        )

        last_purchase_time = premium_raw.get("last_purchase_time")
        if isinstance(last_purchase_time, str) and last_purchase_time:
            premium["last_purchase_time"] = last_purchase_time

        purchase_cooldown = as_int(premium_raw.get("purchase_cooldown_remaining"))
        if purchase_cooldown is not None:
            premium["purchase_cooldown_remaining"] = max(purchase_cooldown, 0)

        max_batch_size = as_int(premium_raw.get("max_purchase_batch_size"))
        if max_batch_size is not None and max_batch_size > 0:
            premium["max_purchase_batch_size"] = max_batch_size

        cost_per_proxy = as_float(premium_raw.get("cost_per_proxy"))
        if cost_per_proxy is not None:
            premium["cost_per_proxy"] = max(cost_per_proxy, 0.0)

        if monthly_budget:
            used_percent = monthly_cost_used / monthly_budget if monthly_budget else 0.0
            if used_percent >= 0.9:
                warnings.append(
                    f"Расход премиум-прокси превысил {used_percent * 100:.0f}% бюджета"
                )

    stats["active_proxies"] = stats.get("active_proxies") or healthy
    if premium:
        stats["premium_proxy_stats"] = premium
        countries = premium.get("proxy_countries")
        protocols = premium.get("proxy_protocols")
        if countries and not stats.get("proxy_countries"):
            stats["proxy_countries"] = countries
        if protocols and not stats.get("proxy_protocols"):
            stats["proxy_protocols"] = protocols

    autoscale_data = stats.get("autoscale")
    if isinstance(autoscale_data, dict):
        status = autoscale_data.get("status")
        deficit = autoscale_data.get("deficit", 0)
        optimal_needed = autoscale_data.get("optimal_proxy_count", 0)
        current_healthy = autoscale_data.get("current_healthy", healthy)

        if status == "critical":
            warnings.append(
                "Критическая нехватка прокси: "
                f"{current_healthy} из {optimal_needed} необходимых"
            )
        elif status == "warning":
            warnings.append(
                f"Рекомендуется докупить {max(0, deficit)} прокси"
            )

        stats.setdefault("optimal_proxy_count", optimal_needed)
        stats.setdefault(
            "recommended_purchase", autoscale_data.get("recommended_purchase")
        )
        stats.setdefault("autoscale_status", status)
        stats.setdefault("purchase_estimate", autoscale_data.get("estimated_cost"))

    existing_warnings = stats.get("warnings")
    if isinstance(existing_warnings, list):
        warnings.extend(str(item) for item in existing_warnings)

    if warnings:
        stats["warnings"] = warnings

    stats["generated_at"] = datetime.now(timezone.utc).isoformat()

    return stats


def main() -> int:
    try:
        stats = asyncio.run(gather_stats())
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": str(exc)}))
        return 1
    print(json.dumps(stats, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
