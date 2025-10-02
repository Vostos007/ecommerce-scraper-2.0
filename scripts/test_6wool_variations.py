#!/usr/bin/env python3
"""Helper script to run 6wool.ru variation parser tests."""
from __future__ import annotations

import argparse
import subprocess
import sys
from typing import List

TEST_MODULES = {
    "unit": ["tests/test_variation_parser_6wool.py"],
    "integration": ["tests/test_variation_parser_6wool_integration.py"],
}


def run_pytest(targets: List[str], args: argparse.Namespace) -> int:
    command: List[str] = ["pytest"]

    marker_parts: List[str] = ["variation_parser", "bitrix", "sixwool"]
    if args.mode == "unit":
        marker_parts.append("not live")
    elif args.mode == "integration":
        marker_parts.append("live")

    if marker_parts:
        command.extend(["-m", " and ".join(marker_parts)])

    command.extend(targets)

    if args.live:
        command.append("--live")
    if args.skip_slow:
        command.append("--skip-slow")
    if args.cache_test_timeout is not None:
        command.extend(["--cache-test-timeout", str(args.cache_test_timeout)])

    if args.pytest_args:
        command.extend(args.pytest_args)

    process = subprocess.run(command, check=False)
    return process.returncode


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the 6wool.ru variation parser test suites",
    )
    parser.add_argument(
        "--mode",
        choices=["unit", "integration", "all"],
        default="all",
        help="Select which suite to execute",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Enable live HTTP integration tests (passes --live to pytest)",
    )
    parser.add_argument(
        "--skip-slow",
        action="store_true",
        help="Skip tests marked as slow",
    )
    parser.add_argument(
        "--cache-test-timeout",
        type=float,
        default=None,
        help="Override cache test timeout value passed to pytest",
    )
    parser.add_argument(
        "pytest_args",
        nargs=argparse.REMAINDER,
        help="Additional arguments forwarded to pytest",
    )

    args = parser.parse_args()

    targets: List[str] = []
    if args.mode in ("unit", "all"):
        targets.extend(TEST_MODULES["unit"])
    if args.mode in ("integration", "all"):
        targets.extend(TEST_MODULES["integration"])

    exit_code = run_pytest(targets, args)

    if exit_code != 0:
        print("Test execution failed", file=sys.stderr)
    else:
        print("6wool.ru variation parser tests completed successfully")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
