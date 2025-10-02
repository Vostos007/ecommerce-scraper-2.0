# Implementation Plan — Webscraper MVP Architecture

> **Статус проекта**: 50% готовности к MVP  
> **Дата создания**: 2025-10-01  
> **Критические блокеры**: FastAPI Backend, Worker Pool, Database Schema

---

## 🎯 Executive Summary

Анализ выявил, что проект имеет **солидную кодовую базу для скрейпинга** (95%), но **критически не хватает orchestration layer** — FastAPI backend с асинхронной очередью задач. Текущая архитектура использует Next.js API routes с прямым spawn процессов, что не соответствует PRD и блокирует:

- Идемпотентность и graceful resume
- Масштабирование (worker pool)
- Мониторинг и observability
- Production-ready deployment

**План**: реализовать недостающие компоненты в 4 фазы (2-3 недели на MVP).

---

## 📋 Phase 1: Database Schema & Migrations (Неделя 1, дни 1-2)

### 1.1. Создание миграционного фреймворка

**Файл**: [`database/migrations/001_create_jobs_schema.sql`](database/migrations/001_create_jobs_schema.sql)

```sql
-- Jobs: основная таблица job'ов
CREATE TABLE IF NOT EXISTS jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    domain VARCHAR(255) NOT NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'queued',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    started_at TIMESTAMP WITH TIME ZONE,
    finished_at TIMESTAMP WITH TIME ZONE,
    options JSONB,
    error_message TEXT,
    
    -- Метрики
    total_urls INTEGER DEFAULT 0,
    success_urls INTEGER DEFAULT 0,
    failed_urls INTEGER DEFAULT 0,
    
    -- Бюджеты
    traffic_mb_used NUMERIC(10,2) DEFAULT 0,
    residential_mb_used NUMERIC(10,2) DEFAULT 0,
    
    CONSTRAINT chk_status CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled'))
);

CREATE INDEX idx_jobs_domain ON jobs(domain);
CREATE INDEX idx_jobs_status ON jobs(status);
CREATE INDEX idx_jobs_created_at ON jobs(created_at DESC);

-- Pages: результаты по каждому URL
CREATE TABLE IF NOT EXISTS pages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    final_url TEXT,
    http_status INTEGER,
    fetched_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    
    -- Content
    title TEXT,
    h1 TEXT,
    content_hash VARCHAR(64),
    bytes_in INTEGER DEFAULT 0,
    
    -- Structured data
    data_full JSONB,
    data_seo JSONB,
    
    -- Errors
    error_class VARCHAR(100),
    error_message TEXT,
    retry_count INTEGER DEFAULT 0,
    
    -- Strategy tracking
    strategy_used VARCHAR(50),
    proxy_used VARCHAR(255)
);

CREATE INDEX idx_pages_job_id ON pages(job_id);
CREATE INDEX idx_pages_url ON pages(url);
CREATE INDEX idx_pages_http_status ON pages(http_status);

-- Snapshots: для diff
CREATE TABLE IF NOT EXISTS snapshots (
    url TEXT PRIMARY KEY,
    domain VARCHAR(255) NOT NULL,
    last_hash VARCHAR(64),
    last_data JSONB,
    last_crawl_at TIMESTAMP WITH TIME ZONE,
    last_job_id UUID REFERENCES jobs(id)
);

CREATE INDEX idx_snapshots_domain ON snapshots(domain);
CREATE INDEX idx_snapshots_last_crawl_at ON snapshots(last_crawl_at DESC);

-- Exports: артефакты экспорта
CREATE TABLE IF NOT EXISTS exports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    type VARCHAR(20) NOT NULL,
    format VARCHAR(10) NOT NULL,
    path TEXT NOT NULL,
    size_bytes INTEGER,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    
    CONSTRAINT chk_type CHECK (type IN ('full', 'seo', 'diff')),
    CONSTRAINT chk_format CHECK (format IN ('csv', 'xlsx', 'json'))
);

CREATE INDEX idx_exports_job_id ON exports(job_id);
CREATE INDEX idx_exports_type ON exports(type);

-- Metrics: временные ряды метрик
CREATE TABLE IF NOT EXISTS metrics (
    id BIGSERIAL PRIMARY KEY,
    job_id UUID REFERENCES jobs(id) ON DELETE CASCADE,
    name VARCHAR(100) NOT NULL,
    value NUMERIC(12,4),
    labels JSONB,
    timestamp TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX idx_metrics_job_id ON metrics(job_id);
CREATE INDEX idx_metrics_name ON metrics(name);
CREATE INDEX idx_metrics_timestamp ON metrics(timestamp DESC);
```

