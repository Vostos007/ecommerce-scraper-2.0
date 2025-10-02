"""Utility helpers built on top of Rich for consistent CLI UX."""

from __future__ import annotations

import contextlib
import itertools
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from rich import box
from rich.align import Align
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.status import Status
from rich.table import Table
from rich.text import Text

from .rich_themes import RichThemeManager, get_console


@dataclass
class ProgressSnapshot:
    """Serializable snapshot of a task's progress for recovery."""

    description: str
    completed: float
    total: Optional[float]
    timestamp: float


@dataclass
class ProgressTracker:
    """Tracks multiple tasks with cumulative metrics."""

    progress: Progress
    task_ids: Dict[str, TaskID] = field(default_factory=dict)
    started_at: float = field(default_factory=time.perf_counter)

    def add_task(self, key: str, description: str, total: Optional[float] = None, **kwargs: Any) -> TaskID:
        task_id = self.progress.add_task(description, total=total, **kwargs)
        self.task_ids[key] = task_id
        return task_id

    def advance(self, key: str, amount: float = 1.0) -> None:
        task_id = self.task_ids[key]
        self.progress.advance(task_id, amount)

    def update(self, key: str, **kwargs: Any) -> None:
        task_id = self.task_ids[key]
        self.progress.update(task_id, **kwargs)

    def snapshot(self) -> Dict[str, ProgressSnapshot]:
        now = time.perf_counter()
        snapshots: Dict[str, ProgressSnapshot] = {}
        for key, task_id in self.task_ids.items():
            task = self.progress.tasks[task_id]
            snapshots[key] = ProgressSnapshot(
                description=task.description or key,
                completed=task.completed or 0.0,
                total=task.total,
                timestamp=now,
            )
        return snapshots


def create_progress(
    *,
    console: Optional[Console] = None,
    transient: bool = False,
    task_description_width: Optional[int] = None,
    show_eta: bool = True,
    show_completed: bool = True,
    expand: bool = True,
) -> Progress:
    _ = task_description_width  # legacy parameter retained for compatibility
    columns: List[Any] = [SpinnerColumn(), TextColumn("[progress.description]{task.description}", justify="left")]
    columns.append(BarColumn(bar_width=None))
    if show_completed:
        columns.append(MofNCompleteColumn())
    columns.append(TaskProgressColumn())
    if show_eta:
        columns.append(TimeRemainingColumn(elapsed_when_finished=True))
    columns.append(TimeElapsedColumn())
    progress = Progress(*columns, console=console, transient=transient, expand=expand)
    return progress


def create_tracker(*, console: Optional[Console] = None, transient: bool = False, **kwargs: Any) -> ProgressTracker:
    progress = create_progress(console=console, transient=transient, **kwargs)
    return ProgressTracker(progress=progress)


@contextlib.contextmanager
def live_group(renderables: Sequence[RenderableType], *, console: Optional[Console] = None, refresh_per_second: float = 4.0) -> Iterator[Live]:
    group = Group(*renderables)
    with Live(group, console=console or get_console(), refresh_per_second=refresh_per_second) as live:
        yield live


@contextlib.contextmanager
def status_spinner(message: str, *, console: Optional[Console] = None, spinner: str = "dots", speed: float = 1.0) -> Iterator[Status]:
    local_console = console or get_console()
    with local_console.status(message, spinner=spinner, speed=speed) as status:
        yield status


def build_table(
    columns: Sequence[Tuple[str, Dict[str, Any]]],
    rows: Iterable[Sequence[Any]],
    *,
    title: Optional[str] = None,
    show_footer: bool = False,
    caption: Optional[str] = None,
    row_styles: Optional[Sequence[str]] = None,
) -> Table:
    table = Table(title=title, box=box.SQUARE, caption=caption, highlight=True)
    for name, meta in columns:
        table.add_column(name, **meta)
    for row in rows:
        display_row = [format_cell(cell) for cell in row]
        table.add_row(*display_row, style=None)
    if row_styles:
        table.row_styles = list(row_styles)
    if show_footer and rows:
        table.show_footer = True
    return table


def format_cell(value: Any) -> str:
    if isinstance(value, (int, float)):
        return f"{value:,}" if isinstance(value, int) else f"{value:.2f}"
    if value is None:
        return "—"
    return str(value)


def format_delay_status(delay_seconds: Optional[float]) -> Text:
    if not delay_seconds or delay_seconds <= 0:
        return Text("—", style="muted")
    if delay_seconds < 10:
        style = "table.success"
    elif delay_seconds < 30:
        style = "table.header"
    else:
        style = "table.error"
    return Text(f"{delay_seconds:.1f}s", style=style)


