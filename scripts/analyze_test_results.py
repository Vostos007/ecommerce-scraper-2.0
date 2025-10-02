#!/usr/bin/env python3
"""Simple analyzer for pytest result logs."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Dict

SUMMARY_KEYS = ["passed", "failed", "skipped", "xfailed", "xpassed", "error"]
SUMMARY_PATTERN = re.compile(r"(\d+)\s+(passed|failed|skipped|xfailed|xpassed|errors?)")


def parse_log(log_path: Path) -> Dict[str, int]:
    summary = {key: 0 for key in SUMMARY_KEYS}
    if not log_path.exists():
        return summary

    for line in log_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        matches = SUMMARY_PATTERN.findall(line)
        for count, label in matches:
            key = label.rstrip("s") if label.startswith("error") else label
            if key not in summary:
                continue
            summary[key] += int(count)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analyze pytest log output")
    parser.add_argument("log", help="Path to a pytest log file")
    parser.add_argument("--json", action="store_true", help="Emit JSON summary")
    args = parser.parse_args(argv)

    summary = parse_log(Path(args.log))
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        for key, value in summary.items():
            print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
