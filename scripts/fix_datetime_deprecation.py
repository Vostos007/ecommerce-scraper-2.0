#!/usr/bin/env python3
"""Replace deprecated ``datetime.utcnow()`` usage across the repository."""

from __future__ import annotations

import argparse
import ast
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Set, Tuple

PYTHON_EXTENSIONS = {".py"}
DEFAULT_EXCLUDE_DIRS = {
    ".git",
    "__pycache__",
    "venv",
    ".venv",
    "node_modules",
    "build",
    "dist",
}
@dataclass
class FileUpdate:
    path: Path
    replacements: int
    backup_path: Optional[Path]
    import_added: bool


def iter_python_files(root: Path, *, include: Optional[Sequence[str]] = None) -> Iterable[Path]:
    if include:
        for item in include:
            candidate = (root / item).resolve()
            if candidate.is_dir():
                yield from iter_python_files(candidate)
            elif candidate.is_file() and candidate.suffix in PYTHON_EXTENSIONS:
                yield candidate
        return

    for path in root.rglob("*.py"):
        if any(part in DEFAULT_EXCLUDE_DIRS for part in path.parts):
            continue
        yield path


class DatetimeAliasCollector(ast.NodeVisitor):
    """Collect aliases for the datetime module and datetime class."""

    def __init__(self) -> None:
        self.module_aliases: Set[str] = {"datetime"}
        self.class_aliases: Set[str] = set()

    def visit_Import(self, node: ast.Import) -> None:  # noqa: D401
        for alias in node.names:
            if alias.name == "datetime":
                name = alias.asname or alias.name
                self.module_aliases.add(name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: D401
        if node.module == "datetime":
            for alias in node.names:
                if alias.name == "datetime":
                    name = alias.asname or alias.name
                    self.class_aliases.add(name)
                elif alias.name == "*":
                    self.class_aliases.add("datetime")
                    self.module_aliases.add("datetime")
        self.generic_visit(node)


class DatetimeUtcnowReplacer(ast.NodeVisitor):
    """Locate utcnow() calls that should be rewritten."""

    def __init__(self, source: str, module_aliases: Set[str], class_aliases: Set[str]) -> None:
        self.source = source
        self.module_aliases = module_aliases
        self.class_aliases = class_aliases
        self.replacements: List[Tuple[int, int, str]] = []
        self._line_offsets = self._compute_line_offsets(source)

    @staticmethod
    def _compute_line_offsets(source: str) -> List[int]:
        offsets: List[int] = []
        running = 0
        for line in source.splitlines(True):
            offsets.append(running)
            running += len(line)
        return offsets

    def _offset(self, lineno: int, col_offset: int) -> int:
        return self._line_offsets[lineno - 1] + col_offset

    def visit_Call(self, node: ast.Call) -> None:  # noqa: D401
        self.generic_visit(node)

        if not isinstance(node.func, ast.Attribute):
            return
        if node.func.attr != "utcnow":
            return
        if node.args or node.keywords:
            return

        base = node.func.value
        if not self._is_datetime_target(base):
            return

        base_segment = ast.get_source_segment(self.source, base)
        if base_segment is None:
            return

        replacement = f"{base_segment}.now(timezone.utc)"
        end_lineno = getattr(node, "end_lineno", None)
        end_col = getattr(node, "end_col_offset", None)
        if end_lineno is None or end_col is None:
            return

        start = self._offset(node.lineno, node.col_offset)
        end = self._offset(end_lineno, end_col)
        self.replacements.append((start, end, replacement))

    def _is_datetime_target(self, expr: ast.AST) -> bool:
        if isinstance(expr, ast.Name) and expr.id in self.class_aliases:
            return True
        if isinstance(expr, ast.Attribute) and expr.attr == "datetime":
            base = expr.value
            if isinstance(base, ast.Name) and base.id in self.module_aliases:
                return True
        return False


def replace_utcnow(content: str) -> tuple[str, int]:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return content, 0

    collector = DatetimeAliasCollector()
    collector.visit(tree)

    replacer = DatetimeUtcnowReplacer(content, collector.module_aliases, collector.class_aliases)
    replacer.visit(tree)

    if not replacer.replacements:
        return content, 0

    updated = content
    for start, end, replacement in sorted(replacer.replacements, key=lambda item: item[0], reverse=True):
        updated = updated[:start] + replacement + updated[end:]

    return updated, len(replacer.replacements)


def timezone_already_imported(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "datetime":
            for alias in node.names:
                if alias.name == "timezone":
                    return True
        if isinstance(node, ast.ImportFrom) and node.module == "datetime" and node.level == 0:
            for alias in node.names:
                if alias.asname == "timezone":
                    return True
    return False


def ensure_timezone_import(text: str) -> tuple[str, bool]:
    if "timezone.utc" not in text:
        return text, False

    try:
        tree = ast.parse(text)
    except SyntaxError:
        return text, False

    if timezone_already_imported(tree):
        return text, False

    if "from datetime import timezone" in text:
        return text, False

    lines = text.splitlines()
    insert_idx = find_import_insertion_index(lines)
    lines.insert(insert_idx, "from datetime import timezone")
    new_text = "\n".join(lines)
    if text.endswith("\n"):
        new_text += "\n"
    return new_text, True


def find_import_insertion_index(lines: List[str]) -> int:
    insert_idx = 0
    idx = 0
    in_docstring = False
    doc_delim: Optional[str] = None

    while idx < len(lines):
        line = lines[idx]
        stripped = line.strip()

        if idx == 0 and line.startswith("#!"):
            insert_idx = idx + 1
            idx += 1
            continue

        if not in_docstring and stripped.startswith(("\"\"\"", "'''")):
            if stripped.count("\"\"\"") == 1:
                in_docstring = True
                doc_delim = "\"\"\""
            elif stripped.count("'''") == 1:
                in_docstring = True
                doc_delim = "'''"
            insert_idx = idx + 1
            idx += 1
            continue

        if in_docstring:
            insert_idx = idx + 1
            if doc_delim and doc_delim in stripped:
                in_docstring = False
                doc_delim = None
            idx += 1
            continue

        if stripped.startswith("from ") or stripped.startswith("import "):
            insert_idx = idx + 1
            idx += 1
            continue

        if stripped == "" or stripped.startswith("#"):
            insert_idx = idx + 1
            idx += 1
            continue

        break

    return insert_idx


def create_backup(path: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_name = f"{path.name}.{timestamp}.bak"
    backup_path = path.with_name(backup_name)
    shutil.copy2(path, backup_path)
    return backup_path


def process_file(path: Path, *, dry_run: bool) -> Optional[FileUpdate]:
    original = path.read_text(encoding="utf-8")
    updated, replacements = replace_utcnow(original)
    if replacements == 0:
        return None

    updated, import_added = ensure_timezone_import(updated)

    try:
        ast.parse(updated, filename=str(path))
    except SyntaxError as exc:  # pragma: no cover - defensive guard
        print(f"[ERROR] Syntax check failed for {path}: {exc}", file=sys.stderr)
        return None

    backup_path = None
    if not dry_run:
        backup_path = create_backup(path)
        path.write_text(updated, encoding="utf-8")

    return FileUpdate(path=path, replacements=replacements, backup_path=backup_path, import_added=import_added)


def main() -> None:
    parser = argparse.ArgumentParser(description="Modernise datetime usage by replacing utcnow calls")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Repository root to scan")
    parser.add_argument("--include", nargs="*", help="Optional subset of files or directories to process")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing files")
    args = parser.parse_args()

    root = args.root.resolve()
    updates: List[FileUpdate] = []

    for file_path in iter_python_files(root, include=args.include):
        update = process_file(file_path, dry_run=args.dry_run)
        if update is not None:
            updates.append(update)
            action = "DRY-RUN would update" if args.dry_run else "Updated"
            import_note = " (added timezone import)" if update.import_added else ""
            print(f"{action}: {file_path}{import_note} [replacements={update.replacements}]")

    if not updates:
        print("No datetime.utcnow() usages found.")
        return

    replaced = sum(update.replacements for update in updates)
    backups = sum(1 for update in updates if update.backup_path is not None)

    if args.dry_run:
        print(f"Dry run complete: {len(updates)} file(s) would change, {replaced} occurrence(s) replaced.")
    else:
        print(f"Updated {len(updates)} file(s); {replaced} occurrence(s) replaced, {backups} backup(s) created.")


if __name__ == "__main__":
    main()