### 1.2. Migration Runner

**Файл**: [`database/migrate.py`](database/migrate.py)

```python
#!/usr/bin/env python3
"""Database migration runner."""
import os
import sys
from pathlib import Path
from typing import List
import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

def get_connection(database_url: str):
    """Create database connection."""
    return psycopg2.connect(database_url)

def create_migrations_table(conn):
    """Create migrations tracking table."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version VARCHAR(255) PRIMARY KEY,
                applied_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
    conn.commit()

def get_applied_migrations(conn) -> List[str]:
    """Get list of applied migrations."""
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM schema_migrations ORDER BY version")
        return [row[0] for row in cur.fetchall()]

def apply_migration(conn, migration_file: Path):
    """Apply a single migration."""
    version = migration_file.stem
    print(f"Applying migration: {version}")
    
    with open(migration_file) as f:
        sql = f.read()
    
    with conn.cursor() as cur:
        cur.execute(sql)
        cur.execute(
            "INSERT INTO schema_migrations (version) VALUES (%s)",
            (version,)
        )
    conn.commit()
    print(f"✅ Applied: {version}")

def main():
    database_url = os.getenv("DATABASE_URL", "postgresql://scraper:scraper@localhost:5432/scraper")
    
    conn = get_connection(database_url)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    
    create_migrations_table(conn)
    applied = get_applied_migrations(conn)
    
    migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    
    for migration_file in migration_files:
        version = migration_file.stem
        if version not in applied:
            apply_migration(conn, migration_file)
        else:
            print(f"⏭️  Skipped (already applied): {version}")
    
    conn.close()
    print("\n✅ All migrations applied")

if __name__ == "__main__":
    main()
```

### 1.3. Обновление Database Manager

**Файл**: [`database/manager.py`](database/manager.py) — добавить методы:

```python
# Jobs CRUD
async def create_job(self, domain: str, options: dict) -> str:
    """Create new job and return job_id."""
    
async def update_job_status(self, job_id: str, status: str, **kwargs):
    """Update job status and metrics."""
    
async def get_job(self, job_id: str) -> dict:
    """Get job details."""
    
async def list_jobs(self, domain: str = None, limit: int = 50) -> List[dict]:
    """List jobs with filters."""

# Pages CRUD
async def insert_page_result(self, job_id: str, result: dict):
    """Insert page scraping result."""
    
async def get_job_pages(self, job_id: str) -> List[dict]:
    """Get all pages for job."""

# Snapshots
async def update_snapshot(self, url: str, domain: str, data: dict, job_id: str):
    """Update snapshot for diff."""
    
async def get_snapshots_for_domain(self, domain: str) -> List[dict]:
    """Get snapshots for diff comparison."""

# Exports
async def register_export(self, job_id: str, type: str, format: str, path: str, size_bytes: int) -> str:
    """Register export artifact."""
    
async def get_job_exports(self, job_id: str) -> List[dict]:
    """Get export artifacts for job."""
```

**Acceptance Criteria Phase 1**:
- ✅ Миграции применяются idempotent
- ✅ Все таблицы созданы с правильными индексами
- ✅ DatabaseManager имеет методы для jobs/pages/snapshots/exports
- ✅ Smoke test: создать job, добавить pages, сгенерировать snapshot

