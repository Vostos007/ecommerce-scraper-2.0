"""Utilities for managing file backups, versioning, and safe writes."""

from __future__ import annotations

import gzip
import hashlib
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from utils import data_paths

logger = logging.getLogger(__name__)


@dataclass
class FileVersion:
    """Metadata describing a single backup version of a file."""

    path: Path
    timestamp: datetime
    checksum: Optional[str]
    size: int
    backup_path: Path


@dataclass
class BackupConfig:
    """Configuration for backup and restore operations."""

    max_count: int = data_paths.MAX_BACKUPS
    max_age_days: int = data_paths.BACKUP_RETENTION_DAYS
    checksum_algorithm: str = data_paths.DEFAULT_CHECKSUM_ALGORITHM
    compression_age_days: int = 7
    enable_integrity_checks: bool = True
    max_total_storage_mb: Optional[int] = None
    backup_dir: Optional[Path] = None


def _compute_checksum(path: Path, algorithm: str) -> Optional[str]:
    try:
        hasher = hashlib.new(algorithm)
    except ValueError:
        logger.warning("Unsupported checksum algorithm %s, falling back to sha256", algorithm)
        hasher = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(8192), b""):
                hasher.update(chunk)
        return hasher.hexdigest()
    except OSError as exc:
        logger.warning("Unable to compute checksum for %s: %s", path, exc)
        return None


def create_backup(source_path: Path, backup_dir: Optional[Path] = None, *, algorithm: str = data_paths.DEFAULT_CHECKSUM_ALGORITHM) -> Optional[FileVersion]:
    """Create a timestamped backup of ``source_path`` and return metadata."""

    if not source_path.exists() or not source_path.is_file():
        logger.debug("Skipped backup because %s does not exist", source_path)
        return None

    if backup_dir is None:
        backup_dir = data_paths.resolve_backup_dir(source_path)
    backup_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    stem = source_path.stem
    suffix = source_path.suffix
    backup_name = f"{stem}_{timestamp}{suffix}.bak"
    backup_path = backup_dir / backup_name
    shutil.copy2(source_path, backup_path)

    checksum = _compute_checksum(backup_path, algorithm)
    size = backup_path.stat().st_size

    return FileVersion(
        path=source_path,
        timestamp=datetime.now(timezone.utc),
        checksum=checksum,
        size=size,
        backup_path=backup_path,
    )


def restore_from_backup(backup_path: Path, target_path: Path) -> bool:
    """Restore ``target_path`` from ``backup_path``."""

    if not backup_path.exists():
        logger.error("Backup %s does not exist", backup_path)
        return False
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup_path, target_path)
        return True
    except OSError as exc:
        logger.error("Failed to restore %s from %s: %s", target_path, backup_path, exc)
        return False


def list_versions(file_path: Path) -> List[FileVersion]:
    """Return metadata for all backups associated with ``file_path``."""

    backups = data_paths.get_backup_files(file_path)
    versions: List[FileVersion] = []
    for backup in backups:
        checksum = _compute_checksum(backup, data_paths.DEFAULT_CHECKSUM_ALGORITHM)
        size = backup.stat().st_size if backup.exists() else 0
        timestamp = datetime.fromtimestamp(backup.stat().st_mtime, tz=timezone.utc)
        versions.append(
            FileVersion(
                path=file_path,
                timestamp=timestamp,
                checksum=checksum,
                size=size,
                backup_path=backup,
            )
        )
    return versions


def cleanup_old_backups(file_path: Path, *, max_count: int = data_paths.MAX_BACKUPS, max_age_days: int = data_paths.BACKUP_RETENTION_DAYS) -> None:
    """Remove old backups based on count and age constraints."""

    backups = list_versions(file_path)
    backups.sort(key=lambda version: version.timestamp, reverse=True)

    for version in backups[max_count:]:
        try:
            version.backup_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Failed to remove backup %s: %s", version.backup_path, exc)

    if max_age_days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        for version in backups:
            if version.timestamp < cutoff:
                try:
                    version.backup_path.unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning("Failed to remove aged backup %s: %s", version.backup_path, exc)


def get_backup_size(file_path: Path) -> int:
    """Return the total size in bytes consumed by backups for ``file_path``."""

    return sum(version.size for version in list_versions(file_path))


