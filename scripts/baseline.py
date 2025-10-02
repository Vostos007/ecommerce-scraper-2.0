#!/usr/bin/env python3
"""Baseline metrics collector for CompetitorMonitor.

Collects code size statistics, TODO/FIXME counts, startup timing, log errors
and writes the output both as JSON and as a rendered Markdown summary.

The script intentionally avoids third-party dependencies so that it can run
in any developer environment and in CI.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

CODE_EXTENSIONS = (".py", ".pyi", ".ts", ".tsx", ".js", ".jsx")
EXCLUDE_DIRS = {"node_modules", ".next", "venv", ".venv", ".venv_test", "__pycache__", ".git"}
TODO_PATTERNS = ("TODO", "FIXME")


@dataclass
class CodeStats:
    total_files: int
    total_lines: int
    total_bytes: int
    top_paths: List[Dict[str, int]]


@dataclass
class TodoStats:
    counts: Dict[str, int]


@dataclass
class StartupStats:
    ok: bool
    seconds: float


@dataclass
class LogStats:
    http_500: int
    api_errors: int
    tracebacks: int


@dataclass
class BaselineReport:
    generated_utc: str
    code: CodeStats
    todo_fixme: TodoStats
    startup: StartupStats
    logs: LogStats
    memory: Optional["MemoryStats"]


@dataclass
class MemoryStats:
    import_time_s: float
    rss_before_mb: float
    rss_after_import_mb: float
    rss_after_entry_mb: float


def iter_code_files(root: Path) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for filename in filenames:
            path = Path(dirpath) / filename
            if path.suffix in CODE_EXTENSIONS:
                yield path


def gather_code_stats(root: Path) -> CodeStats:
    total_files = 0
    total_lines = 0
    total_bytes = 0
    per_dir: Dict[Path, Dict[str, int]] = {}

    for file_path in iter_code_files(root):
        try:
            file_bytes = file_path.stat().st_size
            with file_path.open("rb") as fh:
                file_lines = sum(1 for _ in fh)
        except OSError:
            continue

        total_files += 1
        total_lines += file_lines
        total_bytes += file_bytes

        rel_dir = file_path.parent.relative_to(root)
        entry = per_dir.setdefault(rel_dir, {"files": 0, "lines": 0, "bytes": 0})
        entry["files"] += 1
        entry["lines"] += file_lines
        entry["bytes"] += file_bytes

    top_paths = [
        {"path": str(path), **stats}
        for path, stats in sorted(per_dir.items(), key=lambda item: item[1]["bytes"], reverse=True)[:30]
    ]

    return CodeStats(
        total_files=total_files,
        total_lines=total_lines,
        total_bytes=total_bytes,
        top_paths=top_paths,
    )


def gather_todo_stats(root: Path) -> TodoStats:
    counts = {pattern: 0 for pattern in TODO_PATTERNS}
    regexes = {pattern: re.compile(pattern) for pattern in TODO_PATTERNS}

    for file_path in iter_code_files(root):
        try:
            with file_path.open("r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    for pattern, regex in regexes.items():
                        if regex.search(line):
                            counts[pattern] += 1
        except OSError:
            continue

    return TodoStats(counts=counts)


def measure_startup(module: str, entry: str) -> StartupStats:
    code = (
        "import importlib, time\n"
        "start = time.perf_counter()\n"
        "module = importlib.import_module(\"%s\")\n" % module
        + "fn = getattr(module, \"%s\", None)\n" % entry
        + "ok = True\n"
        + "if callable(fn):\n"
        + "    try:\n"
        + "        fn\n"
        + "    except Exception:\n"
        + "        ok = False\n"
        + "end = time.perf_counter()\n"
        + "print(ok)\n"
        + "print(end - start)\n"
    )

    try:
        output = subprocess.check_output([sys.executable, "-c", code], text=True)
        ok_str, seconds_str = output.strip().splitlines()[-2:]
        ok = ok_str.strip().lower() == "true"
        seconds = float(seconds_str)
    except (subprocess.CalledProcessError, ValueError):
        ok = False
        seconds = float("nan")

    return StartupStats(ok=ok, seconds=round(seconds, 4) if seconds == seconds else float("nan"))


def scan_logs(log_dir: Path) -> LogStats:
    http_500 = 0
    api_errors = 0
    tracebacks = 0

    http_regex = re.compile(
        r"HTTP/\d\.\d\"\s*500\b|status[_\s:=\"]+500\b|statusCode\s*=\s*500\b",
        re.IGNORECASE,
    )
    api_regex = re.compile(r"\bapi\b.*\berr", re.IGNORECASE)
    traceback_regex = re.compile(r"Traceback \(most recent call last\)")

    if not log_dir.exists():
        return LogStats(http_500=0, api_errors=0, tracebacks=0)

    for path in log_dir.rglob("*"):
        if not path.is_file():
            continue
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if traceback_regex.search(line):
                        tracebacks += 1
                    if http_regex.search(line):
                        http_500 += 1
                    if api_regex.search(line):
                        api_errors += 1
        except OSError:
            continue

    return LogStats(http_500=http_500, api_errors=api_errors, tracebacks=tracebacks)


def load_memory_stats(path: Path) -> Optional[MemoryStats]:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return None

    required_keys = {
        "import_time_s",
        "rss_before_mb",
        "rss_after_import_mb",
        "rss_after_entry_mb",
    }
    if not required_keys.issubset(payload.keys()):
        return None

    return MemoryStats(
        import_time_s=float(payload.get("import_time_s", 0.0)),
        rss_before_mb=float(payload.get("rss_before_mb", 0.0)),
        rss_after_import_mb=float(payload.get("rss_after_import_mb", 0.0)),
        rss_after_entry_mb=float(payload.get("rss_after_entry_mb", 0.0)),
    )


def format_number(value: Optional[float]) -> str:
    if value is None:
        return "не указано"
    if isinstance(value, float) and (value != value):  # NaN guard
        return "NaN"
    return f"{value:.3f}" if isinstance(value, float) else str(value)


def write_markdown(path: Path, report: BaselineReport, module: str, entry: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Baseline метрики\n\n",
        f"_Сгенерировано: {report.generated_utc}_\n\n",
        "## Сводка\n\n",
        f"- Холодный старт `{module}.{entry}`: **{report.startup.seconds} s** (ok={report.startup.ok})\n",
        f"- Кодовых файлов: **{report.code.total_files}**, строк: **{report.code.total_lines}**, размер: **{report.code.total_bytes} байт**\n",
        f"- TODO: {report.todo_fixme.counts.get('TODO', 0)}, FIXME: {report.todo_fixme.counts.get('FIXME', 0)}\n",
        f"- Логи: http_500={report.logs.http_500}, api_errors={report.logs.api_errors}, tracebacks={report.logs.tracebacks}\n\n",
        "## Топ директорий по размеру\n\n",
        "| Путь | Файлов | Строк | Байтов |\n|---|---:|---:|---:|\n",
    ]
    for entry in report.code.top_paths:
        lines.append(
            f"| {entry['path']} | {entry['files']} | {entry['lines']} | {entry['bytes']} |\n"
        )

    lines.append("\n## KPI vs Target\n\n")
    lines.append("| Метрика | Факт | Цель | Gap | Статус |\n|---|---:|---:|---:|:---:|\n")

    def append_row(metric: str, actual: Optional[float], target: Optional[float]) -> None:
        if actual is None or target is None:
            lines.append(f"| {metric} | не указано | {format_number(target)} | не указано | ⚠️ |\n")
            return
        gap = actual - target
        status = "✅" if actual <= target else "⚠️"
        lines.append(
            f"| {metric} | {format_number(actual)} | {format_number(target)} | {gap:+.3f} | {status} |\n"
        )

    append_row("Холодный старт, s", report.startup.seconds, 0.45)

    if report.memory is not None:
        append_row("RSS после импорта, MB", report.memory.rss_after_import_mb, 90.0)
    else:
        lines.append("| RSS после импорта, MB | не указано | 90.000 | не указано | ⚠️ |\n")

    append_row("HTTP 500 за период", float(report.logs.http_500), 0.0)

    lines.append("\n> Финальная цель холодного старта — 0.150 s (зафиксировано в stabilization-playbook).\n\n")

    lines.append("\n## Команды\n\n")
    lines.append("```bash\n")
    lines.append("make baseline\n")
    lines.append("```\n")

    path.write_text("".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect baseline metrics")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--logs", type=Path, default=Path("logs"))
    parser.add_argument("--module", type=str, default="competitor_monitor.__main__")
    parser.add_argument("--entry", type=str, default="main")
    parser.add_argument("--memory-json", type=Path, default=None)
    parser.add_argument("--out-json", type=Path, required=True)
    parser.add_argument("--out-md", type=Path, required=True)
    args = parser.parse_args()

    code_stats = gather_code_stats(args.root)
    todo_stats = gather_todo_stats(args.root)
    startup_stats = measure_startup(args.module, args.entry)
    log_stats = scan_logs(args.logs)
    memory_stats = load_memory_stats(args.memory_json) if args.memory_json else None

    report = BaselineReport(
        generated_utc=datetime.now(timezone.utc).isoformat(),
        code=code_stats,
        todo_fixme=todo_stats,
        startup=startup_stats,
        logs=log_stats,
        memory=memory_stats,
    )

    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    with args.out_json.open("w", encoding="utf-8") as f:
        json.dump(
            asdict(report),
            f,
            indent=2,
            ensure_ascii=False,
        )

    write_markdown(args.out_md, report, args.module, args.entry)


if __name__ == "__main__":
    main()
