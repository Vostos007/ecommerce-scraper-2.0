#!/usr/bin/env python3
"""Parallel batch runner for ili-ili.com scraping."""

from __future__ import annotations

import argparse
import math
import subprocess
import sys
import time
from pathlib import Path

CACHE_PATH = Path("data/sites/ili-ili.com/cache/iliili_urls.txt")
SITE_SCRIPT = Path("scripts/sites/ili_ili_com.py")


def count_urls() -> int:
    if not CACHE_PATH.exists():
        raise SystemExit(f"Cache file not found: {CACHE_PATH}")
    with CACHE_PATH.open("r", encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())


def launch_worker(offset: int, batch_size: int) -> subprocess.Popen:
    cmd = [
        sys.executable,
        str(SITE_SCRIPT),
        "--skip-cache-refresh",
        "--batch-size",
        str(batch_size),
        "--batch-offset",
        str(offset),
        "--batch-max",
        "1",
    ]
    print(f"  [spawn] offset={offset} size={batch_size}")
    return subprocess.Popen(cmd)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run ili-ili.com batches in parallel")
    parser.add_argument("--batch-size", type=int, default=150)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--start-offset", type=int, default=0)
    parser.add_argument("--max-batches", type=int, default=0)
    parser.add_argument("--pause", type=float, default=5.0)
    args = parser.parse_args()

    total_urls = count_urls()
    total_batches = math.ceil(total_urls / max(args.batch_size, 1))
    start = max(args.start_offset, 0)
    end = total_batches if args.max_batches <= 0 else min(total_batches, start + args.max_batches)

    print(
        f"Total URLs: {total_urls}\n"
        f"Batch size: {args.batch_size}\n"
        f"Total batches: {total_batches}\n"
        f"Workers: {args.workers}\n"
        f"Running offsets {start}..{end - 1}"
    )

    current_offset = start
    while current_offset < end:
        procs = []
        for _ in range(args.workers):
            if current_offset >= end:
                break
            procs.append((current_offset, launch_worker(current_offset, args.batch_size)))
            current_offset += 1

        # wait for this wave
        failed = False
        for offset, proc in procs:
            code = proc.wait()
            if code != 0:
                print(f"[error] batch offset {offset} exited with code {code}")
                failed = True
        if failed:
            print("Stopping due to failure")
            return

        if args.pause and current_offset < end:
            time.sleep(args.pause)

    print("All batches completed")


if __name__ == "__main__":
    main()