def format_circuit_status(
    *,
    is_open: bool,
    open_until: Optional[datetime] = None,
    message: Optional[str] = None,
) -> Text:
    if not is_open:
        return Text("Healthy", style="table.success")

    parts = ["Open"]
    if message:
        parts.append(message)
    elif open_until:
        remaining = max(0, int(open_until.timestamp() - time.time()))
        parts.append(f"retry in {remaining}s")

    return Text(" - ".join(parts), style="table.error")


def calculate_progress_percentage(current: int, total: int) -> float:
    if not total or total <= 0:
        return 0.0
    return max(0.0, min(100.0, (current / total) * 100.0))


def format_phase_indicator(phase: str) -> Text:
    mapping = {
        "discovery": ("🔍", "accent"),
        "scraping": ("⚙️", "table.header"),
        "complete": ("✅", "table.success"),
    }
    icon, style = mapping.get(phase, ("•", "muted"))
    return Text(f"{icon} {phase.capitalize()}", style=style)


def create_inline_progress_bar(current: int, total: int, *, width: int = 20) -> Text:
    percentage = calculate_progress_percentage(current, total)
    bar_width = max(width, 1)
    filled = int(round((percentage / 100.0) * bar_width))
    filled = min(max(filled, 0), bar_width)

    bar = Text("[", style="muted")
    if filled:
        bar.append("█" * filled, style="table.header")
    if bar_width - filled:
        bar.append(" " * (bar_width - filled), style="muted")
    bar.append("]", style="muted")
    bar.append(f" {percentage:5.1f}%", style="table.neutral")
    return bar


def format_progress_status(
    phase: str,
    current: int,
    total: int,
    message: Optional[str] = None,
) -> Text:
    parts = Text.assemble(
        format_phase_indicator(phase),
        Text(" "),
        create_inline_progress_bar(current, total),
    )
    if message:
        parts.append_text(Text(f"  {message}", style="muted"))
    return parts


class ProgressThrottler:
    """Simple throttler to prevent excessive UI updates."""

    def __init__(self, *, min_interval: float = 0.1, max_skip: int = 5) -> None:
        self.min_interval = min_interval
        self.max_skip = max_skip
        self._last_update = 0.0
        self._skipped = 0

    def should_update(self) -> bool:
        now = time.perf_counter()
        if (now - self._last_update) >= self.min_interval or self._skipped >= self.max_skip:
            self._last_update = now
            self._skipped = 0
            return True
        self._skipped += 1
        return False

    def force(self) -> None:
        self._last_update = 0.0
        self._skipped = 0


