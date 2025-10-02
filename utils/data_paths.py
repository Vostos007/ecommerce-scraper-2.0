"""Helpers for per-site data directories, backups, and legacy migrations."""

from __future__ import annotations

import configparser
import io
import fnmatch
import hashlib
import json
import logging
import os
import re
import shutil
import time
from collections import defaultdict
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - fallback for older interpreters
    tomllib = None

try:  # pragma: no cover - optional dependency
    import yaml  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover - environment without PyYAML
    yaml = None


SITE_DATA_ROOT = Path("data/sites")
COMPILED_DATA_ROOT = SITE_DATA_ROOT / "_compiled"
LEGACY_EXPORT_ROOT = Path("data/exports")
LEGACY_HISTORY_ROOT = Path("data/history/exports")

REPO_ROOT = Path.cwd()
CLEANUP_CONFIG_PATH = Path("config/cleanup_config.json")

DEFAULT_TEMP_DIRS: Tuple[Path, ...] = (
    Path("data/tmp"),
    Path("logs"),
    Path("reports/tmp"),
)

DEFAULT_LEGACY_PATTERNS: Dict[str, Dict[str, Any]] = {
    "root_version_files": {"include": ["=*.0", "=*.0.*"], "exclude": []},
    "root_test_files": {"include": ["test_*.py"], "exclude": []},
    "database_backups": {
        "include": ["**/*.bak", "**/*.db-wal", "**/*.db-shm"],
        "exclude": ["venv/**", "htmlcov/**"],
        "policy": "backups",
    },
    "temporary_logs": {
        "include": ["logs/**/*.log", "logs/**/*_temp*", "logs/**/*.tmp"],
        "exclude": [],
        "policy": "logs",
    },
    "selector_memory": {
        "include": ["data/selector_memory/**/*"],
        "exclude": ["**/*.keep", "**/.gitkeep", "data/selector_memory/current/**"],
        "policy": "selector_memory",
    },
}

NAMING_CONVENTIONS: Dict[str, re.Pattern[str]] = {
    "python": re.compile(r"^[a-z0-9_]+\.py$"),
    "config": re.compile(r"^[a-z0-9_]+\.(json|ya?ml|ini)$"),
    "data": re.compile(r"^[a-z0-9_-]+\.(csv|json|parquet|db)$"),
    "test": re.compile(r"^test_[a-z0-9_]+\.py$"),
    "docs": re.compile(r"^[a-z0-9-]+\.md$"),
}

_CLEANUP_CONFIG_CACHE: Optional[Tuple[float, Dict[str, Any]]] = None
_CONFIG_CACHE_TTL_SECONDS = 30

BACKUP_DIR_NAME = "backups"
MAX_BACKUPS = 5
BACKUP_RETENTION_DAYS = 30
DEFAULT_CHECKSUM_ALGORITHM = "sha256"

logger = logging.getLogger(__name__)


_LEGACY_CACHE_FILES: Dict[str, str] = {
    "6wool.ru": "6wool_urls.txt",
    "atmospherestore.ru": "atmosphere_urls.txt",
    "ili-ili.com": "iliili_urls.txt",
    "initki.ru": "initki_urls.txt",
    "mpyarn.ru": "mpyarn_urls.txt",
    "sittingknitting.ru": "sittingknitting_urls.txt",
    "triskeli.ru": "triskeli_urls.txt",
    "knitshop.ru": "knitshop_urls.txt",
}


@dataclass(frozen=True)
class SiteDataPaths:
    """Resolved paths for all artefacts belonging to a single domain.

    Prefer attribute access (e.g. ``paths.history_csv``) over tuple unpacking.
    The ``backup_dir`` attribute exposes the per-site directory used for
    timestamped history backups.
    """

    domain: str
    root: Path
    cache_dir: Path
    cache_file: Path
    exports_dir: Path
    latest_export: Path
    history_dir: Path
    backup_dir: Path
    history_csv: Path
    history_json: Path


@dataclass(frozen=True)
class RenameAction:
    source: Path
    target_name: str
    target_path: Path


@dataclass(frozen=True)
class ConfigFileInfo:
    path: Path
    format: str
    parsed: Any
    raw_text: Optional[str]
    mtime: float
    parse_success: bool


def _migrate_legacy_file(source: Path, destination: Path) -> None:
    """Move a legacy export if present and the destination is empty."""

    if destination.exists() or not source.exists():
        return

    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        source.rename(destination)
    except OSError:
        destination.write_bytes(source.read_bytes())
        source.unlink(missing_ok=True)
    logger.info("Migrated legacy artefact from %s to %s", source, destination)


