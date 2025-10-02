"""Cleanup orchestration utilities built on top of ``utils.data_paths``."""

from __future__ import annotations

import fnmatch
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from utils import data_paths

LOGGER = logging.getLogger(__name__)


class CleanupError(RuntimeError):
    """Raised when a cleanup strategy fails irrecoverably."""


@dataclass
class CleanupConfig:
    """User-facing configuration for orchestrating cleanup operations."""

    dry_run: bool = True
    max_temp_age_days: int = 7
    environment: str = "development"
    interactive: bool = False
    auto_backup: bool = True
    validation_enabled: bool = True
    exclude_patterns: Sequence[str] = field(default_factory=list)
    confirmation_callback: Optional[Callable[[str], bool]] = None


@dataclass
class CleanupReport:
    strategy: str
    started_at: datetime
    finished_at: datetime
    payload: Dict[str, Any]


class FileClassifier:
    """Classify files by type and track naive dependency relationships."""

    PYTHON_SUFFIXES: Tuple[str, ...] = (".py",)
    CONFIG_SUFFIXES: Tuple[str, ...] = (".json", ".yaml", ".yml", ".ini", ".toml")
    DATA_SUFFIXES: Tuple[str, ...] = (".csv", ".json", ".parquet", ".db")
    DOC_SUFFIXES: Tuple[str, ...] = (".md",)

    def __init__(self, repo_root: Optional[Path] = None, exclude_patterns: Optional[Sequence[str]] = None) -> None:
        self.repo_root = (repo_root or Path.cwd()).resolve()
        self.exclude_patterns = tuple(exclude_patterns or ())

    def classify(self, path: Path) -> str:
        path = self._resolve(path)
        name = path.name

        if path.suffix in self.PYTHON_SUFFIXES:
            return "test" if name.startswith("test_") else "python"
        if path.suffix in self.CONFIG_SUFFIXES:
            return "config"
        if path.suffix in self.DOC_SUFFIXES:
            return "docs"
        if path.suffix in self.DATA_SUFFIXES:
            return "data"
        if name.startswith("=") or name.endswith(".bak"):
            return "legacy"
        return "other"

    def is_temporary(self, path: Path) -> bool:
        path = self._resolve(path)
        lower = path.name.lower()
        return any(
            lower.endswith(suffix)
            for suffix in (".tmp", ".temp", ".log", ".cache", ".session")
        )

    def is_backup(self, path: Path) -> bool:
        path = self._resolve(path)
        return path.suffix in {".bak", ".old"} or path.name.endswith(".backup")

    def dependencies(self, path: Path) -> List[str]:
        file_type = self.classify(path)
        if file_type != "python":
            return []

        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return []

        matches = re.findall(r"^\s*(?:from|import)\s+([\w\.]+)", text, flags=re.MULTILINE)
        return sorted(set(matches))

    def dependents(self, path: Path) -> List[Path]:
        file_type = self.classify(path)
        if file_type != "python":
            return []

        module_name = self._module_name(path)
        dependents: List[Path] = []
        for python_file in self.repo_root.rglob("*.py"):
            if python_file == path:
                continue
            if self._is_excluded(python_file):
                continue
            try:
                text = python_file.read_text(encoding="utf-8")
            except OSError:
                continue
            if re.search(rf"(^|\s)(from|import)\s+{re.escape(module_name)}\b", text):
                dependents.append(python_file)
        return dependents

    def _module_name(self, path: Path) -> str:
        try:
            relative = path.resolve().relative_to(self.repo_root)
        except ValueError:
            return path.stem
        sans_suffix = relative.with_suffix("")
        return ".".join(sans_suffix.parts)

    def _resolve(self, path: Path) -> Path:
        return path if path.is_absolute() else (self.repo_root / path).resolve()

    def resolve(self, path: Path) -> Path:
        return self._resolve(path)

    def _is_excluded(self, path: Path) -> bool:
        try:
            relative = path.resolve().relative_to(self.repo_root)
        except ValueError:
            relative = path
        relative_str = str(relative)
        return any(fnmatch.fnmatch(relative_str, pattern) for pattern in self.exclude_patterns)


