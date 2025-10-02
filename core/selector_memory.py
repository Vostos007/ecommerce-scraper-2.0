"""
Domain-Specific Selector Memory Management System

This module implements a comprehensive selector memory system that manages
domain-specific CSS selectors with persistent storage, backup functionality,
and efficient indexing for fast lookups.

Features:
- Persistent JSON-based storage with backup and recovery
- Domain-specific selector management with confidence scoring
- Efficient indexing for fast domain-based lookups
- In-memory caching with lazy loading
- Automatic cleanup and maintenance
- Integration hooks for ProductParser and SitemapAnalyzer
- Comprehensive error handling and graceful degradation
"""

import json
import logging
import time
import threading
import hashlib
from typing import Any, Dict, List, Optional, Set, Tuple, TYPE_CHECKING
from dataclasses import dataclass, field, asdict
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import gzip

if TYPE_CHECKING:  # pragma: no cover
    from database.manager import DatabaseManager


@dataclass
class SelectorMetadata:
    """Enhanced metadata for selector performance tracking."""

    selector: str
    field: str
    domain: str

    # Performance metrics
    success_count: int = 0
    failure_count: int = 0
    total_attempts: int = 0
    avg_extraction_time: float = 0.0

    # Confidence and scoring
    confidence_score: float = 0.5
    reliability_score: float = 0.0

    # Temporal data
    first_discovered: float = field(default_factory=lambda: time.time())
    last_used: float = 0.0
    last_success: float = 0.0
    last_failure: float = 0.0

    # Source information
    source: str = "learned"  # learned, config, manual, cms
    cms_type: Optional[str] = None

    # Usage statistics
    usage_count: int = 0
    consecutive_successes: int = 0
    consecutive_failures: int = 0

    # Validation
    is_validated: bool = False
    validation_date: Optional[float] = None

    @property
    def success_rate(self) -> float:
        """Calculate success rate."""
        return (
            self.success_count / self.total_attempts if self.total_attempts > 0 else 0.0
        )

    @property
    def is_reliable(self) -> bool:
        """Check if selector is considered reliable."""
        return (
            self.success_rate >= 0.8
            and self.total_attempts >= 5
            and self.consecutive_successes >= 3
        )

    @property
    def is_stale(self) -> bool:
        """Check if selector might be stale."""
        days_since_last_use = (time.time() - self.last_used) / (24 * 3600)
        return days_since_last_use > 30 and self.success_rate < 0.7

    def update_performance(self, success: bool, extraction_time: float = 0.0) -> None:
        """Update performance metrics after an attempt."""
        self.total_attempts += 1
        self.last_used = time.time()
        self.usage_count += 1

        if success:
            self.success_count += 1
            self.last_success = time.time()
            self.consecutive_successes += 1
            self.consecutive_failures = 0
        else:
            self.failure_count += 1
            self.last_failure = time.time()
            self.consecutive_failures += 1
            self.consecutive_successes = 0

        # Update average extraction time
        if extraction_time > 0:
            if self.avg_extraction_time == 0:
                self.avg_extraction_time = extraction_time
            else:
                self.avg_extraction_time = (
                    self.avg_extraction_time + extraction_time
                ) / 2

        # Update confidence score
        self._update_confidence_score()

        # Update reliability score
        self._update_reliability_score()

    def _update_confidence_score(self) -> None:
        """Update confidence score based on multiple factors."""
        base_confidence = self.success_rate

        # Recency factor (newer selectors get slight boost)
        recency_factor = min(
            1.0, (time.time() - self.first_discovered) / (30 * 24 * 3600)
        )

        # Consecutive success factor
        consecutive_factor = min(1.0, self.consecutive_successes / 10.0)

        # Usage factor (more used = more confident)
        usage_factor = min(1.0, self.usage_count / 100.0)

        self.confidence_score = (
            base_confidence * 0.6
            + consecutive_factor * 0.2
            + usage_factor * 0.1
            + recency_factor * 0.1
        )

    def _update_reliability_score(self) -> None:
        """Update reliability score based on consistency."""
        if self.total_attempts < 5:
            self.reliability_score = 0.0
            return

        # Consistency factor
        consistency = 1.0 - (
            abs(self.consecutive_successes - self.consecutive_failures)
            / self.total_attempts
        )

        # Volume factor
        volume_factor = min(1.0, self.total_attempts / 50.0)

        self.reliability_score = (
            self.success_rate * 0.7 + consistency * 0.2 + volume_factor * 0.1
        )


