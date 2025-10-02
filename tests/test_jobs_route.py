"""Tests for job management API routes."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
import sys
import types
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Provide lightweight stubs for optional infrastructure dependencies.
# ---------------------------------------------------------------------------

if "redis" not in sys.modules:  # pragma: no cover - executed only when dependency missing
    redis_module = types.ModuleType("redis")

    class _Redis:
        @classmethod
        def from_url(cls, _url: str) -> "_Redis":
            return cls()

    redis_module.Redis = _Redis
    sys.modules["redis"] = redis_module

if "rq" not in sys.modules:  # pragma: no cover - executed only when dependency missing
    rq_module = types.ModuleType("rq")

    class _Queue:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.enqueued: List[Dict[str, Any]] = []

        def enqueue(self, *_args: Any, **kwargs: Any):
            self.enqueued.append({"args": _args, "kwargs": kwargs})
            return types.SimpleNamespace(id="rq-job-id")

    rq_module.Queue = _Queue
    sys.modules["rq"] = rq_module


from services.api.dependencies import get_db  # noqa: E402  pylint: disable=wrong-import-position
from services.api.routes.jobs import router as jobs_router  # noqa: E402
from services.api import queue as queue_module  # noqa: E402


class InMemoryDB:
    """Simple in-memory substitute for DatabaseManager."""

    def __init__(self) -> None:
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self.create_calls: List[Dict[str, Any]] = []
        self.update_calls: List[Dict[str, Any]] = []

    async def create_job(
        self,
        *,
        job_id: str,
        domain: str,
        options: Dict[str, Any],
        total_urls: int,
    ) -> None:
        now = datetime.now(timezone.utc)
        self._jobs[job_id] = {
            "id": job_id,
            "domain": domain,
            "status": "queued",
            "created_at": now,
            "started_at": None,
            "finished_at": None,
            "total_urls": total_urls,
            "success_urls": 0,
            "failed_urls": 0,
            "traffic_mb_used": 0.0,
            "residential_mb_used": 0.0,
            "error_message": None,
            "options_snapshot": options,
        }
        self.create_calls.append(
            {
                "job_id": job_id,
                "domain": domain,
                "options": options,
                "total_urls": total_urls,
            }
        )

    async def get_job(self, job_id: str) -> Dict[str, Any] | None:
        job = self._jobs.get(job_id)
        if not job:
            return None
        return dict(job)

    async def update_job_status(self, job_id: str, status: str) -> None:
        job = self._jobs.get(job_id)
        if job:
            job["status"] = status
            if status == "cancelled":
                job["finished_at"] = datetime.now(timezone.utc)
        self.update_calls.append({"job_id": job_id, "status": status})

    async def list_jobs(self, *, domain: str | None = None, limit: int = 50) -> List[Dict[str, Any]]:
        jobs = list(self._jobs.values())
        if domain:
            jobs = [job for job in jobs if job["domain"] == domain]
        jobs.sort(key=lambda item: item["created_at"], reverse=True)
        return [dict(job) for job in jobs[:limit]]


def _create_app(db: InMemoryDB) -> FastAPI:
    app = FastAPI()

    async def _db_override():
        return db

    app.dependency_overrides[get_db] = _db_override
    app.include_router(jobs_router, prefix="/api/jobs")
    return app


def test_create_job_with_direct_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    db = InMemoryDB()

    captured: Dict[str, Any] = {}

    def _fake_enqueue(job_id: str, urls: List[str], options: Dict[str, Any]) -> str:
        captured.update({"job_id": job_id, "urls": urls, "options": options})
        return "rq-123"

    monkeypatch.setattr(queue_module, "enqueue_scrape_job", _fake_enqueue)

    fixed_uuid = "123e4567-e89b-12d3-a456-426614174000"
    monkeypatch.setattr("services.api.routes.jobs.uuid.uuid4", lambda: fixed_uuid)

    app = _create_app(db)

    payload = {
        "sitemap_urls": ["https://example.com/page1", "https://example.com/page2"],
        "options": {
            "domain": "example.com",
            "max_urls": 10,
            "max_concurrency": 2,
            "allow_residential": False,
            "enable_firecrawl": False,
            "firecrawl_api_key": None,
            "traffic_budget_mb": 100,
            "residential_limit_mb": 50,
        },
    }

    with TestClient(app) as client:
        response = client.post("/api/jobs", json=payload)

    assert response.status_code == 201
    body = response.json()
    assert body["id"] == fixed_uuid
    assert body["domain"] == "example.com"
    assert body["status"] == "queued"
    assert body["total_urls"] == 2
    assert captured == {
        "job_id": fixed_uuid,
        "urls": payload["sitemap_urls"],
        "options": payload["options"],
    }


def test_create_job_requires_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    db = InMemoryDB()
    monkeypatch.setattr(queue_module, "enqueue_scrape_job", lambda *_args, **_kwargs: "rq")
    monkeypatch.setattr("services.api.routes.jobs.uuid.uuid4", lambda: "uuid")

    app = _create_app(db)

    with TestClient(app) as client:
        response = client.post(
            "/api/jobs",
            json={
                "sitemap_urls": None,
                "options": {
                    "domain": "example.com",
                    "max_urls": 10,
                    "max_concurrency": 2,
                    "allow_residential": False,
                    "enable_firecrawl": False,
                    "firecrawl_api_key": None,
                    "traffic_budget_mb": 100,
                    "residential_limit_mb": 50,
                },
            },
        )

    assert response.status_code == 400
    assert "Either sitemap_url or sitemap_urls required" in response.text


def test_create_job_with_sitemap_url_returns_501(monkeypatch: pytest.MonkeyPatch) -> None:
    db = InMemoryDB()
    monkeypatch.setattr(queue_module, "enqueue_scrape_job", lambda *_args, **_kwargs: "rq")
    app = _create_app(db)

    with TestClient(app) as client:
        response = client.post(
            "/api/jobs",
            json={
                "sitemap_url": "https://example.com/sitemap.xml",
                "options": {
                    "domain": "example.com",
                    "max_urls": 10,
                    "max_concurrency": 2,
                    "allow_residential": False,
                    "enable_firecrawl": False,
                    "firecrawl_api_key": None,
                    "traffic_budget_mb": 100,
                    "residential_limit_mb": 50,
                },
            },
        )

    assert response.status_code == 501


def test_get_job_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    db = InMemoryDB()
    monkeypatch.setattr(queue_module, "enqueue_scrape_job", lambda *_args, **_kwargs: "rq")
    app = _create_app(db)

    with TestClient(app) as client:
        response = client.get("/api/jobs/missing")

    assert response.status_code == 404
    assert response.json() == {"detail": "Job not found"}


def test_cancel_job_success(monkeypatch: pytest.MonkeyPatch) -> None:
    db = InMemoryDB()
    job_id = "job-1"

    import asyncio

    asyncio.run(
        db.create_job(
            job_id=job_id,
            domain="example.com",
            options={},
            total_urls=1,
        )
    )

    monkeypatch.setattr(queue_module, "enqueue_scrape_job", lambda *_args, **_kwargs: "rq")
    app = _create_app(db)

    with TestClient(app) as client:
        response = client.post(f"/api/jobs/{job_id}/cancel")

    assert response.status_code == 202
    assert response.json() == {"ok": True}
    assert db._jobs[job_id]["status"] == "cancelled"


def test_cancel_job_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    db = InMemoryDB()

    import asyncio

    asyncio.run(
        db.create_job(
            job_id="job-2",
            domain="example.com",
            options={},
            total_urls=1,
        )
    )
    db._jobs["job-2"]["status"] = "succeeded"

    monkeypatch.setattr(queue_module, "enqueue_scrape_job", lambda *_args, **_kwargs: "rq")
    app = _create_app(db)

    with TestClient(app) as client:
        response = client.post("/api/jobs/job-2/cancel")

    assert response.status_code == 409
    assert response.json() == {"detail": "Cannot cancel job in status: succeeded"}


def test_list_jobs_applies_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    db = InMemoryDB()

    import asyncio

    async def _populate() -> None:
        for index in range(150):
            job_id = f"job-{index}"
            await db.create_job(
                job_id=job_id,
                domain="example.com" if index % 2 == 0 else "other.com",
                options={},
                total_urls=index,
            )
            db._jobs[job_id]["created_at"] -= timedelta(seconds=index)

    asyncio.run(_populate())

    monkeypatch.setattr(queue_module, "enqueue_scrape_job", lambda *_args, **_kwargs: "rq")
    app = _create_app(db)

    with TestClient(app) as client:
        response = client.get("/api/jobs", params={"limit": 200, "domain": "example.com"})

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 75
    domains = {item["domain"] for item in data}
    assert domains == {"example.com"}