def get_site_paths(domain: str) -> SiteDataPaths:
    sanitized = domain.strip().lower()
    root = SITE_DATA_ROOT / sanitized
    cache_dir = root / "cache"
    exports_dir = root / "exports"
    history_dir = root / "history"
    backup_dir = history_dir / BACKUP_DIR_NAME

    for directory in (cache_dir, exports_dir, history_dir, backup_dir):
        directory.mkdir(parents=True, exist_ok=True)

    cache_name = _LEGACY_CACHE_FILES.get(sanitized, "cached_urls.txt")
    cache_file = cache_dir / cache_name
    latest_export = exports_dir / "latest.json"
    history_csv = history_dir / "history.csv"
    history_json = history_dir / "history.analytics.json"

    _migrate_legacy_file(LEGACY_EXPORT_ROOT / cache_name, cache_file)
    _migrate_legacy_file(LEGACY_EXPORT_ROOT / f"{sanitized}_latest.json", latest_export)
    _migrate_legacy_file(LEGACY_HISTORY_ROOT / f"{sanitized}.csv", history_csv)
    _migrate_legacy_file(
        LEGACY_HISTORY_ROOT / f"{sanitized}.analytics.json", history_json
    )

    return SiteDataPaths(
        domain=sanitized,
        root=root,
        cache_dir=cache_dir,
        cache_file=cache_file,
        exports_dir=exports_dir,
        latest_export=latest_export,
        history_dir=history_dir,
        backup_dir=backup_dir,
        history_csv=history_csv,
        history_json=history_json,
    )


def iter_history_csvs() -> Iterator[Path]:
    """Yield all per-site history CSV files currently available."""

    if not SITE_DATA_ROOT.exists():
        return iter(())

    for site_dir in sorted(SITE_DATA_ROOT.iterdir()):
        if not site_dir.is_dir() or site_dir.name.startswith("_"):
            continue
        history_csv = site_dir / "history" / "history.csv"
        if history_csv.exists():
            yield history_csv


def compiled_workbook_path() -> Path:
    COMPILED_DATA_ROOT.mkdir(parents=True, exist_ok=True)
    return COMPILED_DATA_ROOT / "history_wide.xlsx"


def ensure_all_site_roots(domains: Iterable[str]) -> List[SiteDataPaths]:
    """Convenience helper that resolves paths for a list of domains."""

    return [get_site_paths(domain) for domain in domains]


def iter_site_domains() -> Iterator[str]:
    """Yield domains for which a site data directory exists."""

    if not SITE_DATA_ROOT.exists():
        return iter(())

    for site_dir in sorted(SITE_DATA_ROOT.iterdir()):
        if site_dir.is_dir() and not site_dir.name.startswith("_"):
            yield site_dir.name


def iter_latest_exports() -> Iterator[Tuple[str, Path]]:
    """Yield `(domain, latest_export_path)` pairs for all sites that have exports."""

    for domain in iter_site_domains():
        latest_path = SITE_DATA_ROOT / domain / "exports" / "latest.json"
        if latest_path.exists():
            yield domain, latest_path


def iter_history_dirs() -> Iterator[Tuple[str, Path]]:
    for domain in iter_site_domains():
        history_path = SITE_DATA_ROOT / domain / "history"
        if history_path.exists():
            yield domain, history_path


def migrate_all_legacy_files(domains: Optional[Iterable[str]] = None) -> List[SiteDataPaths]:
    """Migrate all known legacy artefacts into the new per-site structure."""

    discovered: set[str] = set(domains or [])

    generic_exports: Dict[str, Path] = {}
    if not discovered:
        if LEGACY_EXPORT_ROOT.exists():
            for legacy_file in LEGACY_EXPORT_ROOT.iterdir():
                name = legacy_file.name.lower()
                if name == "httpx_latest.json":
                    generic_exports[name] = legacy_file
                    continue
                if "_latest.json" in name:
                    discovered.add(name.replace("_latest.json", ""))
        if LEGACY_HISTORY_ROOT.exists():
            for legacy_file in LEGACY_HISTORY_ROOT.iterdir():
                name = legacy_file.name.lower()
                if name.endswith(".csv"):
                    discovered.add(name.replace(".csv", ""))
                elif name.endswith(".analytics.json"):
                    discovered.add(name.replace(".analytics.json", ""))
        discovered.update(_LEGACY_CACHE_FILES.keys())

    migrated: List[SiteDataPaths] = []
    for domain in sorted(discovered):
        paths = get_site_paths(domain)
        migrated.append(paths)

    if generic_exports:
        httpx_dir = COMPILED_DATA_ROOT / "httpx"
        httpx_dir.mkdir(parents=True, exist_ok=True)
        for path in generic_exports.values():
            destination = httpx_dir / path.name
            if destination.exists():
                continue
            try:
                path.rename(destination)
            except OSError:
                destination.write_bytes(path.read_bytes())
                path.unlink(missing_ok=True)
            logger.info("Migrated generic export %s to %s", path, destination)

    return migrated


def legacy_migration_report() -> Dict[str, Any]:
    """Return information about remaining legacy files for diagnostics."""

    report: Dict[str, Any] = {
        "remaining_exports": [],
        "remaining_history": [],
        "site_directories": list(iter_site_domains()),
    }

    if LEGACY_EXPORT_ROOT.exists():
        remaining = []
        for path in LEGACY_EXPORT_ROOT.glob("*"):
            if not path.is_file():
                continue
            if path.name.lower() in {"httpx_latest.json"}:
                continue
            remaining.append(str(path))
        report["remaining_exports"] = remaining
    if LEGACY_HISTORY_ROOT.exists():
        report["remaining_history"] = [
            str(path) for path in LEGACY_HISTORY_ROOT.glob("*" ) if path.is_file()
        ]

    return report


def resolve_backup_dir(file_path: Path) -> Path:
    """Return the directory used to store backups for ``file_path``."""

    return file_path.parent / BACKUP_DIR_NAME