def render_site_summary(
    summaries: Sequence[Dict[str, Any]],
    *,
    console: Optional[Console] = None,
    scheduler_stats: Optional[Dict[str, Any]] = None,
) -> None:
    local_console = console or get_console()
    columns = [
        ("Site", {"style": "table.header", "no_wrap": True}),
        ("Status", {"style": "table.header", "no_wrap": True}),
        ("Circuit", {"style": "table.header", "no_wrap": True}),
        ("Products", {"justify": "right"}),
        ("Variations", {"justify": "right"}),
        ("Duration", {"justify": "right"}),
        ("Delay", {"justify": "right"}),
        ("Backend", {"no_wrap": True}),
        ("Export", {"overflow": "fold"}),
    ]
    rows = []
    total_products = 0
    total_variations = 0
    total_duration = 0.0
    successes = 0
    for item in summaries:
        duration = float(item.get("duration_seconds") or 0.0)
        if item.get("skipped"):
            status_label = Text("Skipped", style="warning")
        else:
            status_style = "table.success" if item.get("success") else "table.error"
            status_label = Text("Success" if item.get("success") else "Failed", style=status_style)

        circuit_text = format_circuit_status(
            is_open=item.get("skipped", False),
            message=item.get("skip_reason"),
        )
        delay_text = format_delay_status(item.get("delay_applied"))
        export_candidates = []
        if item.get("export_path"):
            export_candidates.append(item["export_path"])
        if item.get("export_path_excel"):
            export_candidates.append(item["export_path_excel"])
        export_display = "\n".join(export_candidates) if export_candidates else "—"

        rows.append(
            [
                item.get("name") or item.get("domain") or "?",
                status_label,
                circuit_text,
                item.get("products_found", 0),
                item.get("variations_found", 0),
                f"{duration:.2f}s",
                delay_text,
                item.get("backend", "auto"),
                export_display,
            ]
        )
        total_products += int(item.get("products_found") or 0)
        total_variations += int(item.get("variations_found") or 0)
        total_duration += duration
        if item.get("success"):
            successes += 1
    table = build_table(columns, rows, title="Scraping Summary", row_styles=("", "dim"))
    footer = Text(
        f"Sites: {len(summaries)}  Success: {successes}  "
        f"Products: {total_products}  Variations: {total_variations}  Duration: {total_duration:.2f}s",
        style="table.footer",
    )
    renderables: List[RenderableType] = [table, Align.left(footer)]

    if scheduler_stats:
        stats_lines = [
            f"Tracked Domains: {scheduler_stats.get('tracked_domains', 0)}",
            f"Active Circuits: {scheduler_stats.get('active_circuits', 0)}",
            f"Average Success Rate: {scheduler_stats.get('average_success_rate', 1.0):.2f}",
            f"Inter-site Delay: {scheduler_stats.get('inter_site_delay', 0)}s",
            f"Min Domain Interval: {scheduler_stats.get('min_domain_interval', 0)}s",
        ]
        skipped = scheduler_stats.get("skipped_domains")
        if skipped is not None:
            stats_lines.append(f"Skipped (circuit): {skipped}")
        if "adaptive_concurrency_limit" in scheduler_stats:
            stats_lines.append(
                f"Concurrency Limit: {scheduler_stats['adaptive_concurrency_limit']}"
            )
        if "active_workers" in scheduler_stats:
            stats_lines.append(f"Active Workers: {scheduler_stats['active_workers']}")
        stats_panel = Panel(
            "\n".join(stats_lines),
            title="Scheduler Stats",
            border_style="accent",
        )
        renderables.append(stats_panel)
    local_console.print(Group(*renderables))


def render_error(message: str, *, details: Optional[str] = None, console: Optional[Console] = None) -> None:
    local_console = console or get_console()
    body: RenderableType = Text(message, style="error")
    if details:
        body = Group(Text(message, style="error"), Text(details, style="muted"))
    local_console.print(Panel(body, title="Error", border_style="error"))


def build_progress_layout(title: str, *, console: Optional[Console] = None) -> Tuple[ProgressTracker, Live]:
    local_console = console or get_console()
    tracker = create_tracker(console=local_console)
    live = Live(tracker.progress, console=local_console, refresh_per_second=10, transient=False)
    tracker.progress.console.print(Panel(title, style="accent"))
    live.start()
    return tracker, live


@contextlib.contextmanager
def managed_tracker(title: str, *, console: Optional[Console] = None) -> Iterator[ProgressTracker]:
    tracker, live = build_progress_layout(title, console=console)
    try:
        with tracker.progress:
            yield tracker
    finally:
        live.stop()


def combine_renderables(*renderables: RenderableType) -> RenderableType:
    return Group(*renderables)


def persist_snapshots(path: Path, snapshot: Dict[str, ProgressSnapshot]) -> None:
    payload = {
        key: {
            "description": snap.description,
            "completed": snap.completed,
            "total": snap.total,
            "timestamp": snap.timestamp,
        }
        for key, snap in snapshot.items()
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json_dumps(payload)
    path.write_text(data, encoding="utf-8")


def load_snapshots(path: Path) -> Dict[str, ProgressSnapshot]:
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8")
    payload = json_loads(raw)
    snapshots: Dict[str, ProgressSnapshot] = {}
    for key, value in payload.items():
        snapshots[key] = ProgressSnapshot(
            description=value.get("description", key),
            completed=value.get("completed", 0.0),
            total=value.get("total"),
            timestamp=value.get("timestamp", time.perf_counter()),
        )
    return snapshots


def json_dumps(data: Dict[str, Any]) -> str:
    import json

    return json.dumps(data, ensure_ascii=False, indent=2)


def json_loads(data: str) -> Dict[str, Any]:
    import json

    return json.loads(data)


__all__ = [
    "ProgressSnapshot",
    "ProgressTracker",
    "build_progress_layout",
    "build_table",
    "combine_renderables",
    "create_progress",
    "create_tracker",
    "format_cell",
    "format_circuit_status",
    "format_delay_status",
    "live_group",
    "load_snapshots",
    "managed_tracker",
    "persist_snapshots",
    "render_error",
    "render_site_summary",
    "status_spinner",
]