---

## 🚀 Phase 2: FastAPI Backend Service (Неделя 1, дни 3-5)

### 2.1. Структура директории `services/api/`

```
services/api/
├── main.py              # FastAPI app entry point
├── config.py            # Settings (pydantic-settings)
├── dependencies.py      # Dependency injection (DB, Redis)
├── models.py            # Pydantic models (request/response)
├── queue.py             # RQ/Celery integration
├── routes/
│   ├── __init__.py
│   ├── jobs.py          # /api/jobs endpoints
│   ├── exports.py       # /api/jobs/:id/exports
│   ├── health.py        # /api/health
│   └── sse.py           # /api/jobs/:id/stream (SSE)
└── requirements.txt
```

### 2.2. Core Files

**Файл**: [`services/api/main.py`](services/api/main.py)

```python
"""FastAPI Backend for Webscraper."""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from services.api import config, routes
from database.manager import DatabaseManager

settings = config.get_settings()

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle."""
    # Startup
    app.state.db = DatabaseManager(settings.database_url)
    await app.state.db.init_pool()
    yield
    # Shutdown
    await app.state.db.close()

app = FastAPI(
    title="Webscraper API",
    version="1.0.0",
    lifespan=lifespan
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routes
app.include_router(routes.jobs.router, prefix="/api/jobs", tags=["jobs"])
app.include_router(routes.exports.router, prefix="/api", tags=["exports"])
app.include_router(routes.health.router, prefix="/api", tags=["health"])
app.include_router(routes.sse.router, prefix="/api", tags=["sse"])

@app.get("/")
def root():
    return {"status": "ok", "service": "webscraper-api"}
```

**Файл**: [`services/api/config.py`](services/api/config.py)

```python
"""Configuration using pydantic-settings."""
from pydantic_settings import BaseSettings
from functools import lru_cache

class Settings(BaseSettings):
    database_url: str = "postgresql://scraper:scraper@localhost:5432/scraper"
    redis_url: str = "redis://localhost:6379/0"
    cors_origins: list[str] = ["http://localhost:3000"]
    admin_token: str = "dev-admin-token"
    s3_endpoint: str = "http://localhost:9000"
    s3_bucket: str = "scraper-artifacts"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    
    class Config:
        env_file = ".env"

@lru_cache
def get_settings() -> Settings:
    return Settings()
```

**Файл**: [`services/api/models.py`](services/api/models.py)

```python
"""Pydantic models for API."""
from pydantic import BaseModel, Field, HttpUrl
from typing import Optional, List, Dict, Any
from datetime import datetime
from enum import Enum

class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

class JobOptions(BaseModel):
    """Job configuration options."""
    domain: str
    max_urls: int = 10000
    max_concurrency: int = 2
    allow_residential: bool = False
    enable_firecrawl: bool = False
    firecrawl_api_key: Optional[str] = None
    traffic_budget_mb: int = 100
    residential_limit_mb: int = 50

class CreateJobRequest(BaseModel):
    """Request to create a new job."""
    sitemap_url: Optional[HttpUrl] = None
    sitemap_urls: Optional[List[str]] = None  # Direct URL list
    options: JobOptions

class JobResponse(BaseModel):
    """Job details response."""
    id: str
    domain: str
    status: JobStatus
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    total_urls: int = 0
    success_urls: int = 0
    failed_urls: int = 0
    traffic_mb_used: float = 0.0
    residential_mb_used: float = 0.0
    error_message: Optional[str] = None

class ExportType(str, Enum):
    FULL = "full"
    SEO = "seo"
    DIFF = "diff"

class ExportFormat(str, Enum):
    CSV = "csv"
    XLSX = "xlsx"
    JSON = "json"

class ExportResponse(BaseModel):
    """Export artifact response."""
    id: str
    job_id: str
    type: ExportType
    format: ExportFormat
    url: str  # Download URL
    size_bytes: int
    created_at: datetime
```