def create_timestamped_backup(file_path: Path) -> Optional[Path]:
    """Create a timestamped backup of ``file_path`` if it exists."""

    if not file_path.exists() or not file_path.is_file():
        return None

    backup_dir = resolve_backup_dir(file_path)
    backup_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    original_name = file_path.name
    stem, suffix = original_name, ""
    if file_path.suffix:
        stem = file_path.stem
        suffix = file_path.suffix

    backup_name = f"{stem}_{timestamp}{suffix}.bak"
    backup_path = backup_dir / backup_name

    shutil.copy2(file_path, backup_path)
    return backup_path


def get_backup_files(file_path: Path) -> List[Path]:
    """Return a sorted list (newest first) of backup files for ``file_path``."""

    backup_dir = resolve_backup_dir(file_path)
    if not backup_dir.exists():
        return []

    stem = file_path.stem
    backups = [p for p in backup_dir.glob(f"{stem}_*{file_path.suffix}.bak") if p.is_file()]
    backups.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return backups


def cleanup_old_backups(file_path: Path, keep_count: int = MAX_BACKUPS) -> None:
    backups = get_backup_files(file_path)
    for backup in backups[keep_count:]:
        try:
            backup.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Failed to remove backup %s: %s", backup, exc)

    if BACKUP_RETENTION_DAYS <= 0:
        return

    cutoff = datetime.now(timezone.utc) - timedelta(days=BACKUP_RETENTION_DAYS)
    for backup in backups:
        try:
            mtime = datetime.fromtimestamp(backup.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if mtime < cutoff:
            try:
                backup.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("Failed to remove expired backup %s: %s", backup, exc)


def get_file_checksum(file_path: Path, algorithm: str = DEFAULT_CHECKSUM_ALGORITHM) -> Optional[str]:
    if not file_path.exists() or not file_path.is_file():
        return None

    try:
        hasher = hashlib.new(algorithm)
    except ValueError:
        hasher = hashlib.sha256()

    try:
        with file_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(8192), b""):
                hasher.update(chunk)
        return hasher.hexdigest()
    except OSError as exc:
        logger.warning("Failed to compute checksum for %s: %s", file_path, exc)
        return None


def file_content_changed(file_path: Path, new_content: str, algorithm: str = DEFAULT_CHECKSUM_ALGORITHM) -> bool:
    existing_checksum = get_file_checksum(file_path, algorithm=algorithm)
    if existing_checksum is None:
        return True

    hasher = hashlib.new(algorithm) if algorithm in hashlib.algorithms_available else hashlib.sha256()
    hasher.update(new_content.encode("utf-8"))
    return existing_checksum != hasher.hexdigest()


