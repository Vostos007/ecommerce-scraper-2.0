#!/usr/bin/env python3
"""Apply project formatting using black and isort."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parents[1]
PYTHON_BIN = sys.executable


def run_tool(tool: List[str]) -> int:
    print("[format-code]", " ".join(tool))
    return subprocess.call(tool, cwd=str(ROOT))


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Format the repository using black + isort")
    parser.add_argument("--check", action="store_true", help="Run tools in check-only mode")
    parser.add_argument(
        "paths",
        nargs="*",
        default=["."],
        help="Optional subset of paths to format (default: project root)",
    )
    return parser.parse_args(argv)


def main(argv: List[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])

    black_cmd = [PYTHON_BIN, "-m", "black"]
    isort_cmd = [PYTHON_BIN, "-m", "isort"]
    if args.check:
        isort_cmd.extend(["--check-only", "--diff"])
        black_cmd.extend(["--check", "--diff"])

    isort_cmd.extend(args.paths)
    black_cmd.extend(args.paths)

    code = run_tool(isort_cmd)
    if code != 0:
        return code
    return run_tool(black_cmd)


if __name__ == "__main__":
    raise SystemExit(main())