**Файл**: [`services/api/queue.py`](services/api/queue.py)

```python
"""RQ (Redis Queue) integration."""
import os
from redis import Redis
from rq import Queue

redis_conn = Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
job_queue = Queue("scraping", connection=redis_conn)

def enqueue_scrape_job(job_id: str, urls: list[str], options: dict) -> str:
    """Enqueue scraping job to worker pool."""
    from services.worker.tasks import scrape_job_task
    
    rq_job = job_queue.enqueue(
        scrape_job_task,
        job_id=job_id,
        urls=urls,
        options=options,
        job_timeout="2h",
        result_ttl=86400,  # 24h
        failure_ttl=604800  # 7d
    )
    return rq_job.id
```

**Файл**: [`services/api/routes/jobs.py`](services/api/routes/jobs.py)

```python
"""Job management endpoints."""
from fastapi import APIRouter, Depends, HTTPException, Request
from typing import List
import uuid

from services.api import models, queue
from services.api.dependencies import get_db
from database.manager import DatabaseManager

router = APIRouter()

@router.post("", response_model=models.JobResponse, status_code=201)
async def create_job(
    req: models.CreateJobRequest,
    db: DatabaseManager = Depends(get_db)
):
    """Create a new scraping job."""
    # Parse sitemap or use direct URLs
    if req.sitemap_url:
        # TODO: Parse sitemap (use existing utils/sitemap_parser.py)
        urls = []  # Placeholder
    elif req.sitemap_urls:
        urls = req.sitemap_urls
    else:
        raise HTTPException(400, "Either sitemap_url or sitemap_urls required")
    
    # Create job record
    job_id = str(uuid.uuid4())
    await db.create_job(
        job_id=job_id,
        domain=req.options.domain,
        options=req.options.model_dump(),
        total_urls=len(urls)
    )
    
    # Enqueue to worker
    queue.enqueue_scrape_job(job_id, urls, req.options.model_dump())
    
    # Return job
    job = await db.get_job(job_id)
    return models.JobResponse(**job)

@router.get("/{job_id}", response_model=models.JobResponse)
async def get_job(
    job_id: str,
    db: DatabaseManager = Depends(get_db)
):
    """Get job status."""
    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return models.JobResponse(**job)

@router.post("/{job_id}/cancel", status_code=202)
async def cancel_job(
    job_id: str,
    db: DatabaseManager = Depends(get_db)
):
    """Cancel running job."""
    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    
    if job["status"] not in ["queued", "running"]:
        raise HTTPException(409, f"Cannot cancel job in status: {job['status']}")
    
    # TODO: Send cancel signal to RQ job
    await db.update_job_status(job_id, "cancelled")
    
    return {"ok": True}

@router.get("", response_model=List[models.JobResponse])
async def list_jobs(
    domain: str = None,
    limit: int = 50,
    db: DatabaseManager = Depends(get_db)
):
    """List jobs."""
    jobs = await db.list_jobs(domain=domain, limit=limit)
    return [models.JobResponse(**j) for j in jobs]
```

**Файл**: [`services/api/dependencies.py`](services/api/dependencies.py)

```python
"""FastAPI dependencies."""
from fastapi import Request
from database.manager import DatabaseManager

async def get_db(request: Request) -> DatabaseManager:
    """Get database manager from app state."""
    return request.app.state.db
```

**Файл**: [`services/api/requirements.txt`](services/api/requirements.txt)

```
fastapi==0.115.0
uvicorn[standard]==0.32.0
pydantic-settings==2.6.0
redis==5.2.0
rq==2.0.0
psycopg2-binary==2.9.10
asyncpg==0.30.0
```

### 2.3. Health & SSE Endpoints

**Файл**: [`services/api/routes/health.py`](services/api/routes/health.py)

