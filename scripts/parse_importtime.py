#!/usr/bin/env python3
"""Convert `python -X importtime` output into CSV sorted by cumulative time."""

from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path
from typing import Iterable, Tuple

PATTERN = re.compile(
    r"import\s+time:\s+self\s+\[us\]\s*=\s*(\d+),\s*cumulative\s+\[us\]\s*=\s*(\d+)\s+module\s+(.*)"
)
ALT_PATTERN = re.compile(r"import\s+time:\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(.*)")


def parse_lines(lines: Iterable[str]) -> Iterable[Tuple[str, float, float]]:
    for line in lines:
        if "import time:" not in line:
            continue
        if "import time: self" in line:
            match = PATTERN.search(line)
            if not match:
                continue
            self_us, cum_us, module = match.groups()
        else:
            if "import time: self [us] | cumulative" in line:
                continue  # header line in newer Python versions
            match = ALT_PATTERN.search(line)
            if not match:
                continue
            self_us, cum_us, module = match.groups()
        yield module.strip(), int(cum_us) / 1e6, int(self_us) / 1e6


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse importtime output")
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args()

    with args.input.open("r", encoding="utf-8", errors="ignore") as fh:
        rows = list(parse_lines(fh))

    rows.sort(key=lambda row: row[1], reverse=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["module", "cum_seconds", "self_seconds"])
        for module, cum, self_time in rows[: args.limit]:
            writer.writerow([module, f"{cum:.6f}", f"{self_time:.6f}"])


if __name__ == "__main__":
    main()
