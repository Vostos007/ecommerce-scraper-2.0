#!/usr/bin/env python3
"""High-level quality assurance pipeline."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
STEP_ORDER = ("config", "imports", "format", "tests")


def run(cmd: List[str]) -> int:
    print("[quality-check]", " ".join(cmd))
    return subprocess.call(cmd, cwd=str(ROOT))


def build_tasks(args: argparse.Namespace) -> "OrderedDict[str, List[str]]":
    tasks: "OrderedDict[str, List[str]]" = OrderedDict()

    tasks["config"] = [PYTHON, "scripts/validate_config.py", "--quiet"]

    imports_cmd = [PYTHON, "scripts/validate_imports.py"]
    if not args.check_imports:
        imports_cmd.append("--skip-imports")
    if args.imports_strict:
        imports_cmd.append("--strict")
    tasks["imports"] = imports_cmd

    format_cmd = [PYTHON, "scripts/format_code.py"]
    if not args.apply_format:
        format_cmd.append("--check")
    tasks["format"] = format_cmd

    tests_cmd = [PYTHON, "scripts/run_tests.py"]
    if args.log_file:
        tests_cmd.extend(["--log-file", args.log_file])
    tasks["tests"] = tests_cmd

    return tasks


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Execute quality checks")
    parser.add_argument(
        "--skip",
        nargs="*",
        default=[],
        help="Names of steps to skip (config, imports, format, tests)",
    )
    parser.add_argument(
        "--apply-format",
        action="store_true",
        help="Run formatters in apply mode (instead of --check)",
    )
    parser.add_argument(
        "--check-imports",
        action="store_true",
        help="Validate imports without skipping third-party modules",
    )
    parser.add_argument(
        "--imports-strict",
        action="store_true",
        help="Enable strict mode when validating imports (equivalent to --strict)",
    )
    parser.add_argument(
        "--log-file",
        help="Path to store pytest output when running tests",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Run all steps even if some fail",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a JSON summary of step exit codes",
    )
    parser.add_argument(
        "pytest-args",
        nargs=argparse.REMAINDER,
        help="Arguments forwarded to pytest through run_tests.py",
    )
    return parser.parse_args(argv)


def main(argv: List[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    tasks = build_tasks(args)

    results: List[Dict[str, Optional[int]]] = []
    final_status = 0

    for name in STEP_ORDER:
        command = tasks.get(name)
        if command is None:
            continue

        if name in args.skip:
            print(f"[quality-check] skipping step: {name}")
            results.append({"name": name, "skipped": True, "exit_code": None})
            continue

        cmd = command
        if name == "tests" and args.pytest_args:
            cmd = cmd + list(args.pytest_args)

        exit_code = run(cmd)
        results.append({"name": name, "skipped": False, "exit_code": exit_code})
        if exit_code != 0:
            if final_status == 0:
                final_status = exit_code
            print(f"[quality-check] step '{name}' failed with exit code {exit_code}")
        if exit_code != 0 and not args.continue_on_error:
            break

    if args.json:
        print(
            json.dumps(
                {
                    "status": final_status,
                    "steps": results,
                },
                indent=2,
            )
        )

    return final_status


if __name__ == "__main__":
    raise SystemExit(main())
