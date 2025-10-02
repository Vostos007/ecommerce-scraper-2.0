#!/usr/bin/env python3
"""Generate import hotspot report using Python's importtime profiler."""

from __future__ import annotations

import argparse
import datetime as dt
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Tuple


def run_importtime(module: str) -> List[str]:
    """Execute the interpreter with ``-X importtime`` and capture stderr lines."""

    cmd = [sys.executable, "-X", "importtime", "-c", f"import {module}"]
    result = subprocess.run(cmd, capture_output=True, text=True)  # noqa: S603, S607
    if result.returncode != 0:
        raise RuntimeError(
            f"importtime run failed for module {module}: {result.stderr or result.stdout}"
        )
    return result.stderr.splitlines()


def parse_importtime(lines: List[str]) -> Tuple[List[dict], float]:
    """Parse ``-X importtime`` output into structured entries and total time (ms)."""

    rows: List[Tuple[str, int, int]] = []
    for line in lines:
        line = line.strip()
        if not line.startswith("import time:"):
            continue
        if "self [us]" in line:
            continue  # header

        parts = [segment.strip() for segment in line.split("|")]
        if len(parts) != 3:
            # Unexpected format — skip the line but keep going
            continue

        try:
            self_us = int(parts[0].split()[-1])
            cumulative_us = int(parts[1])
        except ValueError:
            continue

        module_name = parts[2]
        rows.append((module_name, self_us, cumulative_us))

    if not rows:
        return [], 0.0

    aggregated: Dict[str, Dict[str, float]] = {}
    for module_name, self_us, cumulative_us in rows:
        entry = aggregated.setdefault(
            module_name,
            {"module": module_name, "self_us": 0, "cumulative_us": 0},
        )
        entry["self_us"] += self_us
        entry["cumulative_us"] = max(entry["cumulative_us"], cumulative_us)

    entries: List[dict] = []
    for data in aggregated.values():
        entries.append(
            {
                "module": data["module"],
                "self_ms": round(data["self_us"] / 1000, 3),
                "cumulative_ms": round(data["cumulative_us"] / 1000, 3),
                "self_us": int(data["self_us"]),
                "cumulative_us": int(data["cumulative_us"]),
            }
        )

    total_ms = max(entry["cumulative_ms"] for entry in entries)
    return entries, total_ms


def render_markdown(entries: List[dict], total_ms: float, limit: int) -> str:
    """Render a markdown summary for the hottest imports."""

    header = ["# Import Hotspots", "", f"_Total cumulative import time: **{total_ms:.3f} ms**_"]
    header.append("")
    header.append("| Module | Self ms | Cumulative ms | Share |")
    header.append("| --- | ---: | ---: | ---: |")

    top_entries = sorted(entries, key=lambda item: item["cumulative_ms"], reverse=True)[:limit]
    rows = []
    for entry in top_entries:
        share = (entry["cumulative_ms"] / total_ms * 100) if total_ms else 0.0
        rows.append(
            f"| {entry['module']} | {entry['self_ms']:.3f} | {entry['cumulative_ms']:.3f} | {share:0.1f}% |"
        )

    body = header + rows
    body.append("")
    body.append("Generated on " + dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"))
    return "\n".join(body)


def main() -> None:
    parser = argparse.ArgumentParser(description="Profile import hotspots using importtime")
    parser.add_argument("--module", default="competitor_monitor.__main__")
    parser.add_argument("--output", required=True)
    parser.add_argument("--limit", type=int, default=40)
    args = parser.parse_args()

    lines = run_importtime(args.module)
    entries, total_ms = parse_importtime(lines)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    markdown = render_markdown(entries, total_ms, args.limit)
    output_path.write_text(markdown, encoding="utf-8")
    print(output_path)


if __name__ == "__main__":
    main()
