"""Database Manager with asyncpg connection pool."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse

import asyncpg

logger = logging.getLogger(__name__)


class DatabaseManagerError(RuntimeError):
    """Base exception for database manager errors."""


class DatabasePoolNotInitializedError(DatabaseManagerError):
    """Raised when connection pool usage is attempted before initialization."""


class DatabaseExecutionError(DatabaseManagerError):
    """Raised when a database operation fails."""

    def __init__(self, message: str, *, query: Optional[str] = None) -> None:
        super().__init__(message)
        self.query = query


class DatabaseManager:
    """
    Async database manager using AsyncPG connection pool.

    Manages all database operations for jobs, pages, snapshots, and exports.
    Uses connection pooling for efficient async operations.

    Example:
        >>> db = DatabaseManager("postgresql://user:pass@localhost/db")
        >>> await db.init_pool()
        >>> job_id = await db.create_job("uuid-123", "example.com", {}, 100)
        >>> await db.close()
    """

    def __init__(self, database_url: Optional[str] = None):
        """
        Initialize database manager.

        Args:
            database_url: PostgreSQL connection string
                Format: postgresql://user:password@host:port/database
                Falls back to DATABASE_URL env var if not provided
        """
        self.database_url = database_url or os.getenv(
            "DATABASE_URL",
            "postgresql://scraper:scraper@localhost:5432/scraper",
        )
        self.pool: Optional[asyncpg.Pool] = None
        logger.debug(
            "DatabaseManager initialized",
            extra={"dsn": self._redact_dsn(self.database_url)},
        )

    async def init_pool(self, min_size: int = 10, max_size: int = 20):
        """
        Create AsyncPG connection pool.

        Args:
            min_size: Minimum number of connections in pool (default: 10)
            max_size: Maximum number of connections in pool (default: 20)

        Raises:
            ConnectionError: If unable to connect to database
        """
        if self.pool is not None:
            logger.warning("Connection pool already initialized")
            return

        logger.info(
            "Creating asyncpg pool",
            extra={"min_size": min_size, "max_size": max_size},
        )

        try:
            self.pool = await asyncpg.create_pool(
                self.database_url,
                min_size=min_size,
                max_size=max_size,
                command_timeout=60,
            )
            logger.info(
                "Connection pool ready",
                extra={"min_size": min_size, "max_size": max_size},
            )
        except asyncpg.PostgresError as exc:
            logger.exception("AsyncPG reported database error while creating pool")
            raise ConnectionError(f"Database connection failed: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - propagate unexpected failures
            logger.exception("Unexpected error while creating connection pool")
            raise ConnectionError(f"Database connection failed: {exc}") from exc

    async def close(self):
        """Close connection pool gracefully."""
        if not self.pool:
            logger.debug("close() called but pool is already released")
            return

        await self.pool.close()
        self.pool = None
        logger.info("Connection pool closed")

    async def execute(self, query: str, *args) -> str:
        """
        Execute SQL command.

        Args:
            query: SQL query
            *args: Query parameters

        Returns:
            Result status message

        Raises:
            RuntimeError: If pool not initialized
        """
        pool = self._ensure_pool()

        try:
            async with pool.acquire() as conn:
                result = await conn.execute(query, *args)
                logger.debug(
                    "Query executed",
                    extra=self._build_query_context(query, args, result=result),
                )
                return result
        except asyncpg.PostgresError as exc:
            self._log_query_failure("Query execution failed", exc, query, args)
            raise
        except Exception as exc:  # noqa: BLE001 - propagate unexpected failures
            self._log_query_failure(
                "Unexpected error during query execution", exc, query, args
            )
            raise DatabaseExecutionError(
                str(exc), query=self._trim_query(query)
            ) from exc

    async def fetch_one(self, query: str, *args) -> Optional[Dict]:
        """
        Fetch single row.

        Args:
            query: SQL query
            *args: Query parameters

        Returns:
            Dict with row data or None if not found
        """
        pool = self._ensure_pool()

        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(query, *args)
        except asyncpg.PostgresError as exc:
            self._log_query_failure("Fetch one failed", exc, query, args)
            raise
        except Exception as exc:  # noqa: BLE001 - propagate unexpected failures
            self._log_query_failure(
                "Unexpected error during fetch_one", exc, query, args
            )
            raise DatabaseExecutionError(
                str(exc), query=self._trim_query(query)
            ) from exc

        return dict(row) if row else None

    async def fetch_all(self, query: str, *args) -> List[Dict]:
        """
        Fetch all rows.

        Args:
            query: SQL query
            *args: Query parameters

        Returns:
            List of dicts with row data
        """
        pool = self._ensure_pool()

        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(query, *args)
        except asyncpg.PostgresError as exc:
            self._log_query_failure("Fetch all failed", exc, query, args)
            raise
        except Exception as exc:  # noqa: BLE001 - propagate unexpected failures
            self._log_query_failure(
                "Unexpected error during fetch_all", exc, query, args
            )
            raise DatabaseExecutionError(
                str(exc), query=self._trim_query(query)
            ) from exc

        return [dict(row) for row in rows]

    def _ensure_pool(self) -> asyncpg.Pool:
        if not self.pool:
            raise DatabasePoolNotInitializedError(
                "Connection pool not initialized. Call init_pool() before executing queries."
            )
        return self.pool

    def _log_query_failure(
        self,
        message: str,
        exc: Exception,
        query: str,
        params: Iterable[Any],
    ) -> None:
        logger.exception(
            message,
            extra=self._build_query_context(query, params),
        )

    def _build_query_context(
        self, query: str, params: Iterable[Any], *, result: Optional[str] = None
    ) -> Dict[str, Any]:
        context: Dict[str, Any] = {
            "query": self._trim_query(query),
            "params": self._serialize_params(params),
        }
        if result is not None:
            context["result"] = result
        return context

    @staticmethod
    def _serialize_params(params: Iterable[Any]) -> List[Any]:
        serialized: List[Any] = []
        for value in params:
            if isinstance(value, (dict, list)):
                serialized.append("<json>")
            elif value is None:
                serialized.append(None)
            else:
                try:
                    serialized.append(str(value)[:100])
                except Exception:  # noqa: BLE001 - defensive fallback
                    serialized.append("<unserializable>")
        return serialized

    @staticmethod
    def _trim_query(query: str, limit: int = 200) -> str:
        compact = " ".join(query.split())
        return compact if len(compact) <= limit else f"{compact[:limit]}..."

    @staticmethod
    def _redact_dsn(dsn: str) -> str:
        try:
            parsed = urlparse(dsn)
        except Exception:  # noqa: BLE001 - fallback to slicing if parsing fails
            return dsn[:30] + "..."

        scheme = f"{parsed.scheme}://" if parsed.scheme else ""
        username = parsed.username or ""
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        user_segment = f"{username}@" if username else ""
        return f"{scheme}{user_segment}{host}{port}{parsed.path or ''}"

    # ==================== JOBS CRUD ====================

    async def create_job(
        self, job_id: str, domain: str, options: dict, total_urls: int
    ) -> str:
        """
        Create new job record.

        Args:
            job_id: UUID for job
            domain: Target domain (e.g., "example.com")
            options: Job configuration (max_concurrency, budgets, etc.)
            total_urls: Total number of URLs to scrape

        Returns:
            job_id: Created job ID

        Raises:
            ValueError: If job_id already exists
        """
        logger.info(f"Creating job {job_id} for domain {domain}")

        query = """
            INSERT INTO jobs (id, domain, status, options, total_urls)
            VALUES ($1, $2, 'queued', $3, $4)
            RETURNING id
        """

        try:
            result = await self.fetch_one(query, job_id, domain, options, total_urls)
            if not result:
                raise ValueError(f"Failed to create job {job_id}")
            logger.info(f"Job created successfully: {job_id}")
            return result["id"]
        except asyncpg.UniqueViolationError as exc:
            logger.error(f"Job {job_id} already exists")
            raise ValueError(f"Job {job_id} already exists") from exc

    async def update_job_status(self, job_id: str, status: str, **kwargs):
        """
        Update job status and optional fields.

        Args:
            job_id: Job UUID
            status: New status (queued, running, succeeded, failed, cancelled)
            **kwargs: Optional fields to update:
                - started_at: datetime
                - finished_at: datetime
                - success_urls: int
                - failed_urls: int
                - traffic_mb_used: float
                - residential_mb_used: float
                - error_message: str

        Raises:
            ValueError: If invalid status or job not found
        """
        valid_statuses = ["queued", "running", "succeeded", "failed", "cancelled"]
        if status not in valid_statuses:
            raise ValueError(
                f"Invalid status: {status}. Must be one of {valid_statuses}"
            )

        logger.info(f"Updating job {job_id} status to {status}")

        # Build dynamic UPDATE query
        set_clauses = ["status = $2"]
        params = [job_id, status]
        param_count = 2

        # Auto-manage timestamps
        if status == "running" and "started_at" not in kwargs:
            kwargs["started_at"] = datetime.utcnow()
        if (
            status in ["succeeded", "failed", "cancelled"]
            and "finished_at" not in kwargs
        ):
            kwargs["finished_at"] = datetime.utcnow()

        # Add optional fields
        for key, value in kwargs.items():
            param_count += 1
            set_clauses.append(f"{key} = ${param_count}")
            params.append(value)

        query = f"""
            UPDATE jobs
            SET {', '.join(set_clauses)}
            WHERE id = $1
        """

        try:
            await self.execute(query, *params)
            logger.info(
                f"Job {job_id} updated: status={status}, fields={list(kwargs.keys())}"
            )
        except Exception as exc:
            logger.error(f"Failed to update job {job_id}: {exc}")
            raise

    async def get_job(self, job_id: str) -> Optional[Dict]:
        """
        Get job details by ID.

        Args:
            job_id: Job UUID

        Returns:
            Job record as dict, or None if not found
        """
        logger.debug(f"Fetching job {job_id}")

        query = """
            SELECT 
                id, domain, status, created_at, started_at, finished_at,
                options, total_urls, success_urls, failed_urls,
                traffic_mb_used, residential_mb_used, error_message
            FROM jobs
            WHERE id = $1
        """

        try:
            job_record = await self.fetch_one(query, job_id)
            if job_record:
                logger.debug(f"Job found: {job_id}")
            else:
                logger.warning(f"Job not found: {job_id}")
            return job_record
        except Exception as exc:
            logger.error(f"Failed to fetch job {job_id}: {exc}")
            raise

    async def list_jobs(
        self, domain: Optional[str] = None, limit: int = 50, offset: int = 0
    ) -> List[Dict]:
        """
        List jobs with optional filtering.

        Args:
            domain: Filter by domain (optional)
            limit: Maximum number of results (default: 50)
            offset: Pagination offset (default: 0)

        Returns:
            List of job records
        """
        logger.debug(f"Listing jobs: domain={domain}, limit={limit}, offset={offset}")

        if domain:
            query = """
                SELECT 
                    id, domain, status, created_at, started_at, finished_at,
                    options, total_urls, success_urls, failed_urls,
                    traffic_mb_used, residential_mb_used, error_message
                FROM jobs
                WHERE domain = $1
                ORDER BY created_at DESC
                LIMIT $2 OFFSET $3
            """
            jobs = await self.fetch_all(query, domain, limit, offset)
        else:
            query = """
                SELECT 
                    id, domain, status, created_at, started_at, finished_at,
                    options, total_urls, success_urls, failed_urls,
                    traffic_mb_used, residential_mb_used, error_message
                FROM jobs
                ORDER BY created_at DESC
                LIMIT $1 OFFSET $2
            """
            jobs = await self.fetch_all(query, limit, offset)

        logger.info(f"Found {len(jobs)} jobs")
        return jobs

    # ==================== PAGES CRUD ====================

    async def insert_page_result(self, job_id: str, result: dict):
        """
        Insert page scraping result.

        Args:
            job_id: Parent job UUID
            result: Scraping result containing:
                - url: str (required)
                - final_url: str (optional)
                - http_status: int (optional)
                - title: str (optional)
                - h1: str (optional)
                - content_hash: str (optional, SHA-256)
                - bytes_in: int (optional)
                - data_full: dict (optional, product data)
                - data_seo: dict (optional, SEO metadata)
                - strategy_used: str (optional)
                - proxy_used: str (optional)
                - error_class: str (optional)
                - error_message: str (optional)
                - retry_count: int (optional)
        """
        url = result.get("url")
        if not url:
            raise ValueError("Page result must contain 'url' field")

        logger.debug(f"Inserting page result for job {job_id}: {url}")

        query = """
            INSERT INTO pages (
                job_id, url, final_url, http_status, title, h1,
                content_hash, bytes_in, data_full, data_seo,
                error_class, error_message, retry_count,
                strategy_used, proxy_used
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
        """

        try:
            await self.execute(
                query,
                job_id,
                result.get("url"),
                result.get("final_url"),
                result.get("http_status"),
                result.get("title"),
                result.get("h1"),
                result.get("content_hash"),
                result.get("bytes_in", 0),
                result.get("data_full"),
                result.get("data_seo"),
                result.get("error_class"),
                result.get("error_message"),
                result.get("retry_count", 0),
                result.get("strategy_used"),
                result.get("proxy_used"),
            )
            logger.debug(f"Page result inserted: {url}")
        except Exception as exc:
            logger.error(f"Failed to insert page result {url}: {exc}")
            raise

    async def get_job_pages(
        self, job_id: str, limit: Optional[int] = None
    ) -> List[Dict]:
        """
        Get all pages for a job.

        Args:
            job_id: Job UUID
            limit: Optional limit on results

        Returns:
            List of page records
        """
        logger.debug(f"Fetching pages for job {job_id}")

        query = """
            SELECT 
                id, job_id, url, final_url, http_status, fetched_at,
                title, h1, content_hash, bytes_in,
                data_full, data_seo,
                error_class, error_message, retry_count,
                strategy_used, proxy_used
            FROM pages
            WHERE job_id = $1
            ORDER BY fetched_at
        """

        if limit:
            query += f" LIMIT {limit}"

        try:
            pages = await self.fetch_all(query, job_id)
            logger.info(f"Found {len(pages)} pages for job {job_id}")
            return pages
        except Exception as exc:
            logger.error(f"Failed to fetch pages for job {job_id}: {exc}")
            raise

    # ==================== SNAPSHOTS ====================

    async def update_snapshot(
        self, url: str, domain: str, content_hash: str, data: dict, job_id: str
    ):
        """
        Update or insert snapshot for diff comparison.

        Args:
            url: Page URL (primary key)
            domain: Domain name
            content_hash: SHA-256 hash of content
            data: Structured data (product/page data)
            job_id: Source job UUID
        """
        logger.debug(f"Updating snapshot for {url}")

        query = """
            INSERT INTO snapshots (url, domain, last_hash, last_data, last_crawl_at, last_job_id)
            VALUES ($1, $2, $3, $4, NOW(), $5)
            ON CONFLICT (url)
            DO UPDATE SET
                last_hash = EXCLUDED.last_hash,
                last_data = EXCLUDED.last_data,
                last_crawl_at = EXCLUDED.last_crawl_at,
                last_job_id = EXCLUDED.last_job_id
        """

        try:
            await self.execute(query, url, domain, content_hash, data, job_id)
            logger.debug(f"Snapshot updated: {url}")
        except Exception as exc:
            logger.error(f"Failed to update snapshot {url}: {exc}")
            raise

    async def get_snapshots_for_domain(self, domain: str) -> List[Dict]:
        """
        Get all snapshots for a domain.

        Args:
            domain: Domain name

        Returns:
            List of snapshot records
        """
        logger.debug(f"Fetching snapshots for domain {domain}")

        query = """
            SELECT url, domain, last_hash, last_data, last_crawl_at, last_job_id
            FROM snapshots
            WHERE domain = $1
            ORDER BY last_crawl_at DESC
        """

        try:
            snapshots = await self.fetch_all(query, domain)
            logger.info(f"Found {len(snapshots)} snapshots for {domain}")
            return snapshots
        except Exception as exc:
            logger.error(f"Failed to fetch snapshots for {domain}: {exc}")
            raise

    # ==================== EXPORTS ====================

    async def register_export(
        self, job_id: str, export_type: str, format: str, path: str, size_bytes: int
    ) -> str:
        """
        Register export artifact.

        Args:
            job_id: Parent job UUID
            export_type: Type (full, seo, diff)
            format: Format (csv, xlsx, json)
            path: File path
            size_bytes: File size

        Returns:
            Export ID (UUID)
        """
        logger.info(f"Registering export for job {job_id}: {export_type}.{format}")

        query = """
            INSERT INTO exports (job_id, type, format, path, size_bytes)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
        """

        try:
            result = await self.fetch_one(
                query, job_id, export_type, format, path, size_bytes
            )
            if not result:
                raise ValueError(f"Failed to register export for job {job_id}")
            export_id = result["id"]
            logger.info(f"Export registered: {export_id}")
            return export_id
        except Exception as exc:
            logger.error(f"Failed to register export: {exc}")
            raise

    async def get_job_exports(self, job_id: str) -> List[Dict]:
        """
        Get all exports for a job.

        Args:
            job_id: Job UUID

        Returns:
            List of export records
        """
        logger.debug(f"Fetching exports for job {job_id}")

        query = """
            SELECT id, job_id, type, format, path, size_bytes, created_at
            FROM exports
            WHERE job_id = $1
            ORDER BY created_at DESC
        """

        try:
            exports = await self.fetch_all(query, job_id)
            logger.info(f"Found {len(exports)} exports for job {job_id}")
            return exports
        except Exception as exc:
            logger.error(f"Failed to fetch exports for job {job_id}: {exc}")
            raise
