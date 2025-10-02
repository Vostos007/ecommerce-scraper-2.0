#!/usr/bin/env python3
"""Convenience wrapper around pytest with sensible defaults."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parents[1]


def build_pytest_cmd(args: argparse.Namespace) -> List[str]:
    cmd = [sys.executable, "-m", "pytest", "-m", args.mark]

    if args.verbose:
        cmd.append("-vv")

    if args.cov:
        cmd.extend([
            "--cov=.",
            "--cov-report=term-missing",
        ])
        if args.cov_html:
            cmd.append("--cov-report=html")

    if args.additional:
        cmd.extend(args.additional)

    return cmd


def run(cmd: List[str], log_file: str | None = None) -> int:
    env = os.environ.copy()
    env.setdefault("PYTHONPATH", str(ROOT))
    print("[run-tests] executing:", " ".join(cmd))
    if not log_file:
        return subprocess.call(cmd, cwd=str(ROOT), env=env)

    log_path = Path(log_file)
    if not log_path.is_absolute():
        log_path = ROOT / log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)

    with log_path.open("w", encoding="utf-8") as handle:
        process = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            print(line, end="")
            handle.write(line)
        return process.wait()


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run pytest with project defaults")
    parser.add_argument(
        "--mark",
        default="not live",
        help="Pytest expression for selecting tests (default: 'not live')",
    )
    parser.add_argument("--no-cov", action="store_false", dest="cov", help="Disable coverage")
    parser.add_argument(
        "--cov-html",
        action="store_true",
        help="Generate HTML coverage (requires --cov)",
    )
    parser.add_argument("--verbose", action="store_true", help="Run tests verbosely")
    parser.add_argument(
        "--log-file",
        help="Path to tee pytest output into a log file",
    )
    parser.add_argument(
        "additional",
        nargs=argparse.REMAINDER,
        help="Additional arguments passed directly to pytest",
    )
    parser.set_defaults(cov=True)
    return parser.parse_args(argv)


def main(argv: List[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    cmd = build_pytest_cmd(args)
    return run(cmd, log_file=args.log_file)


if __name__ == "__main__":
    raise SystemExit(main())
