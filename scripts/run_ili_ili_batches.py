#!/usr/bin/env python3
"""Batch runner for ili-ili.com scraping.

Splits cached URLs into batches and invokes site_runner sequentially,
allowing unattended full-site scraping with controlled pauses.
"""

from __future__ import annotations

import argparse
import math
import subprocess
import sys
import time
from pathlib import Path

CACHE_PATH = Path("data/sites/ili-ili.com/cache/iliili_urls.txt")
SITE_SCRIPT = Path("scripts/sites/ili_ili_com.py")
DEFAULT_BATCH_SIZE = 100
DEFAULT_PAUSE = 5.0


def count_cached_urls() -> int:
    if not CACHE_PATH.exists():
        raise SystemExit(f"Cache file not found: {CACHE_PATH}")
    with CACHE_PATH.open("r", encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())


def run_batch(offset: int, batch_size: int, pause_seconds: float) -> bool:
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
    print(f"\n[run] offset={offset} size={batch_size}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"[error] batch offset {offset} exited with code {result.returncode}")
        return False
    if pause_seconds > 0:
        time.sleep(pause_seconds)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Run ili-ili.com batches sequentially")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--start-offset", type=int, default=0, help="Batch offset to start from")
    parser.add_argument("--max-batches", type=int, default=0, help="Limit number of batches (0=all)")
    parser.add_argument("--pause", type=float, default=DEFAULT_PAUSE, help="Pause between batches in seconds")
    args = parser.parse_args()

    total_urls = count_cached_urls()
    batch_count = math.ceil(total_urls / max(args.batch_size, 1))

    start = max(args.start_offset, 0)
    end = batch_count if args.max_batches <= 0 else min(batch_count, start + args.max_batches)

    print(
        f"Total URLs: {total_urls}\n"
        f"Batch size: {args.batch_size}\n"
        f"Total batches: {batch_count}\n"
        f"Running offsets {start}..{end - 1}"
    )

    for offset in range(start, end):
        ok = run_batch(offset, args.batch_size, args.pause)
        if not ok:
            print("Stopping due to batch failure")
            return

    print("All requested batches completed")


if __name__ == "__main__":
    main()