def compress_old_backups(file_path: Path, age_threshold_days: int = 7) -> int:
    """Compress backups older than ``age_threshold_days`` using gzip."""

    compressed = 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=age_threshold_days)
    for version in list_versions(file_path):
        if version.timestamp >= cutoff or version.backup_path.suffix == ".gz":
            continue
        try:
            gz_path = version.backup_path.with_suffix(version.backup_path.suffix + ".gz")
            if gz_path.exists():
                continue
            with version.backup_path.open("rb") as src, gzip.open(gz_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
            version.backup_path.unlink(missing_ok=True)
            compressed += 1
        except OSError as exc:
            logger.warning("Failed to compress backup %s: %s", version.backup_path, exc)
    return compressed


def files_identical(path1: Path, path2: Path, *, algorithm: str = data_paths.DEFAULT_CHECKSUM_ALGORITHM) -> bool:
    checksum1 = _compute_checksum(path1, algorithm) if path1.exists() else None
    checksum2 = _compute_checksum(path2, algorithm) if path2.exists() else None
    return checksum1 == checksum2 and checksum1 is not None


def calculate_checksum(file_path: Path, algorithm: str = data_paths.DEFAULT_CHECKSUM_ALGORITHM) -> Optional[str]:
    return _compute_checksum(file_path, algorithm)


def detect_changes(original: Path, updated: Path, *, algorithm: str = data_paths.DEFAULT_CHECKSUM_ALGORITHM) -> Dict[str, Any]:
    """Return a summary describing whether two files differ."""

    result = {
        "original_exists": original.exists(),
        "updated_exists": updated.exists(),
        "changed": False,
        "original_checksum": None,
        "updated_checksum": None,
    }

    if original.exists():
        result["original_checksum"] = _compute_checksum(original, algorithm)
    if updated.exists():
        result["updated_checksum"] = _compute_checksum(updated, algorithm)

    if result["original_checksum"] and result["updated_checksum"]:
        result["changed"] = result["original_checksum"] != result["updated_checksum"]
    else:
        result["changed"] = original.exists() != updated.exists()

    return result


def validate_backup_integrity(backup_path: Path, *, algorithm: str = data_paths.DEFAULT_CHECKSUM_ALGORITHM) -> bool:
    """Basic integrity check ensuring a backup can be read and hashed."""

    if not backup_path.exists():
        return False
    checksum = _compute_checksum(backup_path, algorithm)
    return checksum is not None


def repair_backup_index(backup_dir: Path) -> int:
    """Ensure backup directory metadata is consistent by removing broken symlinks."""

    if not backup_dir.exists() or not backup_dir.is_dir():
        return 0

    repaired = 0
    for path in backup_dir.glob("*.bak*"):
        if not path.exists():
            try:
                path.unlink(missing_ok=True)
                repaired += 1
            except OSError as exc:
                logger.warning("Failed to repair backup entry %s: %s", path, exc)
    return repaired


def atomic_update(file_path: Path, new_content: str, *, create_backup_file: bool = True, backup_dir: Optional[Path] = None, backup_config: Optional[BackupConfig] = None) -> bool:
    """Atomically update ``file_path`` with ``new_content`` and optional backup."""

    backup_config = backup_config or BackupConfig()

    if create_backup_file and file_path.exists():
        create_backup(file_path, backup_dir or backup_config.backup_dir, algorithm=backup_config.checksum_algorithm)

    temp_fd, temp_path_str = tempfile.mkstemp(dir=str(file_path.parent), suffix=".tmp")
    temp_path = Path(temp_path_str)
    try:
        with os.fdopen(temp_fd, "w", encoding="utf-8") as handle:
            handle.write(new_content)
        temp_path.replace(file_path)
        return True
    except OSError as exc:
        logger.error("Failed atomic update for %s: %s", file_path, exc)
        temp_path.unlink(missing_ok=True)
        return False


def safe_overwrite(file_path: Path, new_content: str, backup_config: Optional[BackupConfig] = None) -> Optional[FileVersion]:
    """Safely overwrite ``file_path`` with ``new_content`` using backups and atomic swap."""

    backup_config = backup_config or BackupConfig()
    backup_version: Optional[FileVersion] = None

    if backup_config.max_total_storage_mb:
        current_size_mb = get_backup_size(file_path) / (1024 * 1024)
        if current_size_mb > backup_config.max_total_storage_mb:
            logger.warning(
                "Backup storage for %s exceeds limit (%.2f MB)",
                file_path,
                current_size_mb,
            )

    if backup_config.enable_integrity_checks and file_path.exists():
        backup_version = create_backup(file_path, backup_config.backup_dir, algorithm=backup_config.checksum_algorithm)

    temp_file = file_path.with_suffix(file_path.suffix + ".tmp")
    try:
        with temp_file.open("w", encoding="utf-8") as handle:
            handle.write(new_content)
        temp_file.replace(file_path)
    except OSError as exc:
        logger.error("Failed to overwrite %s: %s", file_path, exc)
        temp_file.unlink(missing_ok=True)
        if backup_version and backup_version.backup_path.exists():
            restore_from_backup(backup_version.backup_path, file_path)
        return None

    cleanup_old_backups(
        file_path,
        max_count=backup_config.max_count,
        max_age_days=backup_config.max_age_days,
    )

    if backup_version:
        compress_old_backups(file_path, backup_config.compression_age_days)

    return backup_version
