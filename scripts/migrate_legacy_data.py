#!/usr/bin/env python3
"""Comprehensive cleanup and migration orchestrator."""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

if __package__ is None or __package__ == "":  # pragma: no cover - script execution support
    sys.path.append(str(Path(__file__).resolve().parents[1]))

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeRemainingColumn
from rich.table import Table

from utils.cleanup_manager import CleanupConfig, CleanupError, CleanupManager
from utils.data_paths import legacy_migration_report, migrate_all_legacy_files
from utils.serialization import json_dumps, prepare_for_json

console = Console()
DISK_USAGE_TARGETS: Sequence[Path] = (Path("data"), Path("logs"), Path("reports"))
CRITICAL_PATHS: Sequence[Path] = (Path("config/settings.json"), Path("main.py"))


@dataclass
class CleanupExecutionResult:
    """Lightweight container for cleanup results."""

    results: Dict[str, Any]
    before_usage: Dict[str, int]
    after_usage: Dict[str, int]
    validation: Dict[str, bool]


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s %(name)s - %(message)s")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cleanup legacy artefacts and temporary data")
    parser.add_argument("--domains", nargs="*", help="Specific domains to migrate (legacy support)")
    parser.add_argument("--run-migration", action="store_true", help="Execute the legacy migration workflow")
    parser.add_argument("--cleanup-legacy", action="store_true", help="Remove unused legacy files")
    parser.add_argument("--cleanup-empty-dirs", action="store_true", help="Remove empty directories")
    parser.add_argument("--cleanup-temp-files", action="store_true", help="Remove temporary files older than --max-age-days")
    parser.add_argument("--standardize-names", action="store_true", help="Apply naming conventions to files")
    parser.add_argument("--consolidate-configs", action="store_true", help="Merge duplicate configuration files")
    parser.add_argument("--full-cleanup", action="store_true", help="Run all cleanup operations")
    parser.add_argument("--dry-run", action="store_true", help="Simulate operations without modifying files")
    parser.add_argument("--max-age-days", type=int, default=7, help="Maximum age for temporary files in days")
    parser.add_argument("--yes", action="store_true", help="Assume yes for confirmation prompts")
    parser.add_argument("--skip-progress", action="store_true", help="Disable progress bars")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")
    return parser.parse_args()


def measure_disk_usage(targets: Sequence[Path]) -> Dict[str, int]:
    usage: Dict[str, int] = {}
    for target in targets:
        path = target.resolve()
        if not path.exists():
            usage[str(target)] = 0
            continue
        total = 0
        for file_path in path.rglob("*"):
            if file_path.is_file():
                try:
                    total += file_path.stat().st_size
                except OSError:
                    continue
        usage[str(target)] = total
    usage["total"] = sum(usage.values())
    return usage


def format_bytes(num_bytes: int) -> str:
    suffixes = ["B", "KB", "MB", "GB", "TB"]
    value = float(num_bytes)
    for suffix in suffixes:
        if value < 1024.0:
            return f"{value:.1f} {suffix}"
        value /= 1024.0
    return f"{value:.1f} PB"


def prompt_confirmation(message: str) -> bool:
    response = console.input(f"{message} [y/N]: ").strip().lower()
    return response in {"y", "yes"}


def run_migration(domains: Optional[Iterable[str]], dry_run: bool) -> int:
    if dry_run:
        report = legacy_migration_report()
        console.print_json(data=prepare_for_json(report))
        return 0

    migrated = migrate_all_legacy_files(domains)
    if not migrated:
        console.print("[yellow]No legacy artefacts discovered; nothing to migrate.[/yellow]")
    else:
        console.print(f"[green]Migrated data for {len(migrated)} domain(s):[/green]")
        for paths in migrated:
            console.print(f" - {paths.domain}: {paths.root}")

    report = legacy_migration_report()
    if report["remaining_exports"] or report["remaining_history"]:
        console.print("[red]Remaining legacy files detected:[/red]")
        console.print_json(data=prepare_for_json(report))
        return 1
    return 0