```python
"""Health check endpoint."""
from fastapi import APIRouter, Depends
from services.api.dependencies import get_db

router = APIRouter()

@router.get("/health")
async def health_check(db = Depends(get_db)):
    """Health check with dependencies."""
    # Test DB
    try:
        await db.execute("SELECT 1")
        db_status = "ok"
    except Exception as e:
        db_status = f"error: {e}"
    
    return {
        "status": "ok",
        "database": db_status
    }
```

**Файл**: [`services/api/routes/sse.py`](services/api/routes/sse.py)

```python
"""Server-Sent Events for live logs."""
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from services.api.dependencies import get_db
import asyncio
import json

router = APIRouter()

@router.get("/jobs/{job_id}/stream")
async def stream_job_logs(job_id: str, db = Depends(get_db)):
    """Stream job logs via SSE."""
    
    async def event_generator():
        """Generate SSE events."""
        # TODO: Subscribe to Redis pub/sub channel for job logs
        # For now, emit placeholder events
        for i in range(5):
            event = {
                "type": "progress",
                "data": {
                    "job_id": job_id,
                    "message": f"Processing URL {i+1}/5",
                    "progress": (i+1) * 20
                }
            }
            yield f"data: {json.dumps(event)}\n\n"
            await asyncio.sleep(1)
        
        yield f"data: {json.dumps({'type': 'complete'})}\n\n"
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }
    )
```

**Acceptance Criteria Phase 2**:
- ✅ FastAPI сервис стартует на порту 8000
- ✅ `POST /api/jobs` создаёт job и enqueue в Redis
- ✅ `GET /api/jobs/:id` возвращает статус
- ✅ `POST /api/jobs/:id/cancel` отменяет job
- ✅ `GET /api/health` проверяет DB connection
- ✅ CORS настроен для `http://localhost:3000`

---

## 🔧 Phase 3: Worker Pool Service (Неделя 2, дни 1-3)

### 3.1. Структура `services/worker/`

```
services/worker/
├── worker.py            # RQ worker entry point
├── tasks.py             # Task definitions
├── job_executor.py      # Orchestrator for scrape job
└── requirements.txt
```

### 3.2. Core Files

**Файл**: [`services/worker/worker.py`](services/worker/worker.py)

```python
"""RQ Worker process."""
import os
import sys
from pathlib import Path
from redis import Redis
from rq import Worker

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

def main():
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    redis_conn = Redis.from_url(redis_url)
    
    worker = Worker(
        ["scraping"],
        connection=redis_conn,
        name=f"worker-{os.getpid()}"
    )
    
    print(f"🚀 Worker started: {worker.name}")
    worker.work(with_scheduler=True)

if __name__ == "__main__":
    main()
```

**Файл**: [`services/worker/tasks.py`](services/worker/tasks.py)

```python
"""RQ task definitions."""
import asyncio
from services.worker.job_executor import JobExecutor

def scrape_job_task(job_id: str, urls: list[str], options: dict):
    """Main scraping task (executed by RQ worker)."""
    print(f"[RQ Task] Starting job {job_id} with {len(urls)} URLs")
    
    executor = JobExecutor(job_id, urls, options)
    
    # Run async executor in sync context
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        result = loop.run_until_complete(executor.run())
        return result
    finally:
        loop.close()
```

**Файл**: [`services/worker/job_executor.py`](services/worker/job_executor.py)

