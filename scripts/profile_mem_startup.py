#!/usr/bin/env python3
"""Measure RSS memory consumption during module import and optional entry call."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import resource
import sys
import time
import tracemalloc
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]


def rss_mb() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    rss_kb = usage.ru_maxrss
    # macOS reports bytes, Linux reports kilobytes
    if sys.platform == "darwin":
        rss_kb /= 1024
    return round(rss_kb / 1024, 2)


def canonical_label(filename: str) -> str:
    path = Path(filename)
    try:
        rel = path.resolve().relative_to(REPO_ROOT)
        return rel.as_posix()
    except Exception:  # noqa: BLE001 - fall back to best-effort labels
        parts = path.parts
        if "site-packages" in parts:
            idx = parts.index("site-packages")
            return "/".join(parts[idx:idx + 2])
        return path.name


def aggregate_top_allocations(snapshot: tracemalloc.Snapshot, limit: int) -> Tuple[List[Dict[str, float]], int]:
    stats = snapshot.statistics("filename")
    size_by_label: Dict[str, int] = defaultdict(int)
    count_by_label: Dict[str, int] = defaultdict(int)

    for stat in stats:
        if not stat.traceback:
            continue
        label = canonical_label(stat.traceback[0].filename)
        size_by_label[label] += stat.size
        count_by_label[label] += stat.count

    ordered = sorted(size_by_label.items(), key=lambda item: item[1], reverse=True)
    top_allocations: List[Dict[str, float]] = []
    for label, size_bytes in ordered[:limit]:
        top_allocations.append(
            {
                "label": label,
                "size_bytes": size_bytes,
                "size_kb": round(size_bytes / 1024, 2),
                "size_mb": round(size_bytes / (1024 * 1024), 3),
                "allocations": count_by_label[label],
            }
        )

    total_bytes = sum(size_by_label.values())
    return top_allocations, total_bytes


def render_markdown(payload: Dict[str, object], limit: int) -> str:
    lines: List[str] = []
    lines.append("# Startup Memory Hotspots")
    lines.append("")
    lines.append(f"_Generated: {payload['generated_utc']}_")
    lines.append("")
    lines.append(f"- Module: `{payload['module']}`")
    lines.append(f"- RSS before import: {payload['rss_before_mb']} MB")
    lines.append(f"- RSS after import: {payload['rss_after_import_mb']} MB")
    lines.append(f"- RSS after entry: {payload['rss_after_entry_mb']} MB")
    lines.append(f"- Import time: {payload['import_time_s']} s")
    lines.append(f"- Traced allocations total: {payload['traced_total_mb']} MB")
    lines.append("")
    lines.append(f"## Top allocations (limit={limit})")
    lines.append("")
    lines.append("| # | Module/File | Size MB | Allocations | Share |")
    lines.append("| --- | --- | ---: | ---: | ---: |")

    top_allocations: List[Dict[str, object]] = payload.get("top_allocations", [])  # type: ignore[assignment]
    total_bytes = sum(entry["size_bytes"] for entry in top_allocations) or 1
    for idx, entry in enumerate(top_allocations, start=1):
        share = entry["size_bytes"] / total_bytes * 100
        lines.append(
            "| {idx} | {label} | {size:.3f} | {allocs} | {share:.1f}% |".format(
                idx=idx,
                label=entry["label"],
                size=entry["size_mb"],
                allocs=entry["allocations"],
                share=share,
            )
        )

    lines.append("")
    lines.append("Limit covers {:.1f}% of traced allocations.".format(total_bytes / (payload["traced_total_bytes"] or 1) * 100))
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Profile startup RSS usage")
    parser.add_argument("--module", default="competitor_monitor.__main__")
    parser.add_argument("--entry", default="main")
    parser.add_argument("--output")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    args = parser.parse_args()

    t0 = time.perf_counter()
    rss_before = rss_mb()

    tracemalloc.start()
    module = importlib.import_module(args.module)
    t1 = time.perf_counter()
    rss_after_import = rss_mb()

    call_duration = 0.0
    rss_after_entry = rss_after_import
    ok = True

    func = getattr(module, args.entry, None)
    if callable(func):
        try:
            start_call = time.perf_counter()
            # Avoid executing heavy logic: only ensure callable exists.
            func  # type: ignore[pointless-statement]
            call_duration = time.perf_counter() - start_call
            rss_after_entry = rss_mb()
        except Exception:
            ok = False

    snapshot = tracemalloc.take_snapshot()
    tracemalloc.stop()
    top_allocations, total_bytes = aggregate_top_allocations(snapshot, args.limit)

    payload = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "module": args.module,
        "entry": args.entry,
        "import_time_s": round(t1 - t0, 4),
        "entry_check_time_s": round(call_duration, 4),
        "rss_before_mb": rss_before,
        "rss_after_import_mb": rss_after_import,
        "rss_after_entry_mb": rss_after_entry,
        "ok": ok,
        "top_allocations": top_allocations,
        "traced_total_bytes": total_bytes,
        "traced_total_mb": round(total_bytes / (1024 * 1024), 3),
    }

    if args.output:
        os.makedirs(os.path.dirname(args.output), exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)

    if args.format == "json":
        print(json.dumps(payload, indent=2))
    else:
        print(render_markdown(payload, args.limit))


if __name__ == "__main__":
    main()