def _ensure_manager(
    *,
    dry_run: bool,
    max_age_days: int,
    interactive: bool,
    confirmation_callback: Optional[Callable[[str], bool]] = None,
    manager: Optional[CleanupManager] = None,
) -> CleanupManager:
    if manager is not None:
        manager.config.dry_run = dry_run
        manager.config.max_temp_age_days = max_age_days
        manager.config.interactive = interactive
        manager.config.confirmation_callback = confirmation_callback
        return manager

    config = CleanupConfig(
        dry_run=dry_run,
        max_temp_age_days=max_age_days,
        interactive=interactive,
        confirmation_callback=confirmation_callback,
    )
    return CleanupManager(config=config)


def run_legacy_cleanup(dry_run: bool, manager: Optional[CleanupManager] = None) -> Dict[str, Any]:
    mgr = _ensure_manager(
        dry_run=dry_run,
        max_age_days=7,
        interactive=not dry_run,
        confirmation_callback=prompt_confirmation if not dry_run else None,
        manager=manager,
    )
    return mgr.execute_strategy("legacy-files")


def run_directory_cleanup(dry_run: bool, manager: Optional[CleanupManager] = None) -> Dict[str, Any]:
    mgr = _ensure_manager(
        dry_run=dry_run,
        max_age_days=7,
        interactive=not dry_run,
        confirmation_callback=prompt_confirmation if not dry_run else None,
        manager=manager,
    )
    return mgr.execute_strategy("empty-directories")


def run_temp_file_cleanup(
    max_age_days: int,
    dry_run: bool,
    manager: Optional[CleanupManager] = None,
) -> Dict[str, Any]:
    mgr = _ensure_manager(
        dry_run=dry_run,
        max_age_days=max_age_days,
        interactive=not dry_run,
        confirmation_callback=prompt_confirmation if not dry_run else None,
        manager=manager,
    )
    mgr.config.max_temp_age_days = max_age_days
    return mgr.execute_strategy("temporary-files")


def run_naming_standardization(dry_run: bool, manager: Optional[CleanupManager] = None) -> Dict[str, Any]:
    mgr = _ensure_manager(
        dry_run=dry_run,
        max_age_days=7,
        interactive=not dry_run,
        confirmation_callback=prompt_confirmation if not dry_run else None,
        manager=manager,
    )
    return mgr.execute_strategy("naming-standardisation")


def run_config_consolidation(dry_run: bool, manager: Optional[CleanupManager] = None) -> Dict[str, Any]:
    mgr = _ensure_manager(
        dry_run=dry_run,
        max_age_days=7,
        interactive=not dry_run,
        confirmation_callback=prompt_confirmation if not dry_run else None,
        manager=manager,
    )
    return mgr.execute_strategy("duplicate-configs")


def run_full_cleanup(
    max_age_days: int,
    dry_run: bool,
    manager: Optional[CleanupManager] = None,
) -> Dict[str, Dict[str, Any]]:
    mgr = _ensure_manager(
        dry_run=dry_run,
        max_age_days=max_age_days,
        interactive=not dry_run,
        confirmation_callback=prompt_confirmation if not dry_run else None,
        manager=manager,
    )
    return mgr.run_full_cleanup()


def execute_selected_cleanup(
    manager: CleanupManager,
    strategies: Sequence[str],
    show_progress: bool,
) -> Dict[str, Any]:
    results: Dict[str, Any] = {}

    if not strategies:
        return results

    if show_progress:
        progress = Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            TimeRemainingColumn(),
            console=console,
            transient=True,
        )
        with progress:
            task_id = progress.add_task("Running cleanup operations", total=len(strategies))
            for name in strategies:
                progress.update(task_id, description=f"Processing {name}")
                results[name] = manager.execute_strategy(name)
                progress.advance(task_id)
    else:
        for name in strategies:
            console.log(f"Running cleanup strategy: {name}")
            results[name] = manager.execute_strategy(name)
    return results


