#!/usr/bin/env python3
"""Automated cleanup scheduler for recurring maintenance."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

if __package__ is None or __package__ == "":  # pragma: no cover - script execution support
    sys.path.append(str(Path(__file__).resolve().parents[1]))

from utils.cleanup_manager import CleanupConfig, CleanupError, CleanupManager
from utils.serialization import json_dumps

LOGGER = logging.getLogger("automated_cleanup")
CONFIG_PATH = Path("config/cleanup_config.json")
DEFAULT_DISK_THRESHOLD = 0.85


@dataclass
class CleanupPolicy:
    name: str
    strategies: Sequence[str]
    interval: timedelta
    last_run: Optional[datetime] = None
    max_temp_age_days: int = 7
    dry_run: bool = False

    def should_run(self, current_time: datetime) -> bool:
        if current_time.tzinfo is None:
            raise ValueError("current_time must be timezone-aware UTC")
        if self.last_run is None:
            return True
        return current_time - self.last_run >= self.interval

    def mark_run(self, run_time: datetime) -> None:
        self.last_run = run_time


@dataclass
class SchedulerConfig:
    policies: Dict[str, CleanupPolicy] = field(default_factory=dict)
    disk_threshold: float = DEFAULT_DISK_THRESHOLD
    environment: str = "development"
    dry_run: bool = False


class AutomatedCleanupScheduler:
    def __init__(
        self,
        config: SchedulerConfig,
        manager_factory=lambda dry_run, age: CleanupManager(
            config=CleanupConfig(
                dry_run=dry_run,
                max_temp_age_days=age,
                interactive=False
            )
        ),
    ) -> None:
        self.config = config
        self.manager_factory = manager_factory
        self._stop = False

    @classmethod
    def from_file(
        cls,
        config_path: Path = CONFIG_PATH,
        *,
        environment: str = "development",
        dry_run: bool = False,
    ) -> "AutomatedCleanupScheduler":
        if not config_path.exists():
            raise FileNotFoundError(f"Cleanup configuration missing: {config_path}")

        data = json.loads(config_path.read_text())
        automation = data.get("automation", {})
        schedules = automation.get("schedules", {})

        policies: Dict[str, CleanupPolicy] = {}
        for name, cadence in schedules.items():
            strategies = cls._strategies_for_policy(name)
            interval = cls._interval_for_cadence(cadence)
            max_age = data.get("temporary_file_cleanup", {}).get("age_thresholds", {}).get("default", 7)
            policies[name] = CleanupPolicy(
                name=name,
                strategies=strategies,
                interval=interval,
                max_temp_age_days=max_age,
                dry_run=dry_run,
            )

        disk_threshold = automation.get("disk_threshold", DEFAULT_DISK_THRESHOLD)
        scheduler_config = SchedulerConfig(
            policies=policies,
            disk_threshold=disk_threshold,
            environment=environment,
            dry_run=dry_run,
        )
        return cls(scheduler_config)

    @staticmethod
    def _strategies_for_policy(policy_name: str) -> Sequence[str]:
        mapping = {
            "temporary_cleanup": ["temporary-files"],
            "legacy_cleanup": ["legacy-files", "empty-directories"],
            "deep_cleanup": ["legacy-files", "empty-directories", "duplicate-configs", "naming-standardisation"],
            "seasonal_cleanup": ["legacy-files", "duplicate-configs"],
        }
        return mapping.get(policy_name, ["legacy-files"])

    @staticmethod
    def _interval_for_cadence(cadence: str) -> timedelta:
        cadence = cadence.lower()
        if cadence == "daily":
            return timedelta(days=1)
        if cadence == "weekly":
            return timedelta(weeks=1)
        if cadence == "monthly":
            return timedelta(days=30)
        if cadence == "quarterly":
            return timedelta(days=90)
        return timedelta(days=7)

    def run_policy(self, policy_name: str) -> Dict[str, Dict[str, Any]]:
        if policy_name not in self.config.policies:
            raise ValueError(f"Unknown policy: {policy_name}")
        policy = self.config.policies[policy_name]
        manager = self.manager_factory(policy.dry_run, policy.max_temp_age_days)

        LOGGER.info("Running cleanup policy '%s' (strategies=%s, dry_run=%s)", policy_name, policy.strategies, policy.dry_run)
        results: Dict[str, Dict[str, Any]] = {}
        for strategy in policy.strategies:
            results[strategy] = manager.execute_strategy(strategy)
        policy.mark_run(datetime.now(timezone.utc))
        return results

    def disk_usage_ratio(self, path: Path = Path(".")) -> float:
        from shutil import disk_usage

        try:
            usage = disk_usage(path)
        except OSError:
            return 0.0
        if usage.total == 0:
            return 0.0
        return usage.used / usage.total

    def should_trigger_disk_cleanup(self) -> bool:
        ratio = self.disk_usage_ratio(Path("."))
        LOGGER.debug("Disk usage ratio: %.2f", ratio)
        return ratio >= self.config.disk_threshold

    def run_once(self, policies: Sequence[str]) -> Dict[str, Dict[str, Dict[str, Any]]]:
        results: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for policy in policies:
            results[policy] = self.run_policy(policy)
        return results

    def serve_forever(self, policies: Optional[Sequence[str]] = None, poll_seconds: int = 300) -> None:
        LOGGER.info("Starting automated cleanup scheduler for environment=%s", self.config.environment)
        selected = list(policies) if policies else list(self.config.policies.keys())
        while not self._stop:
            now = datetime.now(timezone.utc)
            for name in selected:
                policy = self.config.policies.get(name)
                if policy is None:
                    continue
                if policy.should_run(now) or self.should_trigger_disk_cleanup():
                    try:
                        self.run_policy(name)
                    except CleanupError as exc:
                        LOGGER.error("Cleanup policy '%s' failed: %s", name, exc)
            time.sleep(poll_seconds)

    def stop(self) -> None:
        self._stop = True


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s %(message)s")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Automated cleanup scheduler")
    parser.add_argument("--policy", action="append", help="Cleanup policy to run (can be provided multiple times)")
    parser.add_argument("--run-once", action="store_true", help="Run the selected policies once and exit")
    parser.add_argument("--environment", default="development", help="Environment identifier")
    parser.add_argument("--dry-run", action="store_true", help="Execute policies in dry-run mode")
    parser.add_argument("--poll-seconds", type=int, default=300, help="Scheduler poll interval in seconds")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    configure_logging(args.verbose)

    try:
        scheduler = AutomatedCleanupScheduler.from_file(
            CONFIG_PATH,
            environment=args.environment,
            dry_run=args.dry_run,
        )
    except FileNotFoundError as exc:
        LOGGER.error(str(exc))
        raise SystemExit(1) from exc

    selected_policies = args.policy or list(scheduler.config.policies.keys())

    if args.run_once:
        results = scheduler.run_once(selected_policies)
        LOGGER.info("Cleanup summary: %s", json_dumps(results))
        raise SystemExit(0)

    def _handle_signal(signum, _frame):
        LOGGER.info("Received signal %s, shutting down scheduler", signum)
        scheduler.stop()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        scheduler.serve_forever(selected_policies, poll_seconds=args.poll_seconds)
    except KeyboardInterrupt:
        LOGGER.info("Scheduler interrupted by user")
    raise SystemExit(0)


if __name__ == "__main__":
    main()
