#!/usr/bin/env python3
"""Validate configuration files (JSON, TOML, requirements)."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List

from packaging.requirements import Requirement

try:  # Python 3.11+
    import tomllib as toml_loader
except ModuleNotFoundError:  # pragma: no cover
    import tomli as toml_loader  # type: ignore

ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILES = [ROOT / "config" / "settings.json", ROOT / "config" / "sites.json"]
TOML_FILES = [ROOT / "pyproject.toml"]
REQUIREMENT_FILES = [ROOT / "requirements.txt", ROOT / "requirements-dev.txt"]
PROXY_FILE = ROOT / "config" / "proxies_https.txt"


PROXY_RE = re.compile(
    r"^(?:(https?|socks5)://)?(?:[^:@\s]+(?::[^:@\s]+)?@)?[^:\s]+:\d+$"
)


def validate_json(path: Path) -> List[str]:
    errors: List[str] = []
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        errors.append(f"JSON error in {path}: {exc}")
    return errors


def validate_toml(path: Path) -> List[str]:
    errors: List[str] = []
    try:
        toml_loader.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"TOML error in {path}: {exc}")
    return errors


def validate_requirements(path: Path) -> List[str]:
    errors: List[str] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("-") or stripped.startswith("--"):
            continue
        try:
            Requirement(stripped)
        except Exception as exc:  # noqa: BLE001
            errors.append(
                f"Invalid requirement in {path}:{lineno} -> {stripped} ({exc})"
            )
    return errors


def validate_proxy_file(path: Path) -> List[str]:
    if not path.exists():
        return []
    errors: List[str] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not PROXY_RE.match(stripped):
            errors.append(f"Invalid proxy entry in {path}:{lineno} -> {stripped}")
    return errors


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate configuration files")
    parser.add_argument("--json-only", action="store_true", help="Only validate JSON files")
    parser.add_argument("--quiet", action="store_true", help="Only output errors")
    return parser.parse_args(argv)


def main(argv: List[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    errors: List[str] = []

    for json_file in CONFIG_FILES:
        if json_file.exists():
            errors.extend(validate_json(json_file))

    if not args.json_only:
        for toml_file in TOML_FILES:
            if toml_file.exists():
                errors.extend(validate_toml(toml_file))
        for req_file in REQUIREMENT_FILES:
            if req_file.exists():
                errors.extend(validate_requirements(req_file))
        errors.extend(validate_proxy_file(PROXY_FILE))

    if errors:
        for err in errors:
            print(err)
        return 1
    if not args.quiet:
        print("[validate-config] all configuration files validated successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