def post_cleanup_validation() -> Dict[str, bool]:
    validation: Dict[str, bool] = {}
    for path in CRITICAL_PATHS:
        validation[str(path)] = Path(path).exists()
    return validation


def summarise_results(execution: CleanupExecutionResult, dry_run: bool) -> None:
    console.rule("Cleanup Summary")

    table = Table(title="Cleanup Operations", show_lines=False)
    table.add_column("Strategy")
    table.add_column("Details")

    for strategy, details in execution.results.items():
        table.add_row(strategy, json_dumps(details, ensure_ascii=False, indent=2))

    console.print(table)

    console.rule("Disk Usage")
    usage_table = Table(show_header=True, header_style="bold")
    usage_table.add_column("Target")
    usage_table.add_column("Before")
    usage_table.add_column("After")
    usage_table.add_column("Delta")

    for target, before_bytes in execution.before_usage.items():
        if target == "total":
            continue
        after_bytes = execution.after_usage.get(target, 0)
        delta = after_bytes - before_bytes
        usage_table.add_row(
            target,
            format_bytes(before_bytes),
            format_bytes(after_bytes),
            format_bytes(delta),
        )

    total_delta = execution.after_usage.get("total", 0) - execution.before_usage.get("total", 0)
    usage_table.add_row(
        "total",
        format_bytes(execution.before_usage.get("total", 0)),
        format_bytes(execution.after_usage.get("total", 0)),
        format_bytes(total_delta),
    )
    console.print(usage_table)

    console.rule("Validation")
    for path, exists in execution.validation.items():
        status = "[green]OK[/green]" if exists else "[red]Missing[/red]"
        console.print(f"{path}: {status}")

    if dry_run:
        console.print("[cyan]Dry-run mode enabled: no changes were applied.[/cyan]")

    recommendations: List[str] = []
    if not any(detail for detail in execution.results.values()):
        recommendations.append("Review cleanup configuration; no operations were executed.")
    if any(not exists for exists in execution.validation.values()):
        recommendations.append("Restore missing critical files before deploying.")
    if not recommendations:
        recommendations.append("Review the generated backups before deleting them permanently.")

    console.rule("Recommendations")
    for item in recommendations:
        console.print(f"- {item}")


def orchestrate_cleanup(args: argparse.Namespace) -> Optional[CleanupExecutionResult]:
    interactive = not args.dry_run and not args.yes
    confirmation_callback = prompt_confirmation if interactive else None

    manager = CleanupManager(
        config=CleanupConfig(
            dry_run=args.dry_run,
            max_temp_age_days=args.max_age_days,
            interactive=interactive,
            confirmation_callback=confirmation_callback,
        )
    )

    before_usage = measure_disk_usage(DISK_USAGE_TARGETS)

    selected: List[str] = []
    if args.full_cleanup:
        selected = list(manager.strategies.keys())
    else:
        if args.cleanup_legacy:
            selected.append("legacy-files")
        if args.cleanup_empty_dirs:
            selected.append("empty-directories")
        if args.cleanup_temp_files:
            selected.append("temporary-files")
        if args.standardize_names:
            selected.append("naming-standardisation")
        if args.consolidate_configs:
            selected.append("duplicate-configs")

    if not selected:
        return None

    try:
        results = execute_selected_cleanup(manager, selected, show_progress=not args.skip_progress)
    except CleanupError as exc:
        console.print(f"[red]Cleanup failed:[/red] {exc}")
        raise SystemExit(2) from exc

    after_usage = measure_disk_usage(DISK_USAGE_TARGETS)
    validation = post_cleanup_validation()

    return CleanupExecutionResult(
        results=results,
        before_usage=before_usage,
        after_usage=after_usage,
        validation=validation,
    )


def main() -> None:
    args = parse_args()
    configure_logging(args.verbose)

    exit_code = 0
    if args.run_migration:
        exit_code = run_migration(args.domains, args.dry_run)

    execution = orchestrate_cleanup(args)
    if execution is not None:
        summarise_results(execution, args.dry_run)

    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
