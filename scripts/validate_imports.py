#!/usr/bin/env python3
"""Static validation of imports, syntax, and dependency alignment."""
from __future__ import annotations

import argparse
import ast
import importlib.machinery
import importlib.util
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_REQUIREMENT_FILES = ["requirements.txt", "requirements-dev.txt"]

_LOCAL_NAMESPACES: Optional[Set[str]] = None
_MODULE_SPEC_CACHE: Dict[str, Optional[importlib.machinery.ModuleSpec]] = {}
_STD_LIB_NAMES: Set[str] = set(getattr(sys, "stdlib_module_names", set()))
_STD_LIB_NAMES.update(sys.builtin_module_names)
try:  # Python 3.10+
    from importlib.metadata import packages_distributions as _packages_distributions
except ImportError:  # pragma: no cover
    _packages_distributions = None

_DISTRIBUTION_LOOKUP = (
    _packages_distributions() if _packages_distributions is not None else {}
)


def find_python_files(paths: Iterable[str]) -> List[Path]:
    results: List[Path] = []
    for entry in paths:
        path = (ROOT / entry).resolve()
        if path.is_file() and path.suffix == ".py":
            results.append(path)
        elif path.is_dir():
            for py_file in path.rglob("*.py"):
                if "__pycache__" in py_file.parts:
                    continue
                results.append(py_file)
    return results


def validate_syntax(file_path: Path) -> Tuple[bool, str | None]:
    try:
        ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
    except SyntaxError as exc:
        return False, f"SyntaxError: {exc}"
    except UnicodeDecodeError as exc:
        return False, f"EncodingError: {exc}"
    return True, None


def validate_imports(file_path: Path, *, strict: bool) -> List[str]:
    issues: List[str] = []
    tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module_name = alias.name
                if not strict and not _should_check_module(module_name):
                    continue
                if not _module_exists(module_name):
                    issues.append(f"Missing module '{module_name}' referenced in {file_path}")
        elif isinstance(node, ast.ImportFrom):
            if node.level and not strict:
                continue
            module = node.module or ""
            if node.level:
                # Relative import – rely on Python's own resolution, skip in non-strict mode
                module_name = module
            else:
                module_name = module
            if not module_name:
                continue
            if not strict and not _should_check_module(module_name):
                continue
            if not _module_exists(module_name):
                issues.append(f"Missing module '{module_name}' referenced in {file_path}")
    return issues


def _module_exists(name: str) -> bool:
    if name in _MODULE_SPEC_CACHE:
        return _MODULE_SPEC_CACHE[name] is not None
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError):
        spec = None
    _MODULE_SPEC_CACHE[name] = spec
    return spec is not None


def _should_check_module(module: str) -> bool:
    top = module.split(".", 1)[0]
    return top in _get_local_namespaces()


def _get_local_namespaces() -> Set[str]:
    global _LOCAL_NAMESPACES
    if _LOCAL_NAMESPACES is not None:
        return _LOCAL_NAMESPACES

    namespaces: Set[str] = set()
    for entry in ROOT.iterdir():
        name = entry.name
        if name.startswith(".") or name.startswith("_"):
            continue
        if entry.is_dir():
            if (entry / "__init__.py").exists():
                namespaces.add(name)
        elif entry.suffix == ".py":
            namespaces.add(entry.stem)
    namespaces.update({"scripts", "tests"})
    _LOCAL_NAMESPACES = namespaces
    return namespaces


def collect_top_level_modules(files: Iterable[Path]) -> Set[str]:
    modules: Set[str] = set()
    for file_path in files:
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    modules.add(alias.name.split(".", 1)[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    continue
                if node.module:
                    modules.add(node.module.split(".", 1)[0])
    return modules


def normalize_package_name(name: str) -> str:
    return name.lower().replace("-", "_")


def is_standard_library(module: str) -> bool:
    top = module.split(".", 1)[0]
    if top in _STD_LIB_NAMES:
        return True
    try:
        spec = importlib.util.find_spec(top)
    except (ImportError, ValueError):
        return False
    if not spec or not spec.origin:
        return False
    origin = spec.origin
    if origin == "built-in":
        return True
    origin_lower = origin.lower()
    if "site-packages" in origin_lower or "dist-packages" in origin_lower:
        return False
    return "python" in origin_lower


def load_requirement_packages(paths: List[str]) -> Tuple[Dict[str, str], List[str], bool]:
    packages: Dict[str, str] = {}
    errors: List[str] = []
    any_files = False
    for entry in paths:
        path = (ROOT / entry).resolve()
        if not path.exists():
            continue
        any_files = True
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("-"):
                continue
            try:
                requirement = Requirement(stripped)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{path}:{lineno} -> {stripped} ({exc})")
                continue
            normalized = normalize_package_name(requirement.name)
            packages.setdefault(normalized, requirement.name)
    return packages, errors, any_files


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate imports and syntax")
    parser.add_argument(
        "paths",
        nargs="*",
        default=["."],
        help="Paths to validate (default: project root)",
    )
    parser.add_argument(
        "--skip-imports",
        action="store_true",
        help="Only validate syntax without checking imports",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Validate all imports, including third-party modules",
    )
    parser.add_argument(
        "--requirements",
        nargs="*",
        default=DEFAULT_REQUIREMENT_FILES,
        help="Requirement files used for dependency cross-checks",
    )
    return parser.parse_args(argv)


def main(argv: List[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    files = find_python_files(args.paths)
    if not files:
        print("[validate-imports] No Python files found.")
        return 0

    status = 0
    for file_path in files:
        ok, message = validate_syntax(file_path)
        if not ok:
            print(f"[syntax-error] {file_path}: {message}")
            status = 1
            continue
        if args.skip_imports:
            continue
        for issue in validate_imports(file_path, strict=args.strict):
            print(f"[import-error] {issue}")
            status = 1

    if not args.skip_imports:
        modules = collect_top_level_modules(files)
        requirement_map, requirement_errors, requirements_checked = load_requirement_packages(args.requirements)
        for err in requirement_errors:
            print(f"[import-error] Requirement parse error: {err}")
            status = 1

        if requirements_checked:
            used_requirements: Dict[str, str] = {}
            missing_requirements: Dict[str, str] = {}
            for module in sorted(modules):
                normalized_module = normalize_package_name(module)
                if module in _get_local_namespaces():
                    continue
                if is_standard_library(module):
                    continue
                matched_name: Optional[str] = None
                if normalized_module in requirement_map:
                    matched_name = normalized_module
                else:
                    distributions = {
                        normalize_package_name(dist)
                        for dist in _DISTRIBUTION_LOOKUP.get(module, [])
                    }
                    intersect = distributions & requirement_map.keys()
                    if intersect:
                        matched_name = next(iter(intersect))

                if matched_name:
                    used_requirements[matched_name] = requirement_map[matched_name]
                else:
                    missing_requirements[module] = normalized_module

            if missing_requirements:
                status = 1
                for module, normalized in missing_requirements.items():
                    print(
                        f"[import-error] Module '{module}' is imported but no matching package "
                        f"found in requirements (expected something like '{normalized}')"
                    )

            unused_requirements = sorted(
                name for name in requirement_map if name not in used_requirements
            )
            if unused_requirements:
                for name in unused_requirements:
                    print(
                        f"[import-warning] Requirement '{requirement_map[name]}' not referenced in code"
                    )

    if status == 0:
        print(f"[validate-imports] validated {len(files)} file(s) successfully")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