```python
"""Job execution orchestrator."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from database.manager import DatabaseManager
from core.scraper_engine import ScraperEngine
from utils.export_writers import ExportWriter
import os

class JobExecutor:
    """Orchestrates scraping job execution."""
    
    def __init__(self, job_id: str, urls: list[str], options: dict):
        self.job_id = job_id
        self.urls = urls
        self.options = options
        self.db = DatabaseManager(os.getenv("DATABASE_URL"))
    
    async def run(self) -> dict:
        """Execute job."""
        await self.db.init_pool()
        
        try:
            # Update status to running
            await self.db.update_job_status(self.job_id, "running")
            
            # Initialize scraper
            scraper = ScraperEngine(
                domain=self.options["domain"],
                max_workers=self.options.get("max_concurrency", 2)
            )
            
            results = []
            
            # Scrape URLs
            for i, url in enumerate(self.urls):
                print(f"[JobExecutor] Processing {i+1}/{len(self.urls)}: {url}")
                
                result = await scraper.scrape_url(url)
                results.append(result)
                
                # Store result
                await self.db.insert_page_result(self.job_id, {
                    "url": url,
                    "http_status": result.get("status_code"),
                    "data_full": result.get("data"),
                    "strategy_used": result.get("strategy"),
                    "bytes_in": len(result.get("html", ""))
                })
                
                # Update progress
                await self.db.update_job_status(
                    self.job_id,
                    "running",
                    success_urls=i+1
                )
            
            # Generate exports
            await self.generate_exports(results)
            
            # Mark as succeeded
            await self.db.update_job_status(self.job_id, "succeeded")
            
            return {"status": "success", "results_count": len(results)}
            
        except Exception as e:
            print(f"[JobExecutor] Error: {e}")
            await self.db.update_job_status(
                self.job_id,
                "failed",
                error_message=str(e)
            )
            raise
        
        finally:
            await self.db.close()
    
    async def generate_exports(self, results: list):
        """Generate CSV/XLSX exports."""
        # TODO: Use ExportWriter to generate full.csv, seo.csv, diff.csv
        # Register in database.exports
        pass
```

**Файл**: [`services/worker/requirements.txt`](services/worker/requirements.txt)

```
redis==5.2.0
rq==2.0.0
psycopg2-binary==2.9.10
asyncpg==0.30.0
httpx==0.27.2
playwright==1.48.0
```

**Acceptance Criteria Phase 3**:
- ✅ Worker подключается к Redis и слушает очередь `scraping`
- ✅ Task `scrape_job_task` выполняется и обновляет статус job
- ✅ Результаты сохраняются в `pages` таблицу
- ✅ При ошибке job помечается как `failed`
- ✅ Можно запустить несколько workers параллельно

---

## 🔌 Phase 4: Integration & Testing (Неделя 2, дни 4-5)

### 4.1. FlareSolverr Integration

**Файл**: [`core/flaresolverr_client.py`](core/flaresolverr_client.py)

```python
"""FlareSolverr client for Cloudflare bypass."""
import httpx
from typing import Optional

class FlareSolverrClient:
    """Client for FlareSolverr service."""
    
    def __init__(self, endpoint: str = "http://localhost:8191"):
        self.endpoint = endpoint
        self.client = httpx.AsyncClient(timeout=60.0)
    
    async def solve(self, url: str, max_timeout: int = 60000) -> Optional[dict]:
        """
        Solve Cloudflare challenge.
        
        Returns:
            {
                "status": "ok",
                "solution": {
                    "url": str,
                    "status": int,
                    "response": str (HTML),
                    "cookies": list
                }
            }
        """
        payload = {
            "cmd": "request.get",
            "url": url,
            "maxTimeout": max_timeout
        }
        
        try:
            response = await self.client.post(f"{self.endpoint}/v1", json=payload)
            response.raise_for_status()
            data = response.json()
            
            if data.get("status") == "ok":
                return data["solution"]
            
            return None
            
        except Exception as e:
            print(f"[FlareSolverr] Error: {e}")
            return None
    
    async def close(self):
        await self.client.aclose()
```

**Обновление**: [`core/scraper_engine.py`](core/scraper_engine.py)

```python
# Добавить FlareSolverr в fallback chain

from core.flaresolverr_client import FlareSolverrClient

class ScraperEngine:
    def __init__(self, ...):
        ...
        self.flaresolverr = FlareSolverrClient(
            os.getenv("FLARESOLVERR_URL", "http://localhost:8191")
        )
    
    async def scrape_url(self, url: str) -> dict:
        # ... existing httpx/playwright logic ...
        
        # Add FlareSolverr fallback
        if result.get("blocked") or result.get("status_code") == 403:
            print(f"[Scraper] Trying FlareSolverr for {url}")
            solution = await self.flaresolverr.solve(url)
            
            if solution:
                return {
                    "url": url,
                    "status_code": solution["status"],
                    "html": solution["response"],
                    "strategy": "flaresolverr"
                }
```