@dataclass
class DomainSelectorStore:
    """Storage container for domain-specific selectors."""

    domain: str
    selectors: Dict[str, List[SelectorMetadata]] = field(
        default_factory=lambda: defaultdict(list)
    )
    static_selectors: Dict[str, List[str]] = field(default_factory=dict)
    cms_selectors: Dict[str, List[str]] = field(default_factory=dict)

    # Metadata
    last_updated: float = field(default_factory=time.time)
    total_learning_sessions: int = 0
    cms_type: Optional[str] = None
    cms_confidence: float = 0.0

    # Statistics
    total_selectors: int = 0
    reliable_selectors: int = 0
    stale_selectors: int = 0

    def get_best_selectors(self, field: str, limit: int = 5) -> List[str]:
        """Get best performing selectors for a field."""
        field_selectors = self.selectors.get(field, [])
        if not field_selectors:
            return []

        # Sort by confidence, then reliability, then recency
        sorted_selectors = sorted(
            field_selectors,
            key=lambda s: (s.confidence_score, s.reliability_score, -s.last_used),
            reverse=True,
        )

        return [s.selector for s in sorted_selectors[:limit]]

    def add_selector(
        self,
        field: str,
        selector: str,
        source: str = "learned",
        cms_type: Optional[str] = None,
    ) -> SelectorMetadata:
        """Add or update a selector."""
        field_selectors = self.selectors[field]

        # Check if selector already exists
        for metadata in field_selectors:
            if metadata.selector == selector:
                return metadata

        # Create new metadata
        metadata = SelectorMetadata(
            selector=selector,
            field=field,
            domain=self.domain,
            source=source,
            cms_type=cms_type,
            first_discovered=time.time(),
        )

        field_selectors.append(metadata)
        self.total_selectors += 1
        self.last_updated = time.time()

        return metadata

    def update_selector_performance(
        self, field: str, selector: str, success: bool, extraction_time: float = 0.0
    ) -> None:
        """Update performance for a selector."""
        field_selectors = self.selectors.get(field, [])
        for metadata in field_selectors:
            if metadata.selector == selector:
                metadata.update_performance(success, extraction_time)
                self.last_updated = time.time()
                break

    def cleanup_stale_selectors(self, max_age_days: int = 90) -> int:
        """Remove stale selectors."""
        cutoff_time = time.time() - (max_age_days * 24 * 3600)
        removed_count = 0

        for field_name, field_selectors in self.selectors.items():
            original_count = len(field_selectors)
            self.selectors[field_name] = [
                metadata
                for metadata in field_selectors
                if not metadata.is_stale or metadata.last_used > cutoff_time
            ]
            removed_count += original_count - len(self.selectors[field_name])

        self.total_selectors -= removed_count
        return removed_count

    def get_statistics(self) -> Dict[str, Any]:
        """Get comprehensive statistics."""
        stats = {
            "domain": self.domain,
            "total_selectors": self.total_selectors,
            "reliable_selectors": sum(
                1
                for field_selectors in self.selectors.values()
                for metadata in field_selectors
                if metadata.is_reliable
            ),
            "stale_selectors": sum(
                1
                for field_selectors in self.selectors.values()
                for metadata in field_selectors
                if metadata.is_stale
            ),
            "fields": {},
            "last_updated": self.last_updated,
            "cms_type": self.cms_type,
            "cms_confidence": self.cms_confidence,
        }

        for field_name, field_selectors in self.selectors.items():
            field_stats = {
                "count": len(field_selectors),
                "reliable": sum(1 for m in field_selectors if m.is_reliable),
                "avg_confidence": (
                    sum(m.confidence_score for m in field_selectors)
                    / len(field_selectors)
                    if field_selectors
                    else 0.0
                ),
                "avg_success_rate": (
                    sum(m.success_rate for m in field_selectors) / len(field_selectors)
                    if field_selectors
                    else 0.0
                ),
            }
            stats["fields"][field_name] = field_stats

        return stats


class SelectorIndex:
    """Efficient indexing system for fast selector lookups."""

    def __init__(self):
        self.domain_index: Dict[str, Set[str]] = defaultdict(set)
        self.field_index: Dict[str, Set[str]] = defaultdict(set)
        self.selector_hash_index: Dict[str, str] = {}
        self.cms_index: Dict[str, Set[str]] = defaultdict(set)

    def add_selector(
        self, domain: str, field: str, selector: str, cms_type: Optional[str] = None
    ) -> None:
        """Add selector to indexes."""
        selector_hash = hashlib.md5(f"{domain}:{field}:{selector}".encode()).hexdigest()

        self.domain_index[domain].add(selector_hash)
        self.field_index[field].add(selector_hash)
        self.selector_hash_index[selector_hash] = f"{domain}:{field}:{selector}"

        if cms_type:
            self.cms_index[cms_type].add(selector_hash)

    def remove_selector(self, domain: str, field: str, selector: str) -> None:
        """Remove selector from indexes."""
        selector_hash = hashlib.md5(f"{domain}:{field}:{selector}".encode()).hexdigest()

        self.domain_index[domain].discard(selector_hash)
        self.field_index[field].discard(selector_hash)
        if selector_hash in self.selector_hash_index:
            del self.selector_hash_index[selector_hash]

        # Remove from CMS index if present
        for cms_type, hashes in self.cms_index.items():
            hashes.discard(selector_hash)

    def get_domain_selectors(self, domain: str) -> Set[str]:
        """Get all selector hashes for a domain."""
        return self.domain_index.get(domain, set())

    def get_field_selectors(self, field: str) -> Set[str]:
        """Get all selector hashes for a field."""
        return self.field_index.get(field, set())

    def get_cms_selectors(self, cms_type: str) -> Set[str]:
        """Get all selector hashes for a CMS type."""
        return self.cms_index.get(cms_type, set())

    def rebuild_index(self, stores: Dict[str, DomainSelectorStore]) -> None:
        """Rebuild index from domain stores."""
        self.clear()

        for domain, store in stores.items():
            for field_name, field_selectors in store.selectors.items():
                for metadata in field_selectors:
                    self.add_selector(
                        domain, field_name, metadata.selector, metadata.cms_type
                    )

    def clear(self) -> None:
        """Clear all indexes."""
        self.domain_index.clear()
        self.field_index.clear()
        self.selector_hash_index.clear()
        self.cms_index.clear()


