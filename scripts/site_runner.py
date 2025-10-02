#!/usr/bin/env python3
"""Shared CLI that runs a single site scrape with Rich progress output."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from rich import box
from rich.align import Align
from rich.console import Console, RenderableType
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.text import Text

from database.history_writer import (
    export_site_history_to_csv,
    export_site_history_to_json,
)
from run_sites import load_sites_config, parse_method, run_site
from network.firecrawl_client import FirecrawlClient
from utils.data_paths import get_site_paths
from utils.url_cache_builder import refresh_cached_urls
from utils.rich_helpers import (
    create_tracker,
    format_phase_indicator,
    format_progress_status,
    ProgressThrottler,
    render_error,
    status_spinner,
)
from utils.rich_themes import get_console
from core.types import (
    ProgressEvent,
    ProgressCallback,
    PHASE_COMPLETE,
    PHASE_DISCOVERY,
    PHASE_SCRAPING,
)


@dataclass(frozen=True)
class BatchSpec:
    urls: Optional[List[str]]
    label: str


class DebugAbortError(RuntimeError):
    """Raised when the debug monitor decides the run must stop."""


class DebugMonitor:
    """Lightweight watchdog that records progress and aborts on stalls/errors."""

    _ERROR_TOKENS = (
        "timeout",
        "тайм-аут",
        "http 404",
        "404",
        "not found",
        "no element",
    )

    def __init__(
        self,
        *,
        domain: str,
        stall_timeout_seconds: int = 300,
        error_threshold: int = 25,
    ) -> None:
        self.domain = domain
        self.stall_timeout = max(0, stall_timeout_seconds)
        self.error_threshold = max(0, error_threshold)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        debug_dir = Path("logs") / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = debug_dir / f"{domain}-{timestamp}.log"
        self.phase_progress: Dict[str, int] = {}
        self.consecutive_errors = 0
        self.last_progress_time = time.time()
        self._aborted = False
        self._write({
            "type": "start",
            "ts": self._timestamp(),
            "domain": domain,
            "stall_timeout_seconds": self.stall_timeout,
            "consecutive_error_threshold": self.error_threshold,
        })

    def _timestamp(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z"

    def _write(self, payload: Dict[str, object]) -> None:
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def handle_event(self, event: ProgressEvent) -> None:
        message_raw = event.message if isinstance(event.message, str) else str(event.message or "")
        payload = {
            "type": "event",
            "ts": self._timestamp(),
            "phase": getattr(event.phase, "value", str(event.phase)),
            "current": event.current,
            "total": event.total,
            "message": message_raw,
        }
        self._write(payload)

        phase_key = str(getattr(event.phase, "value", event.phase))
        previous_value = self.phase_progress.get(phase_key, -1)
        progressed = event.current is not None and event.current > previous_value
        if progressed:
            self.phase_progress[phase_key] = event.current
            self.last_progress_time = time.time()
            self.consecutive_errors = 0
        else:
            self.phase_progress.setdefault(phase_key, event.current or previous_value)

        lowered_message = message_raw.lower()
        if any(token in lowered_message for token in self._ERROR_TOKENS):
            self.consecutive_errors += 1
        elif progressed:
            self.consecutive_errors = 0

        now = time.time()
        if self.stall_timeout and now - self.last_progress_time > self.stall_timeout:
            reason = (
                f"No progress detected for {int(now - self.last_progress_time)}s; "
                f"stall timeout is {self.stall_timeout}s"
            )
            self.log_abort(reason, context={"phase": phase_key})
            raise DebugAbortError(reason)

        if self.error_threshold and self.consecutive_errors >= self.error_threshold:
            reason = (
                f"Detected {self.consecutive_errors} consecutive error signals (threshold {self.error_threshold})"
            )
            self.log_abort(reason, context={"phase": phase_key, "message": message_raw})
            raise DebugAbortError(reason)

    def log_batch_summary(self, batch_label: str, summary: Dict[str, object]) -> None:
        failures = summary.get("failures") if isinstance(summary, dict) else None
        failure_count = 0
        error_hits = 0
        if isinstance(failures, dict):
            failure_count = len(failures)
            for details in failures.values():
                text = details if isinstance(details, str) else str(details)
                if any(token in text.lower() for token in self._ERROR_TOKENS):
                    error_hits += 1

        payload = {
            "type": "batch",
            "ts": self._timestamp(),
            "batch": batch_label,
            "summary": {
                "products": summary.get("products_found"),
                "variations": summary.get("variations_found"),
                "failures": failure_count,
            },
        }
        self._write(payload)

        if self.error_threshold and error_hits >= self.error_threshold:
            reason = (
                f"Batch {batch_label} logged {error_hits} error entries (threshold {self.error_threshold})"
            )
            self.log_abort(reason, context={"batch": batch_label})
            raise DebugAbortError(reason)

    def log_abort(self, reason: str, context: Optional[Dict[str, object]] = None) -> None:
        if self._aborted:
            return
        self._aborted = True
        payload: Dict[str, object] = {
            "type": "abort",
            "ts": self._timestamp(),
            "reason": reason,
        }
        if context:
            payload["context"] = context
        self._write(payload)

console: Console = get_console()
error_console: Console = get_console(stderr=True)


def _update_phase_task_progress(tracker, task_key: Optional[str], event: ProgressEvent) -> None:
    """Update per-phase tracker task without disturbing batch-level totals."""

    if not task_key:
        return
    if task_key not in tracker.task_ids:
        return

    total = event.total if event.total and event.total > 0 else None
    tracker.update(task_key, visible=True, total=total)
    tracker.update(task_key, completed=event.current)


def _parse_args(parser: argparse.ArgumentParser, argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser.add_argument(
        "--sites-config",
        type=Path,
        default=Path("config/sites.json"),
        help="Path to sites configuration JSON",
    )
    parser.add_argument(
        "--engine-config",
        type=Path,
        default=Path("config/settings.json"),
        help="Path to scraper settings JSON",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=0,
        help="If > 0, split cached URLs into batches of this size",
    )
    parser.add_argument(
        "--batch-offset",
        type=int,
        default=0,
        help="Number of batches to skip before processing",
    )
    parser.add_argument(
        "--batch-max",
        type=int,
        default=0,
        help="Maximum number of batches to process per site (0 = all)",
    )
    parser.add_argument(
        "--max-products",
        type=int,
        help="Override max_products for the site",
    )
    parser.add_argument(
        "--backend",
        choices=["auto", "httpx", "playwright", "flaresolverr"],
        help="Override backend for this run",
    )
    parser.add_argument(
        "--skip-cache-refresh",
        action="store_true",
        help="Do not refresh the cached URL list before scraping",
    )
    parser.add_argument(
        "--no-history-export",
        action="store_true",
        help="Skip exporting per-site history CSV/JSON after scraping",
    )
    parser.add_argument(
        "--rebuild-workbook",
        action="store_true",
        help="Regenerate the consolidated workbook after this run",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only display planned actions without executing the scraper",
    )
    return parser.parse_args(argv)


def _resolve_site(config_path: Path, domain: str) -> Dict[str, object]:
    sites_config = load_sites_config(config_path)
    for site in sites_config.get("sites", []):
        if site.get("domain", "").lower() == domain.lower():
            return {"site": site, "defaults": sites_config.get("defaults", {})}
    raise SystemExit(f"Site '{domain}' not found in {config_path}")


def _load_cached_urls(cache_path: Path) -> List[str]:
    if not cache_path.exists():
        return []
    return [line.strip() for line in cache_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _build_batches(
    all_urls: List[str],
    batch_size: int,
    offset: int,
    batch_max: int,
) -> List[BatchSpec]:
    if batch_size <= 0 or not all_urls:
        return [BatchSpec(urls=None, label="full-catalog")]

    total_batches = math.ceil(len(all_urls) / batch_size)
    start = min(max(offset, 0), total_batches)
    end = total_batches if batch_max <= 0 else min(total_batches, start + max(batch_max, 0))

    batches: List[BatchSpec] = []
    for idx, start_index in enumerate(range(start * batch_size, end * batch_size, batch_size), start=start + 1):
        chunk = all_urls[start_index : start_index + batch_size]
        if chunk:
            batches.append(BatchSpec(urls=chunk, label=f"batch-{idx:03d}"))

    if not batches:
        batches.append(BatchSpec(urls=None, label="full-catalog"))

    return batches


def run_site_cli(domain: str, display_name: Optional[str] = None, argv: Optional[Sequence[str]] = None) -> None:
    parser = argparse.ArgumentParser(description=f"Run scraper for {domain}")
    args = _parse_args(parser, argv)

    header_panel = Panel(
        Text(
            f"{display_name or domain}\nEngine config: {args.engine_config}\nSites config: {args.sites_config}",
            justify="center",
            style="accent",
        ),
        title="Site Runner",
        border_style="accent",
    )
    console.print(header_panel)

    try:
        site_bundle = _resolve_site(args.sites_config, domain)
    except SystemExit as exc:  # pragma: no cover - defensive, but we display via Rich
        render_error(str(exc), console=error_console)
        raise

    site = site_bundle["site"]
    defaults = site_bundle["defaults"]

    base_url = site.get("base_url") or site.get("url")
    if not base_url:
        render_error("Site configuration missing base_url", console=error_console)
        raise SystemExit(1)

    engine_settings: Dict[str, object] = {}
    try:
        with args.engine_config.open("r", encoding="utf-8") as cfg_fp:
            engine_settings = json.load(cfg_fp)
    except Exception as exc:  # noqa: BLE001 - non fatal, fallback to defaults
        error_console.print(
            Panel.fit(
                f"Failed to load engine config {args.engine_config}: {exc}",
                title="Engine Config",
                border_style="warning",
            )
        )

    firecrawl_client: Optional[FirecrawlClient] = None
    firecrawl_cfg = engine_settings.get("firecrawl") if isinstance(engine_settings, dict) else None
    if isinstance(firecrawl_cfg, dict):
        try:
            candidate = FirecrawlClient(firecrawl_cfg)
            if candidate.enabled and candidate.api_key:
                firecrawl_client = candidate
            else:
                firecrawl_client = candidate  # keep reference for logging consistency
        except Exception as exc:  # noqa: BLE001 - do not abort CLI
            error_console.print(
                Panel.fit(
                    f"Firecrawl client init failed: {exc}",
                    title="Firecrawl",
                    border_style="warning",
                )
            )

    site_paths = get_site_paths(domain)
    cache_path = Path(site_paths.cache_file)
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    debug_cfg = site.get("overrides", {}).get("debug_monitor", {})
    debug_monitor: Optional[DebugMonitor] = None
    if isinstance(debug_cfg, dict) and debug_cfg.get("enabled"):
        stall_timeout = int(debug_cfg.get("stall_timeout_seconds", 300) or 0)
        error_threshold = int(debug_cfg.get("consecutive_error_threshold", 25) or 0)
        debug_monitor = DebugMonitor(
            domain=domain,
            stall_timeout_seconds=stall_timeout,
            error_threshold=error_threshold,
        )
        console.print(
            Panel.fit(
                f"Debug monitor active\nLog: {debug_monitor.log_path}\n"
                f"Stall timeout: {stall_timeout}s | Error threshold: {error_threshold}",
                title="Debug Monitor",
                border_style="accent",
            )
        )

    backend_override = parse_method(args.backend) if args.backend else None
    tracker = create_tracker(console=console, transient=False)
    tracker.progress.columns = [
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=None),
        TaskProgressColumn(),
        TimeElapsedColumn(),
    ]

    total_products = 0
    total_variations = 0
    total_failures = 0
    batch_summaries: List[Dict[str, object]] = []
    batch_results: List[Dict[str, object]] = []

    metrics: Dict[str, object] = {
        "phase": "preparing",
        "progress_text": Text("Preparing", style="accent"),
        "refresh": "—",
        "batch_index": 0,
        "batches": 0,
        "products": 0,
        "variations": 0,
        "failures": 0,
        "urls_discovered": 0,
        "discovery_total": 0,
        "urls_scraped": 0,
        "scrape_total": 0,
    }

    throttler = ProgressThrottler(min_interval=0.1, max_skip=5)
    live_ref: Dict[str, Optional[Live]] = {"instance": None}
    phase_to_task = {
        PHASE_DISCOVERY: "phase_discovery",
        PHASE_SCRAPING: "phase_scrape",
    }

    def _progress_callback(event: ProgressEvent) -> None:
        metrics["phase"] = event.phase
        metrics["progress_text"] = format_progress_status(
            event.phase,
            event.current,
            event.total,
            event.message,
        )

        if debug_monitor:
            debug_monitor.handle_event(event)

        if event.phase == PHASE_DISCOVERY:
            metrics["urls_discovered"] = event.current
            metrics["discovery_total"] = event.total
        elif event.phase == PHASE_SCRAPING:
            metrics["urls_scraped"] = event.current
            metrics["scrape_total"] = event.total
        elif event.phase == PHASE_COMPLETE:
            metrics["urls_scraped"] = event.current
            metrics["scrape_total"] = event.total

        task_key = phase_to_task.get(event.phase)
        _update_phase_task_progress(tracker, task_key, event)

        if throttler.should_update():
            live = live_ref.get("instance")
            if live:
                live.update(_build_live_layout(progress, _metrics_panel()))

    progress_callback: ProgressCallback = _progress_callback

    def _metrics_panel() -> Panel:
        table = Table(box=box.MINIMAL_DOUBLE_HEAD, show_header=False, expand=True)
        table.add_column("Metric", style="table.header", width=18)
        table.add_column("Value", style="table.neutral")
        table.add_row("Phase", format_phase_indicator(str(metrics.get("phase", "preparing"))))
        table.add_row("Progress", metrics.get("progress_text", Text("—", style="muted")))
        discovery_total = metrics.get("discovery_total") or "—"
        table.add_row(
            "Discovered",
            Text(f"{metrics.get('urls_discovered', 0)} / {discovery_total}"),
        )
        scrape_total = metrics.get("scrape_total") or "—"
        table.add_row(
            "Scraped URLs",
            Text(f"{metrics.get('urls_scraped', 0)} / {scrape_total}"),
        )
        table.add_row("Products", str(metrics["products"]))
        table.add_row("Variations", str(metrics["variations"]))
        table.add_row("Failures", str(metrics["failures"]))
        table.add_row("Refresh", str(metrics["refresh"]))
        table.add_row("Batch", f"{metrics['batch_index']} / {metrics['batches']}")
        return Panel(table, title="Live Metrics", border_style="accent", padding=(1, 2))

    prep_task = tracker.add_task("prep", "Preparing", total=3)
    discovery_task = tracker.add_task(
        "discovery",
        "URL discovery",
        total=1,
        visible=not args.skip_cache_refresh and not args.dry_run,
    )
    scrape_task = tracker.add_task("scrape", "Scraping batches", total=1, visible=False)
    validate_task = tracker.add_task("validate", "Validating results", total=1, visible=False)
    export_task = tracker.add_task(
        "export",
        "Exporting artifacts",
        total=2,
        visible=not args.no_history_export and not args.dry_run,
    )
    phase_discovery_task = tracker.add_task(
        "phase_discovery",
        "URL discovery (phase)",
        total=1,
        visible=False,
    )
    phase_scrape_task = tracker.add_task(
        "phase_scrape",
        "URL scraping (phase)",
        total=1,
        visible=False,
    )

    refresh_count: Optional[int] = None
    cached_urls: List[str] = []
    batches: List[BatchSpec]

    with status_spinner("Loading cached URLs", console=console):
        cached_urls = _load_cached_urls(cache_path)
    tracker.advance("prep")

    if not args.skip_cache_refresh and not args.dry_run:
        tracker.update("discovery", visible=True, total=1)
        scraping_cfg = site.get("overrides", {}).get("scraping", {})
        merged_cfg: Dict[str, object] = {}
        merged_cfg.update(defaults.get("scraping", {}))
        merged_cfg.update(scraping_cfg)
        merged_cfg["cached_urls_file"] = str(cache_path)
        metrics["phase"] = PHASE_DISCOVERY
        metrics["progress_text"] = Text("Refreshing cached URLs", style="table.header")
        with status_spinner("Refreshing cached URLs", console=console):
            refresh_count = refresh_cached_urls(
                merged_cfg,
                base_url,
                firecrawl_client=firecrawl_client,
            )
        tracker.update("discovery", total=max(refresh_count or 1, 1))
        tracker.advance("discovery", refresh_count or 1)
        metrics["refresh"] = str(refresh_count or 0)
        with status_spinner("Reloading cached URLs", console=console):
            cached_urls = _load_cached_urls(Path(str(merged_cfg["cached_urls_file"])))
    else:
        tracker.update("discovery", visible=False)

    batches = _build_batches(cached_urls, args.batch_size, args.batch_offset, args.batch_max)
    metrics["batches"] = len(batches)
    tracker.advance("prep")

    tracker.update("scrape", visible=not args.dry_run, total=len(batches) or 1)
    tracker.update("validate", visible=not args.dry_run, total=len(batches) or 1)
    tracker.advance("prep")

    if args.dry_run:
        dry_table = Table(title="Dry-Run Batches", box=box.SIMPLE)
        dry_table.add_column("Batch", style="table.header")
        dry_table.add_column("URL Count", justify="right")
        for batch in batches:
            dry_table.add_row(batch.label, str(len(batch.urls or cached_urls)))
        console.print(dry_table)
        return

    def _build_live_layout(progress_renderable: RenderableType, metrics_renderable: RenderableType) -> Layout:
        layout = Layout(name="root")
        width = console.size.width if console.size else 120
        if width < 100:
            layout.split_column(
                Layout(name="progress", ratio=2, minimum_size=10),
                Layout(name="metrics", ratio=1, minimum_size=8),
            )
        else:
            layout.split_row(
                Layout(name="progress", ratio=3, minimum_size=60),
                Layout(name="metrics", ratio=1, minimum_size=32),
            )
        layout["progress"].update(progress_renderable)
        layout["metrics"].update(metrics_renderable)
        return layout

    progress = tracker.progress
    start_time = time.time()

    initial_layout = _build_live_layout(progress, _metrics_panel())

    with Live(initial_layout, console=console, refresh_per_second=10, transient=False) as live:
        live_ref["instance"] = live
        for index, batch in enumerate(batches, start=1):
            metrics["batch_index"] = index
            metrics["phase"] = "preparing"
            metrics["progress_text"] = Text(f"Scraping {batch.label}", style="accent")
            metrics["urls_discovered"] = 0
            metrics["discovery_total"] = 0
            metrics["urls_scraped"] = 0
            metrics["scrape_total"] = 0
            tracker.update("phase_discovery", completed=0, total=1, visible=False)
            tracker.update("phase_scrape", completed=0, total=1, visible=False)
            throttler.force()
            live.update(_build_live_layout(progress, _metrics_panel()))

            try:
                try:
                    asyncio.get_running_loop()
                except RuntimeError:
                    asyncio.set_event_loop(asyncio.new_event_loop())
                summary = run_site(
                    site,
                    defaults,
                    engine_config=args.engine_config,
                    backend_override=backend_override,
                    max_products_override=args.max_products if batch.urls is None else len(batch.urls),
                    email_override=None,
                    output_format_override=None,
                    timeout_override=None,
                    dry_run=False,
                    cached_urls_override=batch.urls or cached_urls,
                    skip_cache_refresh=True,
                    progress_callback=progress_callback,
                )
                if debug_monitor:
                    debug_monitor.log_batch_summary(batch.label, summary or {})
            except DebugAbortError as exc:
                metrics["progress_text"] = Text(str(exc), style="table.error")
                throttler.force()
                live.update(_build_live_layout(progress, _metrics_panel()))
                render_error(
                    "Run aborted by debug monitor",
                    details=f"{exc}\nLog file: {debug_monitor.log_path if debug_monitor else 'n/a'}",
                    console=error_console,
                )
                return
            except Exception as exc:  # noqa: BLE001
                metrics["progress_text"] = Text(
                    f"Error on {batch.label}", style="table.error"
                )
                live.update(_build_live_layout(progress, _metrics_panel()))
                render_error(
                    f"Batch {batch.label} failed",
                    details=str(exc),
                    console=error_console,
                )
                raise
            finally:
                throttler.force()
                live.update(_build_live_layout(progress, _metrics_panel()))

            batch_summaries.append(summary)

            batch_products = int(summary.get("products_found") or 0)
            batch_variations = int(summary.get("variations_found") or 0)
            batch_failures = len(summary.get("failures") or {})

            total_products += batch_products
            total_variations += batch_variations
            total_failures += batch_failures
            batch_results.append(
                {
                    "label": batch.label,
                    "products": batch_products,
                    "variations": batch_variations,
                    "failures": batch_failures,
                }
            )

            metrics.update(
                {
                    "products": total_products,
                    "variations": total_variations,
                    "failures": total_failures,
                }
            )
            metrics["phase"] = PHASE_COMPLETE
            metrics["progress_text"] = Text(
                f"Completed {batch.label}", style="table.success"
            )
            tracker.advance("scrape")
            tracker.advance("validate")
            tracker.update(
                "scrape",
                description=f"Scraping batches ({index}/{len(batches)})",
            )
            tracker.update(
                "validate",
                description=f"Validating results ({index}/{len(batches)})",
            )
            live.update(_build_live_layout(progress, _metrics_panel()))
    live_ref["instance"] = None

    run_duration = time.time() - start_time

    history_csv_path = None
    history_json_path = None
    if not args.no_history_export:
        metrics["progress_text"] = Text("Exporting CSV", style="table.header")
        with status_spinner("Exporting history CSV", console=console):
            history_csv_path = export_site_history_to_csv(domain)
        tracker.advance("export")

        metrics["progress_text"] = Text("Exporting JSON", style="table.header")
        with status_spinner("Exporting history JSON", console=console):
            history_json_path = export_site_history_to_json(domain)
        tracker.advance("export")

    latest_summary = batch_summaries[-1] if batch_summaries else {}
    backend_label = latest_summary.get("backend", "n/a")
    cache_size = len(cached_urls)

    history_rows = 0
    if history_csv_path and history_csv_path.exists():
        with history_csv_path.open("r", encoding="utf-8") as fp:
            history_rows = max(sum(1 for _ in fp) - 1, 0)

    if batch_results:
        batch_table = Table(title="Batch Results", box=box.SIMPLE_HEAD, highlight=True)
        batch_table.add_column("Batch", style="table.header", no_wrap=True)
        batch_table.add_column("Products", justify="right")
        batch_table.add_column("Variations", justify="right")
        batch_table.add_column("Failures", justify="right")
        for row in batch_results:
            failure_text = Text(
                str(row["failures"]),
                style="table.success" if row["failures"] == 0 else "table.error",
            )
            batch_table.add_row(
                row["label"],
                str(row["products"]),
                str(row["variations"]),
                failure_text,
            )
        console.print(batch_table)

    summary_table = Table(title="Scraping Summary", box=box.ROUNDED, highlight=True)
    summary_table.add_column("Metric", style="table.header", no_wrap=True)
    summary_table.add_column("Value", style="table.neutral")
    summary_table.add_row("Backend", str(backend_label))
    summary_table.add_row("Batches", str(len(batches)))
    summary_table.add_row("Products scraped", str(total_products))
    summary_table.add_row("Variations scraped", str(total_variations))
    summary_table.add_row("Failures", Text(str(total_failures), style="table.error" if total_failures else "table.success"))
    summary_table.add_row("Cache URLs", str(cache_size))
    summary_table.add_row("Run duration", f"{run_duration:.1f}s")
    if refresh_count is not None:
        summary_table.add_row("URL refresh", f"{refresh_count} URLs")
    if history_rows:
        summary_table.add_row("History rows", str(history_rows))
    console.print(summary_table)

    outputs_table = Table(title="Outputs", box=box.SIMPLE_HEAD, show_edge=False)
    outputs_table.add_column("Artifact", style="table.header", no_wrap=True)
    outputs_table.add_column("Path", style="table.neutral")
    outputs_table.add_row("Latest JSON", str(site_paths.latest_export))
    if history_csv_path:
        outputs_table.add_row("History CSV", str(history_csv_path))
    if history_json_path:
        outputs_table.add_row("History JSON", str(history_json_path))
    console.print(outputs_table)

    if args.rebuild_workbook and not args.dry_run:
        with status_spinner("Rebuilding history workbook", console=console):
            from build_history_workbook import main as build_history_workbook

            build_history_workbook()


__all__ = ["run_site_cli"]