### 4.2. Export Schema Compliance

**Файл**: [`utils/export_schema.py`](utils/export_schema.py)

```python
"""PRD-compliant export schemas."""
from typing import TypedDict, Optional

class FullCSVRow(TypedDict):
    """Schema for full.csv (PRD §1.9)."""
    url: str
    final_url: Optional[str]
    http_status: int
    fetched_at: str  # ISO 8601
    title: Optional[str]
    h1: Optional[str]
    price: Optional[float]
    currency: Optional[str]
    availability: Optional[str]
    sku: Optional[str]
    brand: Optional[str]
    category: Optional[str]
    breadcrumbs: Optional[str]
    images: Optional[str]  # pipe-separated
    attrs_json: Optional[str]  # JSON string
    text_hash: Optional[str]  # SHA-256

class SEOCSVRow(TypedDict):
    """Schema for seo.csv (PRD §1.9)."""
    url: str
    fetched_at: str
    title: Optional[str]
    meta_description: Optional[str]
    h1: Optional[str]
    og_title: Optional[str]
    og_description: Optional[str]
    og_image: Optional[str]
    twitter_title: Optional[str]
    twitter_description: Optional[str]
    canonical: Optional[str]
    robots: Optional[str]
    hreflang: Optional[str]
    images_alt_joined: Optional[str]

class DiffCSVRow(TypedDict):
    """Schema for diff.csv (PRD §1.9)."""
    url: str
    prev_crawl_at: Optional[str]
    curr_crawl_at: str
    change_type: str  # ADDED, REMOVED, MODIFIED, UNCHANGED
    fields_changed: Optional[str]  # semicolon-separated
    price_prev: Optional[float]
    price_curr: Optional[float]
    availability_prev: Optional[str]
    availability_curr: Optional[str]
    title_prev: Optional[str]
    title_curr: Optional[str]
```

**Обновление**: [`utils/export_writers.py`](utils/export_writers.py)

```python
# Refactor to use FullCSVRow, SEOCSVRow, DiffCSVRow schemas
# Add missing fields: final_url, http_status, currency, etc.
```

### 4.3. End-to-End Test

**Файл**: [`tests/integration/test_full_pipeline.py`](tests/integration/test_full_pipeline.py)

```python
"""Integration test: full scraping pipeline."""
import pytest
import httpx
import asyncio
from uuid import uuid4

@pytest.mark.asyncio
async def test_full_job_pipeline():
    """Test: create job → worker processes → exports generated."""
    
    # 1. Create job via API
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "http://localhost:8000/api/jobs",
            json={
                "sitemap_urls": [
                    "https://example.com/product1",
                    "https://example.com/product2"
                ],
                "options": {
                    "domain": "example.com",
                    "max_concurrency": 1
                }
            }
        )
        assert response.status_code == 201
        job = response.json()
        job_id = job["id"]
    
    # 2. Wait for job to complete (max 60s)
    for _ in range(60):
        await asyncio.sleep(1)
        response = await client.get(f"http://localhost:8000/api/jobs/{job_id}")
        job = response.json()
        
        if job["status"] in ["succeeded", "failed"]:
            break
    
    assert job["status"] == "succeeded"
    assert job["success_urls"] >= 1
    
    # 3. Check exports
    response = await client.get(f"http://localhost:8000/api/jobs/{job_id}/exports")
    exports = response.json()
    
    assert any(e["type"] == "full" and e["format"] == "csv" for e in exports)
    assert any(e["type"] == "seo" and e["format"] == "csv" for e in exports)
```