class CleanupStrategy:
    name: str = "base"

    def preview(self, manager: "CleanupManager") -> Dict[str, Any]:
        return {}

    def execute(self, manager: "CleanupManager", preview: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        raise NotImplementedError


class LegacyFileCleanup(CleanupStrategy):
    name = "legacy-files"

    def preview(self, manager: "CleanupManager") -> Dict[str, Any]:
        return {"file_groups": data_paths.find_unused_legacy_files()}

    def execute(self, manager: "CleanupManager", preview: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        groups = (preview or self.preview(manager)).get("file_groups", {})
        manager.validate_dependencies(self._collect_paths(groups))
        counts = data_paths.cleanup_legacy_files(groups, dry_run=manager.config.dry_run)
        return {"removed_groups": counts}

    @staticmethod
    def _collect_paths(groups: Dict[str, List[Path]]) -> List[Path]:
        paths: List[Path] = []
        for items in groups.values():
            paths.extend(items)
        return paths


class EmptyDirectoryCleanup(CleanupStrategy):
    name = "empty-directories"

    def preview(self, manager: "CleanupManager") -> Dict[str, Any]:
        return {"directories": data_paths.find_empty_directories()}

    def execute(self, manager: "CleanupManager", preview: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        directories = (preview or self.preview(manager)).get("directories", [])
        removed = data_paths.cleanup_empty_directories(directories, dry_run=manager.config.dry_run)
        return {"removed": removed}


class TemporaryFileCleanup(CleanupStrategy):
    name = "temporary-files"

    def execute(self, manager: "CleanupManager", preview: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        removed = data_paths.cleanup_temporary_files(
            max_age_days=manager.config.max_temp_age_days,
            dry_run=manager.config.dry_run,
        )
        return {"removed": removed, "max_age_days": manager.config.max_temp_age_days}


class NamingStandardization(CleanupStrategy):
    name = "naming-standardisation"

    def preview(self, manager: "CleanupManager") -> Dict[str, Any]:
        return {"violations": data_paths.standardize_naming_conventions(dry_run=True)}

    def execute(self, manager: "CleanupManager", preview: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        preview_payload = preview or self.preview(manager)
        violations: Dict[str, Any] = preview_payload.get("violations", {})

        if manager.config.dry_run:
            return {"renamed": violations, "dry_run": True}

        allowed_paths: Set[Path] = set()
        skipped: List[Dict[str, Any]] = []

        for stats in violations.values():
            renames = stats.get("renames", [])
            for action in renames:
                raw_source = getattr(action, "source", None) or Path(action.get("source"))
                source_path = manager.classifier.resolve(raw_source)
                target_name = getattr(action, "target_name", None) or action.get("target_name")
                target_path = getattr(action, "target_path", None) or Path(action.get("target_path"))

                try:
                    manager.validate_dependencies([source_path])
                except CleanupError as exc:
                    manager.logger.warning(
                        "Skipping rename %s -> %s due to dependency validation: %s",
                        source_path,
                        target_name,
                        exc,
                    )
                    skipped.append(
                        {
                            "source": source_path,
                            "target": target_path,
                            "reason": str(exc),
                        }
                    )
                    continue

                allowed_paths.add(source_path)

        renamed = data_paths.standardize_naming_conventions(
            dry_run=False,
            allow_paths=list(allowed_paths),
        )

        return {"renamed": renamed, "dry_run": False, "skipped": skipped}


class DuplicateConfigCleanup(CleanupStrategy):
    name = "duplicate-configs"

    def preview(self, manager: "CleanupManager") -> Dict[str, Any]:
        return {"duplicates": data_paths.find_duplicate_configs()}

    def execute(self, manager: "CleanupManager", preview: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        duplicates = (preview or self.preview(manager)).get("duplicates", {})
        all_paths: List[Path] = []
        for bucket in duplicates.values():
            all_paths.extend(bucket)
        manager.validate_dependencies(all_paths)
        consolidated = data_paths.consolidate_duplicate_configs(
            duplicates,
            dry_run=manager.config.dry_run,
        )
        return {"consolidated": consolidated}


class CleanupManager:
    """Coordinate cleanup strategies with validation and rollback."""

    def __init__(
        self,
        config: Optional[CleanupConfig] = None,
        logger: Optional[logging.Logger] = None,
        runtime_config_path: Optional[Path] = None,
    ) -> None:
        self.config = config or CleanupConfig()
        self.logger = logger or LOGGER
        self.runtime_config_path = runtime_config_path or Path("config/cleanup_config.json")
        self.runtime_config: Dict[str, Any] = self._load_runtime_config()
        self.classifier = FileClassifier(
            repo_root=data_paths.REPO_ROOT,
            exclude_patterns=self._get_exclusion_patterns(),
        )
        self.strategies: Dict[str, CleanupStrategy] = {}
        self.operation_log: List[CleanupReport] = []
        self._register_default_strategies()

    def _load_runtime_config(self) -> Dict[str, Any]:
        if not self.runtime_config_path.exists():
            return {}
        try:
            return json.loads(self.runtime_config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.logger.warning("Failed to load runtime cleanup config: %s", exc)
            return {}

    def _get_exclusion_patterns(self) -> Sequence[str]:
        section = self.runtime_config.get("safety_and_validation", {})
        return tuple(section.get("exclusions", []))

    def _register_default_strategies(self) -> None:
        self.register_strategy(LegacyFileCleanup())
        self.register_strategy(EmptyDirectoryCleanup())
        self.register_strategy(TemporaryFileCleanup())
        self.register_strategy(NamingStandardization())
        self.register_strategy(DuplicateConfigCleanup())

    def register_strategy(self, strategy: CleanupStrategy) -> None:
        self.strategies[strategy.name] = strategy

    def require_confirmation(self, message: str) -> bool:
        if not self.config.interactive:
            return True
        if self.config.confirmation_callback:
            return self.config.confirmation_callback(message)
        response = input(f"{message} [y/N]: ").strip().lower()
        return response in {"y", "yes"}

    def validate_dependencies(self, paths: Iterable[Path]) -> None:
        if not self.config.validation_enabled:
            return

        protected_paths = {
            self.classifier.resolve(Path(p))
            for p in self.runtime_config.get("safety_and_validation", {}).get("protected_paths", [])
        }

        for path in paths:
            resolved = self.classifier.resolve(path)
            if resolved in protected_paths:
                raise CleanupError(f"Refusing to modify protected path: {resolved}")

            dependents = self.classifier.dependents(resolved)
            if dependents:
                dependent_list = ", ".join(str(dep) for dep in dependents[:5])
                raise CleanupError(
                    f"Cleanup aborted for {resolved}; dependents detected: {dependent_list}"
                )

    def execute_strategy(self, name: str) -> Dict[str, Any]:
        if name not in self.strategies:
            raise CleanupError(f"Unknown cleanup strategy: {name}")

        strategy = self.strategies[name]
        preview = strategy.preview(self)

        if not self.require_confirmation(f"Execute cleanup strategy '{name}'?"):
            self.logger.info("Skipping strategy %s per user choice", name)
            return {"skipped": True}

        payload = self._execute_with_reporting(strategy, preview)
        return payload

    def execute_multiple(self, strategy_names: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        results: Dict[str, Dict[str, Any]] = {}
        for name in strategy_names:
            results[name] = self.execute_strategy(name)
        return results

    def run_full_cleanup(self) -> Dict[str, Dict[str, Any]]:
        results: Dict[str, Dict[str, Any]] = {}
        for name in self.strategies:
            results[name] = self.execute_strategy(name)
        return results

    def _execute_with_reporting(
        self,
        strategy: CleanupStrategy,
        preview: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        start = datetime.now(timezone.utc)
        try:
            payload = strategy.execute(self, preview=preview)
        except CleanupError:
            raise
        except Exception as exc:
            raise CleanupError(f"Strategy '{strategy.name}' failed: {exc}") from exc
        else:
            report = CleanupReport(
                strategy=strategy.name,
                started_at=start,
                finished_at=datetime.now(timezone.utc),
                payload=payload,
            )
            self.operation_log.append(report)
            return payload

    def summary(self) -> List[CleanupReport]:
        return list(self.operation_log)


__all__ = [
    "CleanupManager",
    "CleanupConfig",
    "CleanupError",
    "CleanupReport",
    "FileClassifier",
    "CleanupStrategy",
    "LegacyFileCleanup",
    "TemporaryFileCleanup",
    "EmptyDirectoryCleanup",
    "DuplicateConfigCleanup",
    "NamingStandardization",
]