def _load_cleanup_config(force_refresh: bool = False) -> Dict[str, Any]:
    """Load cleanup configuration with memoisation and error handling."""

    global _CLEANUP_CONFIG_CACHE

    if not force_refresh and _CLEANUP_CONFIG_CACHE is not None:
        cached_at, config = _CLEANUP_CONFIG_CACHE
        if time.time() - cached_at < _CONFIG_CACHE_TTL_SECONDS:
            return config

    if not CLEANUP_CONFIG_PATH.exists():
        _CLEANUP_CONFIG_CACHE = (time.time(), {})
        return {}

    try:
        config = json.loads(CLEANUP_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Unable to load cleanup configuration: %s", exc)
        config = {}

    _CLEANUP_CONFIG_CACHE = (time.time(), config)
    return config


def _resolve_repo_path(path: Path) -> Path:
    if path.is_absolute():
        return path
    return (REPO_ROOT / path).resolve()


def _normalised_relative_path(path: Path) -> str:
    try:
        relative = path.resolve().relative_to(REPO_ROOT)
    except (FileNotFoundError, ValueError):
        relative = path
    return str(relative).replace(os.sep, "/")


def _is_excluded(path: Path, patterns: Sequence[str]) -> bool:
    if not patterns:
        return False

    relative_str = _normalised_relative_path(path)
    for pattern in patterns:
        normalised_pattern = pattern.replace("\\", "/")
        if fnmatch.fnmatch(relative_str, normalised_pattern):
            return True
    return False


def _iter_paths_from_patterns(
    patterns: Sequence[str],
    *,
    include_directories: bool = False,
    base: Optional[Path] = None,
) -> Iterator[Path]:
    base_path = _resolve_repo_path(base or REPO_ROOT)
    seen: Set[Path] = set()

    for pattern in patterns:
        if not pattern:
            continue
        for candidate in base_path.glob(pattern):
            resolved = candidate.resolve()
            if resolved in seen:
                continue
            if not include_directories and not resolved.is_file():
                continue
            seen.add(resolved)
            yield resolved


def _normalise_grouped_paths(groups: Dict[str, Iterable[Path]]) -> Dict[str, List[Path]]:
    normalised: Dict[str, List[Path]] = {}
    for group, paths in groups.items():
        normalised[group] = sorted({_resolve_repo_path(Path(path)) for path in paths})
    return normalised


def _resolve_legacy_group_config() -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    config = _load_cleanup_config()
    legacy_cfg = config.get("legacy_file_cleanup", {})
    configured_groups: Dict[str, Any] = legacy_cfg.get("patterns", {})
    retention_cfg: Dict[str, Any] = legacy_cfg.get("retention_policies", {})

    groups: Dict[str, Dict[str, Any]] = {}
    groups.update(DEFAULT_LEGACY_PATTERNS)

    for group_name, value in configured_groups.items():
        include: Sequence[str]
        exclude: Sequence[str]
        policy: Optional[str] = None
        if isinstance(value, dict):
            include = value.get("include", [])
            exclude = value.get("exclude", [])
            policy = value.get("policy")
        elif isinstance(value, list):
            include = value
            exclude = []
        else:
            include = [str(value)]
            exclude = []

        merged = {
            "include": [str(pattern) for pattern in include],
            "exclude": [str(pattern) for pattern in exclude],
        }
        if group_name in groups and "policy" in groups[group_name]:
            merged["policy"] = groups[group_name]["policy"]
        if policy:
            merged["policy"] = policy
        groups[group_name] = merged

    return groups, retention_cfg


def _resolve_retention_days(policy_key: Optional[str], retention_config: Dict[str, Any]) -> Optional[int]:
    if not retention_config:
        return None

    if policy_key:
        lookup_keys = [policy_key, f"{policy_key}_days", f"{policy_key}_day"]
        for key in lookup_keys:
            value = retention_config.get(key)
            if isinstance(value, (int, float)):
                return int(value)

    default_value = retention_config.get("default_days")
    if isinstance(default_value, (int, float)):
        return int(default_value)
    return None


def _passes_retention_policy(path: Path, retention_days: Optional[int], *, now: Optional[float] = None) -> bool:
    if retention_days is None or retention_days <= 0:
        return True
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False
    cutoff = (now or time.time()) - retention_days * 86400
    return mtime <= cutoff


def _is_preserved_directory(directory: Path, preserved: Iterable[Path]) -> bool:
    for preserved_dir in preserved:
        try:
            preserved_resolved = preserved_dir.resolve()
        except OSError:
            preserved_resolved = preserved_dir
        if directory == preserved_resolved:
            return True
        try:
            directory.relative_to(preserved_resolved)
            return True
        except ValueError:
            continue
    return False


def _parse_temporary_patterns(patterns: Sequence[Any]) -> List[Dict[str, Any]]:
    parsed: List[Dict[str, Any]] = []
    for entry in patterns:
        if isinstance(entry, str):
            parsed.append({"pattern": entry, "backup": True})
        elif isinstance(entry, dict):
            pattern = entry.get("pattern") or entry.get("glob")
            if not pattern:
                continue
            backup = entry.get("backup")
            parsed.append({
                "pattern": str(pattern),
                "backup": True if backup is None else bool(backup),
            })
    return parsed


def _select_canonical_config_path(paths: Sequence[Path], preferences: Sequence[str]) -> Path:
    if not paths:
        raise ValueError("No paths provided for canonical selection")

    normalised_prefs = [pref.replace("\\", "/") for pref in preferences]

    def preference_key(path: Path) -> Tuple[int, int, str]:
        rel = _normalised_relative_path(path)
        try:
            rel_path = path.resolve().relative_to(REPO_ROOT)
            rel_str = str(rel_path).replace(os.sep, "/")
        except (FileNotFoundError, ValueError):
            rel_str = rel
        for index, prefix in enumerate(normalised_prefs):
            if rel_str.startswith(prefix):
                return (index, len(rel_str), rel_str)
        return (len(normalised_prefs), len(rel_str), rel_str)

    return min(paths, key=preference_key)


def validate_naming_convention(file_path: Path, file_type: str) -> bool:
    pattern = NAMING_CONVENTIONS.get(file_type)
    if pattern is None:
        return True
    return bool(pattern.match(file_path.name))


def suggest_standard_name(file_path: Path, file_type: str) -> str:
    delimiter_map = {
        "docs": "-",
        "data": "-",
        "config": "_",
        "python": "_",
        "test": "_",
    }
    delimiter = delimiter_map.get(file_type, "_")

    stem = file_path.stem
    suffix = file_path.suffix.lower()
    normalised = re.sub(r"[^0-9A-Za-z]+", delimiter, stem).strip(delimiter).lower()
    if not normalised:
        normalised = "file"

    if file_type == "test" and not normalised.startswith("test_"):
        normalised = f"test_{normalised}"

    if file_type == "docs":
        normalised = normalised.replace("_", "-")

    return f"{normalised}{suffix}"


def _compute_target_path(
    candidate: Path,
    suggestion: str,
    reserved_targets: Set[Path],
    *,
    reserve: bool,
) -> Path:
    base_target = candidate.with_name(suggestion)
    base_stem = base_target.stem
    suffix = base_target.suffix
    target = base_target
    counter = 1
    while target.exists() or (reserve and target in reserved_targets):
        target = candidate.with_name(f"{base_stem}_{counter}{suffix}")
        counter += 1
    if reserve:
        reserved_targets.add(target)
    return target


def find_unused_legacy_files() -> Dict[str, List[Path]]:
    """Detect legacy artefacts across the repository based on configured patterns."""

    groups, retention_cfg = _resolve_legacy_group_config()

    legacy_files: Dict[str, List[Path]] = defaultdict(list)
    now = time.time()
    for group_name, pattern_set in groups.items():
        include_patterns = pattern_set.get("include", [])
        exclude_patterns = pattern_set.get("exclude", [])
        retention_days = _resolve_retention_days(pattern_set.get("policy"), retention_cfg)
        for candidate in _iter_paths_from_patterns(include_patterns):
            if _is_excluded(candidate, exclude_patterns):
                continue
            if not _passes_retention_policy(candidate, retention_days, now=now):
                continue
            legacy_files[group_name].append(candidate)

    return _normalise_grouped_paths(legacy_files)


def find_empty_directories() -> List[Path]:
    config = _load_cleanup_config()
    details = config.get("directory_cleanup", {})
    scan_roots = details.get("scan_roots", ["data", "logs", "reports"])
    exclude_patterns = details.get("exclude", [])
    validation_rules = details.get("validation_rules", {})
    preserved_dirs = {
        _resolve_repo_path(Path(p)) for p in validation_rules.get("preserve_named_directories", [])
    }

    empty_directories: List[Path] = []
    for root_name in scan_roots:
        root_path = _resolve_repo_path(Path(root_name))
        if not root_path.exists() or not root_path.is_dir():
            continue
        for directory in root_path.rglob("*"):
            if not directory.is_dir():
                continue
            if _is_excluded(directory, exclude_patterns):
                continue
            resolved_dir = directory.resolve()
            if _is_preserved_directory(resolved_dir, preserved_dirs):
                continue
            try:
                if any(resolved_dir.iterdir()):
                    continue
            except OSError as exc:
                logger.debug("Skipping directory %s due to access error: %s", resolved_dir, exc)
                continue
            empty_directories.append(resolved_dir)

    return sorted(empty_directories)


def find_duplicate_configs() -> Dict[str, List[Path]]:
    """Identify duplicate configuration files by filename and checksum."""

    config = _load_cleanup_config()
    details = config.get("configuration_consolidation", {})
    scan_roots = details.get("scan_roots", ["config"])
    include_patterns = details.get(
        "include", ["*.json", "*.yaml", "*.yml", "*.ini", "*.toml"]
    )
    exclude_patterns = details.get("exclude", [])

    by_name: Dict[str, Dict[str, List[Path]]] = defaultdict(lambda: defaultdict(list))

    for root_name in scan_roots:
        root_path = _resolve_repo_path(Path(root_name))
        if not root_path.exists() or not root_path.is_dir():
            continue
        for pattern in include_patterns:
            for file_path in root_path.rglob(pattern):
                if not file_path.is_file():
                    continue
                if _is_excluded(file_path, exclude_patterns):
                    continue
                checksum = get_file_checksum(file_path)
                if checksum is None:
                    continue
                by_name[file_path.name][checksum].append(file_path.resolve())

    duplicates: Dict[str, List[Path]] = {}
    for name, checksum_map in by_name.items():
        for checksum, paths in checksum_map.items():
            if len(paths) > 1:
                key = f"{name}:{checksum[:8]}"
                duplicates[key] = sorted(paths)

    return duplicates


def _rollback_files(backups: Dict[Path, Path]) -> None:
    for original, backup in backups.items():
        if not backup.exists():
            continue
        original.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(backup, original)
        except OSError as exc:
            logger.error("Failed to roll back file %s from %s: %s", original, backup, exc)


def _rollback_directories(backups: Dict[Path, Path]) -> None:
    for original, backup in backups.items():
        if not backup.exists():
            continue
        if original.exists():
            continue
        try:
            shutil.move(str(backup), str(original))
        except OSError as exc:
            logger.error("Failed to restore directory %s from %s: %s", original, backup, exc)


def cleanup_legacy_files(
    file_groups: Dict[str, List[Path]],
    dry_run: bool = True,
) -> Dict[str, int]:
    """Remove legacy files while creating backups and supporting dry-run mode."""

    normalised_groups = _normalise_grouped_paths(file_groups)
    file_backups: Dict[Path, Path] = {}
    result: Dict[str, int] = {}
    groups_cfg, retention_cfg = _resolve_legacy_group_config()
    now = time.time()

    try:
        for group, paths in normalised_groups.items():
            removed_count = 0
            policy_key = groups_cfg.get(group, {}).get("policy")
            retention_days = _resolve_retention_days(policy_key, retention_cfg)
            for file_path in paths:
                if not file_path.exists() or not file_path.is_file():
                    continue
                if not _passes_retention_policy(file_path, retention_days, now=now):
                    logger.debug(
                        "Skipping legacy file %s due to retention policy '%s'",
                        file_path,
                        policy_key or "default",
                    )
                    continue
                if dry_run:
                    logger.debug("[DRY-RUN] Would remove legacy file %s", file_path)
                    removed_count += 1
                    continue

                backup_path = create_timestamped_backup(file_path)
                if backup_path is not None:
                    file_backups[file_path] = backup_path

                try:
                    file_path.unlink()
                    removed_count += 1
                except OSError as exc:
                    logger.error("Failed to remove legacy file %s: %s", file_path, exc)
                    raise

            result[group] = removed_count
    except Exception:
        _rollback_files(file_backups)
        raise

    return result


def cleanup_empty_directories(
    directories: Sequence[Path],
    dry_run: bool = True,
) -> int:
    """Remove empty directories and return the number removed."""

    config = _load_cleanup_config()
    details = config.get("directory_cleanup", {})
    validation_rules = details.get("validation_rules", {})
    preserved_dirs = {
        _resolve_repo_path(Path(p)) for p in validation_rules.get("preserve_named_directories", [])
    }
    require_gitkeep = bool(validation_rules.get("require_gitkeep"))

    handled = 0
    gitkeep_created = 0
    dir_backups: Dict[Path, Path] = {}

    try:
        for directory in directories:
            dir_path = _resolve_repo_path(directory)
            if not dir_path.exists() or not dir_path.is_dir():
                continue
            if _is_preserved_directory(dir_path, preserved_dirs):
                logger.debug("Skipping preserved directory %s", dir_path)
                continue

            try:
                if any(dir_path.iterdir()):
                    continue
            except OSError as exc:
                logger.debug("Skipping directory %s due to access error: %s", dir_path, exc)
                continue

            if dry_run:
                action = "create .gitkeep" if require_gitkeep else "remove"
                logger.debug("[DRY-RUN] Would %s empty directory %s", action, dir_path)
                handled += 1
                continue

            if require_gitkeep:
                gitkeep_path = dir_path / ".gitkeep"
                try:
                    gitkeep_path.write_text("", encoding="utf-8")
                except OSError as exc:
                    logger.error("Failed to create .gitkeep in %s: %s", dir_path, exc)
                    raise
                gitkeep_created += 1
                handled += 1
                continue

            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            backup_dir = dir_path.parent / f"{dir_path.name}.backup_{timestamp}"

            try:
                shutil.copytree(dir_path, backup_dir)
                dir_backups[dir_path] = backup_dir
                shutil.rmtree(dir_path)
                handled += 1
            except OSError as exc:
                logger.error("Failed to remove directory %s: %s", dir_path, exc)
                raise
    except Exception:
        _rollback_directories(dir_backups)
        raise

    if gitkeep_created:
        logger.debug("Created %d .gitkeep placeholder(s) for preserved directories", gitkeep_created)

    return handled


def cleanup_temporary_files(
    max_age_days: int = 7,
    dry_run: bool = True,
) -> int:
    """Remove temporary files older than ``max_age_days`` based on configuration."""

    config = _load_cleanup_config()
    details = config.get("temporary_file_cleanup", {})
    directories = details.get("directories") or [str(path) for path in DEFAULT_TEMP_DIRS]
    raw_patterns = details.get("patterns") or ["*.tmp", "*.log", "*.log.*", "*.session", "*.cache"]
    pattern_entries = _parse_temporary_patterns(raw_patterns)
    if not pattern_entries:
        pattern_entries = _parse_temporary_patterns(["*.tmp", "*.log", "*.log.*", "*.session", "*.cache"])
    exclude_patterns = details.get("exclude", [])
    min_size = int(details.get("size_threshold_bytes", 0))

    cutoff = time.time() - max(0, max_age_days) * 86400
    removed = 0
    file_backups: Dict[Path, Path] = {}

    try:
        for directory_name in directories:
            directory = _resolve_repo_path(Path(directory_name))
            if not directory.exists() or not directory.is_dir():
                continue

            for candidate in directory.rglob("*"):
                if not candidate.is_file():
                    continue

                relative_str = _normalised_relative_path(candidate)
                matched_entry: Optional[Dict[str, Any]] = None
                for entry in pattern_entries:
                    pattern = entry.get("pattern", "").replace("\\", "/")
                    if not pattern:
                        continue
                    if fnmatch.fnmatch(relative_str, pattern) or fnmatch.fnmatch(candidate.name, pattern):
                        matched_entry = entry
                        break

                if matched_entry is None:
                    continue

                if _is_excluded(candidate, exclude_patterns):
                    continue

                try:
                    stats = candidate.stat()
                except OSError as exc:
                    logger.debug("Skipping file %s due to stat error: %s", candidate, exc)
                    continue

                if stats.st_mtime > cutoff:
                    continue

                if min_size and stats.st_size < min_size:
                    continue

                should_backup = bool(matched_entry.get("backup", True))

                if dry_run:
                    action = "remove"
                    if should_backup:
                        action += " (with backup)"
                    else:
                        action += " (no backup)"
                    logger.debug("[DRY-RUN] Would %s temporary file %s", action, candidate)
                    removed += 1
                    continue

                backup_path = None
                if should_backup:
                    backup_path = create_timestamped_backup(candidate)
                    if backup_path is not None:
                        file_backups[candidate] = backup_path

                try:
                    candidate.unlink()
                    removed += 1
                except OSError as exc:
                    logger.error("Failed to remove temporary file %s: %s", candidate, exc)
                    raise
    except Exception:
        _rollback_files(file_backups)
        raise

    return removed


def standardize_naming_conventions(
    dry_run: bool = True,
    *,
    allow_paths: Optional[Sequence[Path]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Rename files to adhere to configured naming conventions."""

    config = _load_cleanup_config()
    naming_details = config.get("naming_conventions", {})
    rules: Dict[str, Any] = naming_details.get("rules", {})

    results: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"attempted": 0, "count": 0, "renames": [], "skipped": []}
    )
    file_backups: Dict[Path, Path] = {}
    reserved_targets: Set[Path] = set()
    allow_set: Optional[Set[Path]] = None
    if allow_paths is not None:
        allow_set = { _resolve_repo_path(Path(p)).resolve() for p in allow_paths }

    try:
        for file_type, rule in rules.items():
            include_patterns = rule.get("include", [])
            exclude_patterns = rule.get("exclude", [])

            if not include_patterns:
                continue

            for candidate in _iter_paths_from_patterns(include_patterns):
                if _is_excluded(candidate, exclude_patterns):
                    continue
                if not candidate.is_file():
                    continue

                resolved_candidate = candidate.resolve()

                if validate_naming_convention(resolved_candidate, file_type):
                    continue

                suggestion = suggest_standard_name(resolved_candidate, file_type)
                if suggestion == resolved_candidate.name:
                    continue

                stats = results[file_type]
                stats["attempted"] += 1

                if dry_run:
                    target_path = _compute_target_path(resolved_candidate, suggestion, reserved_targets, reserve=True)
                    stats["renames"].append(
                        RenameAction(resolved_candidate, target_path.name, target_path)
                    )
                    stats["count"] += 1
                    continue

                if allow_set is not None and resolved_candidate not in allow_set:
                    tentative_target = _compute_target_path(
                        resolved_candidate,
                        suggestion,
                        reserved_targets,
                        reserve=False,
                    )
                    stats["skipped"].append(
                        RenameAction(resolved_candidate, tentative_target.name, tentative_target)
                    )
                    continue

                target_path = _compute_target_path(
                    resolved_candidate,
                    suggestion,
                    reserved_targets,
                    reserve=True,
                )

                backup_path = create_timestamped_backup(resolved_candidate)
                if backup_path is not None:
                    file_backups[resolved_candidate] = backup_path

                try:
                    resolved_candidate.rename(target_path)
                    stats["renames"].append(
                        RenameAction(resolved_candidate, target_path.name, target_path)
                    )
                    stats["count"] += 1
                except OSError as exc:
                    logger.error(
                        "Failed to rename %s to %s: %s",
                        resolved_candidate,
                        target_path,
                        exc,
                    )
                    raise
    except Exception:
        _rollback_files(file_backups)
        raise

    # Remove file types with no activity to keep payload compact
    return {
        file_type: data
        for file_type, data in results.items()
        if data["attempted"] or data["count"] or data["renames"] or data["skipped"]
    }


def _read_config_content(file_path: Path) -> Any:
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Unable to read config file %s: %s", file_path, exc)
        return None

    suffix = file_path.suffix.lower()
    if suffix == ".json":
        with suppress(json.JSONDecodeError):
            return json.loads(text)
    return text


def _write_config_content(file_path: Path, content: Any) -> None:
    if isinstance(content, str):
        file_path.write_text(content, encoding="utf-8")
        return

    file_path.write_text(
        json.dumps(content, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _merge_config_content(base_content: Any, new_content: Any) -> Any:
    if isinstance(base_content, dict) and isinstance(new_content, dict):
        merged: Dict[str, Any] = dict(base_content)
        for key, value in new_content.items():
            if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
                merged[key] = _merge_config_content(merged[key], value)
            else:
                merged[key] = value
        return merged

    if isinstance(base_content, list) and isinstance(new_content, list):
        seen: Set[str] = set()
        merged_list: List[Any] = []
        for item in base_content + new_content:
            if isinstance(item, (dict, list)):
                identifier = json.dumps(item, sort_keys=True, default=str)
            else:
                identifier = str(item)
            if identifier in seen:
                continue
            seen.add(identifier)
            merged_list.append(item)
        return merged_list

    return new_content


def _load_config_file(path: Path) -> ConfigFileInfo:
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Unable to read config file %s: %s", path, exc)
        return ConfigFileInfo(path=path, format=path.suffix.lower(), parsed=None, raw_text=None, mtime=0.0, parse_success=False)

    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0

    suffix = path.suffix.lower()
    format_key = suffix.lstrip(".")

    parsed: Any = None
    parse_success = False

    if suffix == ".json":
        try:
            parsed = json.loads(raw_text)
            parse_success = True
        except json.JSONDecodeError:
            parse_success = False
    elif suffix in {".yaml", ".yml"} and yaml is not None:
        try:
            parsed = yaml.safe_load(raw_text)
            parse_success = True
        except Exception:  # pragma: no cover - depends on yaml contents
            parse_success = False
    elif suffix == ".ini":
        parser = configparser.ConfigParser()
        try:
            parser.read_string(raw_text)
            parsed = {
                section: dict(parser.items(section))
                for section in parser.sections()
            }
            if parser.defaults():
                parsed.setdefault("DEFAULT", dict(parser.defaults()))
            parse_success = True
        except (configparser.Error, ValueError):
            parse_success = False
    elif suffix == ".toml" and tomllib is not None:
        try:
            parsed = tomllib.loads(raw_text)
            parse_success = True
        except (tomllib.TOMLDecodeError, AttributeError):  # pragma: no cover - depends on tomllib
            parse_success = False

    return ConfigFileInfo(
        path=path,
        format=format_key,
        parsed=parsed,
        raw_text=raw_text,
        mtime=mtime,
        parse_success=parse_success,
    )


def _serialise_config_content(config: ConfigFileInfo, content: Any) -> Optional[str]:
    format_key = config.format

    if format_key == "json":
        try:
            return json.dumps(content, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        except (TypeError, ValueError):
            return None

    if format_key in {"yaml", "yml"}:
        if yaml is None:
            return None
        try:
            return yaml.safe_dump(content, sort_keys=True)
        except Exception:  # pragma: no cover - depends on yaml dumper
            return None

    if format_key == "ini":
        if not isinstance(content, dict):
            return None
        parser = configparser.ConfigParser()
        defaults = content.get("DEFAULT")
        if isinstance(defaults, dict):
            parser.read_dict({"DEFAULT": defaults})
        for section, values in content.items():
            if section == "DEFAULT":
                continue
            if not isinstance(values, dict):
                continue
            parser[section] = {str(k): str(v) for k, v in values.items()}
        stream = io.StringIO()
        parser.write(stream)
        return stream.getvalue()

    if format_key == "toml":
        # Serialisation requires external dependency; fall back to raw content.
        return None

    if isinstance(content, str):
        return content

    return config.raw_text


def consolidate_duplicate_configs(
    duplicates: Dict[str, List[Path]],
    dry_run: bool = True,
) -> int:
    """Merge duplicate configuration files and remove redundant copies."""

    config = _load_cleanup_config()
    details = config.get("configuration_consolidation", {})
    merge_strategy = str(details.get("merge_strategy", "deep")).lower()
    canonical_preferences: Sequence[str] = details.get("canonical_preference", [])
    validation_rules = details.get("validation_rules", {})
    ensure_valid_json = bool(validation_rules.get("ensure_valid_json"))
    preserve_comments = bool(validation_rules.get("preserve_comments"))

    if preserve_comments:
        logger.warning(
            "configuration_consolidation.validation_rules.preserve_comments is enabled, "
            "but comment preservation is not supported; proceeding with best-effort consolidation."
        )

    merged = 0
    file_backups: Dict[Path, Path] = {}

    try:
        for _, paths in duplicates.items():
            if len(paths) < 2:
                continue

            resolved_paths = [_resolve_repo_path(path) for path in paths]
            canonical_path = _select_canonical_config_path(resolved_paths, canonical_preferences)
            other_paths = [path for path in resolved_paths if path != canonical_path]

            canonical_info = _load_config_file(canonical_path)
            other_infos = [_load_config_file(path) for path in other_paths]

            if canonical_info.raw_text is None and not canonical_info.parse_success:
                logger.debug(
                    "Skipping consolidation for %s due to unreadable content",
                    canonical_path,
                )
                continue

            if dry_run:
                logger.debug(
                    "[DRY-RUN] Would consolidate %d duplicate configs into %s using strategy '%s'",
                    len(other_paths),
                    canonical_path,
                    merge_strategy,
                )
                merged += len(other_paths)
                continue

            effective_strategy = merge_strategy
            if effective_strategy == "deep":
                if not canonical_info.parse_success or not all(info.parse_success for info in other_infos):
                    logger.warning(
                        "Deep merge unavailable for %s; falling back to prefer-canonical",
                        canonical_path,
                    )
                    effective_strategy = "prefer-canonical"

            content_to_write: Any = canonical_info.parsed
            write_required = False

            if effective_strategy == "deep":
                combined = canonical_info.parsed
                for info in other_infos:
                    combined = _merge_config_content(combined, info.parsed)
                content_to_write = combined
                write_required = True
            elif effective_strategy == "prefer-newer":
                candidates = [info for info in [canonical_info] + other_infos if info.parse_success or info.raw_text is not None]
                if not candidates:
                    logger.warning(
                        "No readable configs found for prefer-newer strategy at %s; skipping",
                        canonical_path,
                    )
                    continue
                newest = max(candidates, key=lambda info: info.mtime)
                if newest.path != canonical_info.path:
                    if newest.parse_success and canonical_info.parse_success:
                        content_to_write = newest.parsed
                    else:
                        content_to_write = newest.raw_text
                    write_required = True
            else:  # prefer-canonical
                write_required = False

            serialized_content: Optional[str] = None
            if write_required:
                if isinstance(content_to_write, str):
                    serialized_content = content_to_write
                else:
                    serialized_content = _serialise_config_content(canonical_info, content_to_write)

                if serialized_content is None:
                    logger.warning(
                        "Unable to serialise merged content for %s; skipping consolidation",
                        canonical_path,
                    )
                    continue

                if ensure_valid_json and canonical_info.format == "json":
                    try:
                        json.loads(serialized_content)
                    except json.JSONDecodeError:
                        logger.warning(
                            "Skipping consolidation for %s due to invalid JSON output",
                            canonical_path,
                        )
                        continue

                backup_path = create_timestamped_backup(canonical_path)
                if backup_path is not None:
                    file_backups[canonical_path] = backup_path

                try:
                    canonical_path.write_text(serialized_content, encoding="utf-8")
                except OSError as exc:
                    logger.error("Failed to write merged config %s: %s", canonical_path, exc)
                    raise

            for info in other_infos:
                if not info.path.exists():
                    continue
                backup = create_timestamped_backup(info.path)
                if backup is not None:
                    file_backups[info.path] = backup
                try:
                    info.path.unlink()
                    merged += 1
                except OSError as exc:
                    logger.error("Failed to remove duplicate config %s: %s", info.path, exc)
                    raise
    except Exception:
        _rollback_files(file_backups)
        raise

    return merged