**Acceptance Criteria Phase 4**:
- ✅ FlareSolverr интегрирован в fallback chain
- ✅ Export schemas соответствуют PRD §1.9
- ✅ E2E test проходит: job создаётся, обрабатывается, генерирует экспорты
- ✅ Dashboard может отображать статус job через новый API

---

## 📦 Deployment Updates

### Docker Compose

**Обновить**: [`docker-compose.yml`](docker-compose.yml)

```yaml
services:
  # ... existing services (postgres, redis, flaresolverr, minio) ...
  
  api:
    build:
      context: .
      dockerfile: network/NEW_PROJECT/Вайбкодинг Скрипт Dockerfile.backend
    container_name: scraper_api
    environment:
      DATABASE_URL: postgresql://scraper:scraper@postgres:5432/scraper
      REDIS_URL: redis://redis:6379/0
      FLARESOLVERR_URL: http://flaresolverr:8191
    ports:
      - "8000:8000"
    command: ["uvicorn", "services.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
  
  worker:
    build:
      context: .
      dockerfile: network/NEW_PROJECT/Вайбкодинг Скрипт Dockerfile.backend
    container_name: scraper_worker
    environment:
      DATABASE_URL: postgresql://scraper:scraper@postgres:5432/scraper
      REDIS_URL: redis://redis:6379/0
      FLARESOLVERR_URL: http://flaresolverr:8191
    command: ["python", "services/worker/worker.py"]
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    deploy:
      replicas: 2  # Run 2 workers
```

### Dockerfile Updates

**Обновить**: [`network/NEW_PROJECT/Вайбкодинг Скрипт Dockerfile.backend`](network/NEW_PROJECT/Вайбкодинг Скрипт Dockerfile.backend)

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Copy requirements
COPY services/api/requirements.txt /app/services/api/
COPY services/worker/requirements.txt /app/services/worker/

# Install deps
RUN pip install --no-cache-dir \
    -r services/api/requirements.txt \
    -r services/worker/requirements.txt

# Copy source
COPY . /app

EXPOSE 8000

CMD ["uvicorn", "services.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

---

## 📊 Success Metrics

| Метрика | Цель | Способ измерения |
|---------|------|------------------|
| Архитектурная готовность | 100% | Все компоненты из Architecture.md реализованы |
| Job idempotency | ✅ | Тест: cancel job → resume → same results |
| Export compliance | 100% | Все колонки из PRD §1.9 присутствуют |
| E2E test success | ✅ | `test_full_pipeline.py` проходит |
| FlareSolverr success rate | ≥80% | Метрика `flaresolverr_success_rate` |
| Deployment | ✅ | `make up` запускает все сервисы healthcheck green |

---

## 🚦 Implementation Phases Summary

| Phase | Scope | Duration | Status |
|-------|-------|----------|--------|
| 1 | Database Schema & Migrations | 2 дня | 🟡 Pending |
| 2 | FastAPI Backend Service | 3 дня | 🟡 Pending |
| 3 | Worker Pool Service | 3 дня | 🟡 Pending |
| 4 | Integration & Testing | 2 дня | 🟡 Pending |

**Total**: ~2 недели для достижения MVP compliance.

---

## 📝 Next Steps

1. **Review & Approve** этот план с командой
2. **Create GitHub Issues** для каждой фазы
3. **Assign agents**:
   - Phase 1: `database-admin`
   - Phase 2-3: `backend-architect`, `backend-security-coder`
   - Phase 4: `test-automator`, `code-reviewer`
4. **Start Phase 1** с миграций БД

---

## 📚 References

- [`network/NEW_PROJECT/prd.md`](network/NEW_PROJECT/prd.md) — Product Requirements
- [`network/NEW_PROJECT/Architecture.md`](network/NEW_PROJECT/Architecture.md) — System Architecture
- [`network/NEW_PROJECT/tech_stack_policy.md`](network/NEW_PROJECT/tech_stack_policy.md) — Tech Stack Policies
- Анализ от 2025-10-01: текущая готовность 50%, критические пробелы в orchestration layer