class BackupManager:
    """Backup and recovery management system."""

    def __init__(self, memory_dir: Path, max_backups: int = 10):
        self.memory_dir = memory_dir
        self.backup_dir = memory_dir / "backups"
        self.backup_dir.mkdir(exist_ok=True)
        self.max_backups = max_backups

    def create_backup(self, stores: Dict[str, DomainSelectorStore]) -> str:
        """Create a backup of all domain stores."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_file = self.backup_dir / f"selector_memory_{timestamp}.json.gz"

        try:
            # Convert stores to serializable format
            backup_data = {"timestamp": time.time(), "version": "1.0", "stores": {}}

            for domain, store in stores.items():
                store_data = {
                    "domain": store.domain,
                    "selectors": {},
                    "static_selectors": store.static_selectors,
                    "cms_selectors": store.cms_selectors,
                    "last_updated": store.last_updated,
                    "total_learning_sessions": store.total_learning_sessions,
                    "cms_type": store.cms_type,
                    "cms_confidence": store.cms_confidence,
                }

                for field_name, field_selectors in store.selectors.items():
                    store_data["selectors"][field_name] = [
                        asdict(metadata) for metadata in field_selectors
                    ]

                backup_data["stores"][domain] = store_data

            # Write compressed backup
            with gzip.open(backup_file, "wt", encoding="utf-8") as f:
                json.dump(backup_data, f, indent=2, ensure_ascii=False)

            # Cleanup old backups
            self._cleanup_old_backups()

            return str(backup_file)

        except Exception as e:
            logging.error(f"Failed to create backup: {e}")
            return ""

    def restore_backup(self, backup_file: str) -> Dict[str, DomainSelectorStore]:
        """Restore stores from backup."""
        try:
            with gzip.open(backup_file, "rt", encoding="utf-8") as f:
                backup_data = json.load(f)

            stores = {}

            for domain, store_data in backup_data.get("stores", {}).items():
                store = DomainSelectorStore(
                    domain=domain,
                    last_updated=store_data.get("last_updated", time.time()),
                    total_learning_sessions=store_data.get(
                        "total_learning_sessions", 0
                    ),
                    cms_type=store_data.get("cms_type"),
                    cms_confidence=store_data.get("cms_confidence", 0.0),
                )

                store.static_selectors = store_data.get("static_selectors", {})
                store.cms_selectors = store_data.get("cms_selectors", {})

                # Restore selector metadata
                for field, field_data in store_data.get("selectors", {}).items():
                    for metadata_dict in field_data:
                        metadata = SelectorMetadata(**metadata_dict)
                        store.selectors[field].append(metadata)
                        store.total_selectors += 1

                stores[domain] = store

            return stores

        except Exception as e:
            logging.error(f"Failed to restore backup {backup_file}: {e}")
            return {}

    def list_backups(self) -> List[str]:
        """List available backups."""
        return [str(f) for f in self.backup_dir.glob("*.json.gz")]

    def _cleanup_old_backups(self) -> None:
        """Remove old backups beyond max_backups limit."""
        backup_files = sorted(
            self.backup_dir.glob("*.json.gz"),
            key=lambda x: x.stat().st_mtime,
            reverse=True,
        )

        if len(backup_files) > self.max_backups:
            for old_backup in backup_files[self.max_backups :]:
                try:
                    old_backup.unlink()
                except Exception as e:
                    logging.warning(f"Failed to remove old backup {old_backup}: {e}")


class MemoryManager:
    """Memory management with caching and lazy loading."""

    def __init__(self, cache_size: int = 100):
        self.cache_size = cache_size
        self._store_cache: Dict[str, DomainSelectorStore] = {}
        self._html_cache: Dict[str, Any] = {}
        self._lock = threading.RLock()

        # Initialize LRU caches
        self._init_caches()

    def _init_caches(self) -> None:
        """Initialize LRU caches."""
        self.get_store = lru_cache(maxsize=self.cache_size)(self._get_store_uncached)
        self._analyze_html = lru_cache(maxsize=self.cache_size)(
            self._analyze_html_uncached
        )

    def _get_store_uncached(self, domain: str) -> Optional[DomainSelectorStore]:
        """Get store without caching."""
        return self._store_cache.get(domain)

    def cache_store(self, store: DomainSelectorStore) -> None:
        """Cache a domain store."""
        with self._lock:
            self._store_cache[store.domain] = store
            # Clear LRU cache to force refresh
            self.get_store.cache_clear()

    def invalidate_cache(self, domain: Optional[str] = None) -> None:
        """Invalidate cache for domain or all domains."""
        with self._lock:
            if domain:
                self._store_cache.pop(domain, None)
            else:
                self._store_cache.clear()
            self.get_store.cache_clear()

    def get_cache_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        return {
            "cached_stores": len(self._store_cache),
            "cache_size_limit": self.cache_size,
            "html_cache_size": len(self._html_cache),
        }

    def _analyze_html_uncached(self, html: str) -> Dict[str, Any]:
        """Analyze HTML structure without caching."""
        # Placeholder for HTML analysis
        return {}


class SelectorMemory:
    """
    Comprehensive domain-specific selector memory management system.

    Provides persistent storage, backup functionality, efficient indexing,
    and integration hooks for web scraping components.
    """

    def __init__(
        self,
        memory_dir: str = "data/selector_memory",
        cache_size: int = 100,
        database_manager: Optional["DatabaseManager"] = None,
    ):
        """
        Initialize SelectorMemory.

        Args:
            memory_dir: Directory for storing selector data
            cache_size: Size of in-memory cache
        """
        self.memory_dir = Path(memory_dir)
        self.memory_dir.mkdir(parents=True, exist_ok=True)

        self.logger = logging.getLogger(__name__)

        # Core components
        self.index = SelectorIndex()
        self.backup_manager = BackupManager(self.memory_dir)
        self.memory_manager = MemoryManager(cache_size)

        # Optional database integration
        self.database_manager = database_manager
        self._database_sync_enabled = database_manager is not None

        # Domain stores
        self.stores: Dict[str, DomainSelectorStore] = {}

        # Threading and async
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="selector_memory"
        )

        # Load existing data
        self._load_all_stores()

        # Integration hooks
        self._integration_hooks = {}

    def _load_all_stores(self) -> None:
        """Load all domain stores from disk and synchronize with database if available."""
        file_domains: Set[str] = set()
        for store_file in self.memory_dir.glob("*.json"):
            if store_file.name != "backups":  # Skip backup directory
                domain = store_file.stem
                file_domains.add(domain)
                try:
                    store = self._load_domain_store(domain)
                    if store:
                        self.stores[domain] = store
                        self.memory_manager.cache_store(store)
                except Exception as e:
                    self.logger.warning(f"Failed to load store for {domain}: {e}")

        db_domains: Set[str] = set()
        if self.database_manager:
            try:
                db_domains = {
                    self.database_manager._normalize_domain(domain)
                    for domain in self.database_manager.get_all_site_domains()
                }
            except Exception as exc:  # pragma: no cover - defensive logging
                self.logger.debug(f"Database selector load failed: {exc}")

        for domain in file_domains | db_domains:
            self._merge_domain_sources(domain)

        # Rebuild index
        self.index.rebuild_index(self.stores)

    def _load_domain_store(self, domain: str) -> Optional[DomainSelectorStore]:
        """Load a single domain store from disk."""
        store_file = self.memory_dir / f"{domain}.json"

        if not store_file.exists():
            return None

        try:
            with open(store_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            store = DomainSelectorStore(
                domain=domain,
                last_updated=data.get("last_updated", time.time()),
                total_learning_sessions=data.get("total_learning_sessions", 0),
                cms_type=data.get("cms_type"),
                cms_confidence=data.get("cms_confidence", 0.0),
            )

            store.static_selectors = data.get("static_selectors", {})
            store.cms_selectors = data.get("cms_selectors", {})

            # Load selector metadata
            for field, field_data in data.get("selectors", {}).items():
                for metadata_dict in field_data:
                    # Convert ISO last_used back to timestamp
                    if "last_used" in metadata_dict and metadata_dict["last_used"]:
                        try:
                            if isinstance(metadata_dict["last_used"], str):
                                metadata_dict["last_used"] = datetime.fromisoformat(
                                    metadata_dict["last_used"]
                                ).timestamp()
                            elif isinstance(metadata_dict["last_used"], (int, float)):
                                metadata_dict["last_used"] = float(
                                    metadata_dict["last_used"]
                                )
                        except (ValueError, TypeError):
                            metadata_dict["last_used"] = time.time()

                    metadata = SelectorMetadata(**metadata_dict)
                    store.selectors[field].append(metadata)
                    store.total_selectors += 1

            return store

        except Exception as e:
            self.logger.error(f"Failed to load domain store {domain}: {e}")
            return None

    def _save_domain_store(self, store: DomainSelectorStore) -> None:
        """Save a domain store to disk."""
        store_file = self.memory_dir / f"{store.domain}.json"

        try:
            data = {
                "domain": store.domain,
                "selectors": {},
                "static_selectors": store.static_selectors,
                "cms_selectors": store.cms_selectors,
                "last_updated": store.last_updated,
                "total_learning_sessions": store.total_learning_sessions,
                "cms_type": store.cms_type,
                "cms_confidence": store.cms_confidence,
            }

            selectors_payload: Dict[str, List[Dict[str, Any]]] = {}

            # Convert selector metadata to dict
            for field_name, field_selectors in store.selectors.items():
                serialized = [asdict(metadata) for metadata in field_selectors]
                data["selectors"][field_name] = serialized
                selectors_payload[field_name] = [
                    {
                        "selector": metadata.selector,
                        "confidence_score": metadata.confidence_score,
                        "success_count": metadata.success_count,
                        "failure_count": metadata.failure_count,
                        "last_used": (
                            datetime.fromtimestamp(metadata.last_used, UTC).isoformat()
                            if metadata.last_used > 0
                            else None
                        ),
                        "source": metadata.source,
                    }
                    for metadata in field_selectors
                ]

            with open(store_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

            if self._database_sync_enabled:
                self.database_manager.save_site_selectors(
                    store.domain, selectors_payload
                )

        except Exception as e:
            self.logger.error(f"Failed to save domain store {store.domain}: {e}")

    def _merge_domain_sources(self, domain: str) -> Optional[Dict[str, Any]]:
        """Merge domain sources and return reconciliation report."""
        file_store = self.stores.get(domain)
        db_store = self.load_from_database(domain) if self.database_manager else None

        if file_store is None and db_store is None:
            return None

        if file_store is None and db_store is not None:
            self.stores[domain] = db_store
            self.memory_manager.cache_store(db_store)
            self._save_domain_store(db_store)
            return {
                "action": "db_to_file",
                "selectors_synced": db_store.total_selectors,
                "file_only": 0,
                "db_only": db_store.total_selectors,
                "merged": 0,
            }

        if file_store is not None and db_store is None:
            if self.database_manager:
                self.sync_to_database(file_store)
            return {
                "action": "file_to_db",
                "selectors_synced": file_store.total_selectors,
                "file_only": file_store.total_selectors,
                "db_only": 0,
                "merged": 0,
            }

        if file_store is None or db_store is None:
            return None

        merged_store, report = self._merge_selector_metadata(
            domain, file_store, db_store
        )
        self.stores[domain] = merged_store
        self.memory_manager.cache_store(merged_store)
        self._save_domain_store(merged_store)
        if self.database_manager:
            self.sync_to_database(merged_store)

        # Add action info to report
        extended_report = dict(report)
        extended_report["action"] = "merged"
        extended_report["selectors_synced"] = merged_store.total_selectors

        self.logger.info(
            "Selector memory reconciled for %s: file_only=%d db_only=%d merged=%d conflicts=%d",
            domain,
            report["file_only"],
            report["db_only"],
            report["merged"],
            report.get("total_conflicts", 0),
        )

        return report

    def _merge_selector_metadata(
        self,
        domain: str,
        file_store: DomainSelectorStore,
        db_store: DomainSelectorStore,
    ) -> Tuple[DomainSelectorStore, Dict[str, Any]]:
        """Enhanced merge with detailed reconciliation reporting."""
        report = {
            "file_only": 0,
            "db_only": 0,
            "merged": 0,
            "file_newer": 0,
            "db_newer": 0,
            "file_higher_confidence": 0,
            "db_higher_confidence": 0,
            "file_more_reliable": 0,
            "db_more_reliable": 0,
            "file_more_used": 0,
            "db_more_used": 0,
            "file_tiebreaker": 0,
            "total_conflicts": 0,
        }

        combined = DomainSelectorStore(domain=domain)
        combined.static_selectors = {
            **db_store.static_selectors,
            **file_store.static_selectors,
        }
        combined.cms_selectors = {**db_store.cms_selectors, **file_store.cms_selectors}
        combined.total_learning_sessions = max(
            file_store.total_learning_sessions, db_store.total_learning_sessions
        )
        combined.cms_type = file_store.cms_type or db_store.cms_type
        combined.cms_confidence = max(
            file_store.cms_confidence, db_store.cms_confidence
        )

        def build_map(
            store: DomainSelectorStore,
        ) -> Dict[Tuple[str, str], SelectorMetadata]:
            mapping: Dict[Tuple[str, str], SelectorMetadata] = {}
            for field_name, metas in store.selectors.items():
                for meta in metas:
                    mapping[(field_name, meta.selector)] = meta
            return mapping

        file_map = build_map(file_store)
        db_map = build_map(db_store)

        all_keys = set(file_map.keys()) | set(db_map.keys())

        for field_name, selector in sorted(all_keys):
            file_meta = file_map.get((field_name, selector))
            db_meta = db_map.get((field_name, selector))

            if file_meta and not db_meta:
                chosen = file_meta
                report["file_only"] += 1
            elif db_meta and not file_meta:
                chosen = db_meta
                report["db_only"] += 1
            else:
                chosen, reason = self._prefer_metadata(file_meta, db_meta)
                report["merged"] += 1
                report[reason] += 1
                if reason.startswith("file_"):
                    report["file_wins"] = report.get("file_wins", 0) + 1
                else:
                    report["db_wins"] = report.get("db_wins", 0) + 1
                report["total_conflicts"] += 1

            combined.selectors[field].append(chosen)
            combined.total_selectors += 1

        # Recompute reliability stats
        combined.reliable_selectors = sum(
            1
            for metas in combined.selectors.values()
            for meta in metas
            if meta.is_reliable
        )
        combined.stale_selectors = sum(
            1
            for metas in combined.selectors.values()
            for meta in metas
            if meta.is_stale
        )
        combined.last_updated = time.time()

        return combined, report

    @staticmethod
    def _prefer_metadata(
        file_meta: Optional[SelectorMetadata], db_meta: Optional[SelectorMetadata]
    ) -> Tuple[SelectorMetadata, str]:
        """Enhanced metadata preference with detailed reasoning."""
        if file_meta is None:
            return db_meta, "db_only"  # type: ignore
        if db_meta is None:
            return file_meta, "file_only"  # type: ignore

        # Primary: Compare by last_used timestamp (newer wins)
        file_last = file_meta.last_used or 0.0
        db_last = db_meta.last_used or 0.0

        if file_last > db_last:
            return file_meta, "file_newer"
        if db_last > file_last:
            return db_meta, "db_newer"

        # Secondary: Compare by confidence_score (higher wins)
        if file_meta.confidence_score > db_meta.confidence_score:
            return file_meta, "file_higher_confidence"
        if db_meta.confidence_score > file_meta.confidence_score:
            return db_meta, "db_higher_confidence"

        # Tertiary: Compare by reliability_score (higher wins)
        if file_meta.reliability_score > db_meta.reliability_score:
            return file_meta, "file_more_reliable"
        if db_meta.reliability_score > file_meta.reliability_score:
            return db_meta, "db_more_reliable"

        # Quaternary: Compare by usage_count (higher wins)
        if file_meta.usage_count > db_meta.usage_count:
            return file_meta, "file_more_used"
        if db_meta.usage_count > file_meta.usage_count:
            return db_meta, "db_more_used"

        # Final tiebreaker: Prefer file (local changes)
        return file_meta, "file_tiebreaker"

    def force_sync(self, domain: str, direction: str = "to_database") -> Dict[str, Any]:
        """Force synchronization in specified direction with detailed reporting."""
        domain_norm = domain.strip().lower()
        report = {
            "domain": domain_norm,
            "direction": direction,
            "success": False,
            "selectors_synced": 0,
            "error": None,
            "timestamp": time.time(),
        }

        try:
            if direction == "to_database" and self.database_manager:
                store = self.hybrid_load(domain_norm)
                if store:
                    self.sync_to_database(store)
                    report["selectors_synced"] = store.total_selectors
                    report["success"] = True
                else:
                    report["error"] = "No store found to sync"
            elif direction == "to_file" and self.database_manager:
                store = self.load_from_database(domain_norm)
                if store:
                    self.stores[domain_norm] = store
                    self.memory_manager.cache_store(store)
                    self._save_domain_store(store)
                    report["selectors_synced"] = store.total_selectors
                    report["success"] = True
                else:
                    report["error"] = "No database data found to sync"
            elif direction == "merge":
                merge_result = self._merge_domain_sources(domain_norm)
                if merge_result:
                    report.update(merge_result)
                    report["success"] = True
            else:
                report["error"] = f"Invalid direction: {direction}"
        except Exception as e:
            report["error"] = str(e)
            self.logger.error(f"Force sync failed for {domain_norm}: {e}")

        return report

    def get_reconciliation_report(self, domain: str) -> Dict[str, Any]:
        """Generate detailed reconciliation report for a domain."""
        domain_norm = domain.strip().lower()
        report = {
            "domain": domain_norm,
            "timestamp": time.time(),
            "file_exists": False,
            "db_exists": False,
            "merge_performed": False,
            "merge_report": {},
            "file_stats": {},
            "db_stats": {},
            "recommendation": "",
        }

        # Check file store
        file_store = self._load_domain_store(domain_norm)
        if file_store:
            report["file_exists"] = True
            report["file_stats"] = file_store.get_statistics()

        # Check DB store
        db_store = None
        if self.database_manager:
            db_store = self.load_from_database(domain_norm)
            if db_store:
                report["db_exists"] = True
                report["db_stats"] = db_store.get_statistics()

        # Perform dry-run merge to get conflict analysis
        if file_store and db_store:
            _, merge_report = self._merge_selector_metadata(
                domain_norm, file_store, db_store
            )
            report["merge_performed"] = True
            report["merge_report"] = merge_report

            # Generate recommendation
            conflicts = merge_report.get("total_conflicts", 0)
            if conflicts > 0:
                file_wins = merge_report.get("file_wins", 0)
                db_wins = merge_report.get("db_wins", 0)
                if file_wins > db_wins:
                    report["recommendation"] = (
                        "File appears to have more recent/changes. Consider merging."
                    )
                elif db_wins > file_wins:
                    report["recommendation"] = (
                        "Database appears to have more recent/changes. Consider merging."
                    )
                else:
                    report["recommendation"] = "Balanced conflicts. Safe to merge."
            else:
                report["recommendation"] = "No conflicts detected. Stores are in sync."
        elif file_store and not db_store:
            report["recommendation"] = (
                "File exists but no database data. Consider migrating to database."
            )
        elif db_store and not file_store:
            report["recommendation"] = (
                "Database exists but no file data. Consider syncing to file."
            )
        else:
            report["recommendation"] = "No data found in either location."

        return report

    def get_all_domains_reconciliation_report(self) -> Dict[str, Any]:
        """Generate reconciliation reports for all domains."""
        all_domains = set()

        # Get domains from files
        for store_file in self.memory_dir.glob("*.json"):
            if store_file.name != "backups":
                all_domains.add(store_file.stem)

        # Get domains from database
        if self.database_manager:
            try:
                db_domains = {
                    self.database_manager._normalize_domain(domain)
                    for domain in self.database_manager.get_all_site_domains()
                }
                all_domains.update(db_domains)
            except Exception as e:
                self.logger.warning(f"Failed to get DB domains: {e}")

        reports = {}
        summary = {
            "total_domains": len(all_domains),
            "domains_with_conflicts": 0,
            "domains_file_only": 0,
            "domains_db_only": 0,
            "domains_synced": 0,
            "total_conflicts": 0,
        }

        for domain in sorted(all_domains):
            report = self.get_reconciliation_report(domain)
            reports[domain] = report

            # Update summary
            if report["merge_performed"]:
                conflicts = report["merge_report"].get("total_conflicts", 0)
                if conflicts > 0:
                    summary["domains_with_conflicts"] += 1
                    summary["total_conflicts"] += conflicts
                else:
                    summary["domains_synced"] += 1
            elif report["file_exists"] and not report["db_exists"]:
                summary["domains_file_only"] += 1
            elif report["db_exists"] and not report["file_exists"]:
                summary["domains_db_only"] += 1

        return {
            "summary": summary,
            "domain_reports": reports,
            "generated_at": time.time(),
        }

    def ingest_json_to_database(
        self,
        domains: Optional[List[str]] = None,
        force: bool = False,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Enhanced one-time migration utility to ingest JSON stores to database.

        Args:
            domains: List of domains to migrate (None = all)
            force: Force migration even if DB data exists
            dry_run: Report what would be migrated without actually doing it

        Returns:
            Migration report with statistics
        """
        if not self.database_manager:
            return {
                "success": False,
                "error": "Database integration is disabled",
                "timestamp": time.time(),
            }

        report = {
            "success": True,
            "total_domains_processed": 0,
            "domains_migrated": 0,
            "domains_skipped": 0,
            "selectors_migrated": 0,
            "errors": [],
            "domain_reports": {},
            "dry_run": dry_run,
            "timestamp": time.time(),
        }

        # Determine which domains to process
        target_domains = domains if domains else list(self.stores.keys())

        for domain in target_domains:
            domain_report = {
                "domain": domain,
                "processed": False,
                "selectors_count": 0,
                "error": None,
            }

            try:
                store = self.stores.get(domain) or self._load_domain_store(domain)
                if not store:
                    domain_report["error"] = "No JSON store found"
                    report["errors"].append(f"{domain}: No JSON store found")
                    continue

                domain_report["selectors_count"] = store.total_selectors

                # Check if DB already has data for this domain
                if not force:
                    existing_db_data = self.database_manager.load_site_selectors(domain)
                    if existing_db_data:
                        domain_report["error"] = (
                            "Database already has data (use force=True to override)"
                        )
                        report["domains_skipped"] += 1
                        report["domain_reports"][domain] = domain_report
                        continue

                if not dry_run:
                    self.sync_to_database(store)
                    self.logger.info(
                        f"Migrated {store.total_selectors} selectors for {domain}"
                    )

                domain_report["processed"] = True
                report["domains_migrated"] += 1
                report["selectors_migrated"] += store.total_selectors
                report["total_domains_processed"] += 1

            except Exception as e:
                error_msg = f"Failed to migrate {domain}: {str(e)}"
                domain_report["error"] = str(e)
                report["errors"].append(error_msg)
                self.logger.error(error_msg)

            report["domain_reports"][domain] = domain_report

        if dry_run:
            report["message"] = (
                f"Dry run completed. Would migrate {report['selectors_migrated']} selectors across {report['domains_migrated']} domains."
            )
        else:
            report["message"] = (
                f"Migration completed. Migrated {report['selectors_migrated']} selectors across {report['domains_migrated']} domains."
            )

        return report

    # ------------------------------------------------------------------
    # Hybrid persistence helpers
    # ------------------------------------------------------------------

    def load_from_database(self, domain: str) -> Optional[DomainSelectorStore]:
        if not self._database_sync_enabled:
            return None

        selectors_payload = self.database_manager.load_site_selectors(domain)
        if not selectors_payload:
            return None

        store = DomainSelectorStore(domain=domain)
        profile = self.database_manager.get_or_create_site_profile(domain)
        store.cms_type = profile.get("cms_type")
        store.cms_confidence = profile.get("cms_confidence", 0.0)

        for field_name, selectors in selectors_payload.items():
            for selector_data in selectors:
                metadata = SelectorMetadata(
                    selector=selector_data.get("selector", ""),
                    field=field_name,
                    domain=domain,
                    success_count=selector_data.get("success_count", 0),
                    failure_count=selector_data.get("failure_count", 0),
                    total_attempts=selector_data.get("success_count", 0)
                    + selector_data.get("failure_count", 0),
                    confidence_score=selector_data.get("confidence_score", 0.5),
                    source=selector_data.get("source", "learned"),
                )
                last_used = selector_data.get("last_used")
                if last_used:
                    try:
                        metadata.last_used = float(last_used)
                    except (TypeError, ValueError):
                        metadata.last_used = time.time()
                store.selectors[field].append(metadata)
                store.total_selectors += 1

        store.last_updated = time.time()
        return store

    def sync_to_database(self, store: DomainSelectorStore) -> None:
        if not self._database_sync_enabled:
            return

        payload: Dict[str, List[Dict[str, Any]]] = {}
        for field_name, field_selectors in store.selectors.items():
            payload[field_name] = [
                {
                    "selector": metadata.selector,
                    "confidence_score": metadata.confidence_score,
                    "success_count": metadata.success_count,
                    "failure_count": metadata.failure_count,
                    "last_used": metadata.last_used,
                    "source": metadata.source,
                }
                for metadata in field_selectors
            ]
        self.database_manager.save_site_selectors(store.domain, payload)

    def hybrid_load(self, domain: str) -> Optional[DomainSelectorStore]:
        store = self.memory_manager.get_store(domain)
        if store:
            return store

        if domain in self.stores:
            store = self.stores[domain]
            self.memory_manager.cache_store(store)
            return store

        if self._database_sync_enabled:
            store = self.load_from_database(domain)
            if store:
                self.stores[domain] = store
                self.memory_manager.cache_store(store)
                return store

        store = self._load_domain_store(domain)
        if store:
            self.stores[domain] = store
            self.memory_manager.cache_store(store)
        return store

    def hybrid_save(self, store: DomainSelectorStore) -> None:
        self._save_domain_store(store)

    # Core API Methods

    def load_domain_selectors(self, domain: str) -> Dict[str, List[str]]:
        """
        Load all selectors for a domain.

        Args:
            domain: Domain name

        Returns:
            Dict of field -> list of selectors
        """
        with self._lock:
            store = self.hybrid_load(domain)
            if not store:
                return {}

            result = {}

            # Add learned selectors
            for field in store.selectors:
                result[field] = store.get_best_selectors(field, limit=10)

            # Add static selectors
            for field, selectors in store.static_selectors.items():
                if field not in result:
                    result[field] = []
                result[field].extend(selectors)

            # Add CMS selectors
            for field, selectors in store.cms_selectors.items():
                if field not in result:
                    result[field] = []
                result[field].extend(selectors)

            return result

    def save_domain_selectors(
        self, domain: str, selectors: Dict[str, List[str]], source: str = "config"
    ) -> None:
        """
        Save selectors for a domain.

        Args:
            domain: Domain name
            selectors: Dict of field -> list of selectors
            source: Source of selectors (config, cms, manual)
        """
        with self._lock:
            store = self._get_or_create_store(domain)

            if source == "static":
                store.static_selectors.update(selectors)
            elif source == "cms":
                store.cms_selectors.update(selectors)
            else:
                # Add as learned selectors
                for field, field_selectors in selectors.items():
                    for selector in field_selectors:
                        store.add_selector(field, selector, source=source)

            store.last_updated = time.time()
            self.memory_manager.cache_store(store)

            # Save asynchronously
            self._executor.submit(self._save_domain_store, store)

    def update_selector_confidence(
        self,
        domain: str,
        field: str,
        selector: str,
        success: bool,
        extraction_time: float = 0.0,
    ) -> None:
        """
        Update confidence score for a selector.

        Args:
            domain: Domain name
            field: Field type (name, price, stock)
            selector: CSS selector
            success: Whether extraction was successful
            extraction_time: Time taken for extraction
        """
        with self._lock:
            store = self._get_or_create_store(domain)
            # Ensure selector exists before updating
            store.add_selector(field, selector)
            store.update_selector_performance(field, selector, success, extraction_time)

            # Update index if needed
            if success:
                self.index.add_selector(domain, field, selector, store.cms_type)

            # Save asynchronously
            self._executor.submit(self._save_domain_store, store)

    def cleanup_old_selectors(self, max_age_days: int = 90) -> Dict[str, int]:
        """
        Cleanup old selectors across all domains.

        Args:
            max_age_days: Maximum age in days for selectors

        Returns:
            Dict of domain -> number of removed selectors
        """
        cleanup_results = {}

        for domain, store in self.stores.items():
            try:
                removed_count = store.cleanup_stale_selectors(max_age_days)
                if removed_count > 0:
                    cleanup_results[domain] = removed_count
                    self._executor.submit(self._save_domain_store, store)
            except Exception as e:
                self.logger.warning(f"Failed to cleanup selectors for {domain}: {e}")

        # Rebuild index after cleanup
        self.index.rebuild_index(self.stores)

        return cleanup_results

    def _get_or_create_store(self, domain: str) -> DomainSelectorStore:
        """Get existing store or create new one."""
        store = self.hybrid_load(domain)
        if store is None:
            store = DomainSelectorStore(domain=domain)
            self.stores[domain] = store
            self.memory_manager.cache_store(store)
        return store

    # Integration Hooks

    def register_integration_hook(self, component_name: str, hook_function) -> None:
        """
        Register an integration hook for a component.

        Args:
            component_name: Name of the component (e.g., 'product_parser')
            hook_function: Function to call for integration
        """
        self._integration_hooks[component_name] = hook_function

    def get_selectors_for_component(
        self, component_name: str, domain: str, field: str, **kwargs
    ) -> List[str]:
        """
        Get selectors optimized for a specific component.

        Args:
            component_name: Component name
            domain: Domain name
            field: Field type
            **kwargs: Additional parameters

        Returns:
            List of optimized selectors
        """
        base_selectors = self.load_domain_selectors(domain).get(field, [])

        # Apply component-specific optimization if hook exists
        if component_name in self._integration_hooks:
            try:
                optimized_selectors = self._integration_hooks[component_name](
                    base_selectors, domain, field, **kwargs
                )
                return optimized_selectors
            except Exception as e:
                self.logger.warning(
                    f"Integration hook failed for {component_name}: {e}"
                )

        return base_selectors

    # Maintenance and Statistics

    def get_memory_stats(self) -> Dict[str, Any]:
        """Get comprehensive memory statistics."""
        stats = {
            "total_domains": len(self.stores),
            "total_selectors": sum(
                store.total_selectors for store in self.stores.values()
            ),
            "cache_stats": self.memory_manager.get_cache_stats(),
            "backup_count": len(self.backup_manager.list_backups()),
            "domains": {},
        }

        for domain, store in self.stores.items():
            stats["domains"][domain] = store.get_statistics()

        return stats

    def create_backup(self) -> str:
        """Create a backup of all selector data."""
        return self.backup_manager.create_backup(self.stores)

    def restore_backup(self, backup_file: str) -> bool:
        """Restore from a backup file."""
        try:
            restored_stores = self.backup_manager.restore_backup(backup_file)

            with self._lock:
                self.stores.update(restored_stores)
                # Update cache and index
                for store in restored_stores.values():
                    self.memory_manager.cache_store(store)
                self.index.rebuild_index(self.stores)

            return True
        except Exception as e:
            self.logger.error(f"Failed to restore backup: {e}")
            return False

    def optimize_memory(self) -> None:
        """Optimize memory usage and performance."""
        # Clear old cache entries
        self.memory_manager.invalidate_cache()

        # Cleanup stale selectors
        self.cleanup_old_selectors()

        # Rebuild index for efficiency
        self.index.rebuild_index(self.stores)

    # Error Handling and Recovery

    def handle_corruption(self, domain: str) -> bool:
        """
        Handle corrupted domain store.

        Args:
            domain: Domain name

        Returns:
            True if recovery successful
        """
        try:
            # Try to restore from backup
            backups = self.backup_manager.list_backups()
            if backups:
                latest_backup = max(backups, key=lambda x: Path(x).stat().st_mtime)
                restored_stores = self.backup_manager.restore_backup(latest_backup)

                if domain in restored_stores:
                    with self._lock:
                        self.stores[domain] = restored_stores[domain]
                        self.memory_manager.cache_store(self.stores[domain])
                    return True

            # Fallback: create empty store
            with self._lock:
                self.stores[domain] = DomainSelectorStore(domain=domain)
                self.memory_manager.cache_store(self.stores[domain])

            return True

        except Exception as e:
            self.logger.error(f"Failed to handle corruption for {domain}: {e}")
            return False

    def graceful_degradation(self, domain: str, field: str) -> List[str]:
        """
        Provide fallback selectors when primary selectors fail.

        Args:
            domain: Domain name
            field: Field type

        Returns:
            List of fallback selectors
        """
        # Generic fallback selectors
        generic_fallbacks = {
            "name": ["h1", ".product-title", ".title", "[data-product-name]"],
            "price": [".price", ".cost", ".amount", "[data-price]"],
            "stock": [".stock", ".availability", ".quantity", "[data-stock]"],
            "variations": ["select", ".options select", "[data-variant]"],
        }

        return generic_fallbacks.get(field, [])

    def __del__(self):
        """Cleanup resources."""
        try:
            self._executor.shutdown(wait=False)
        except Exception:
            pass
