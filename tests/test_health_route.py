"""Tests for the health check route."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.api.routes.health import router as health_router
from services.api.dependencies import get_db


class _SuccessfulDB:
    def __init__(self) -> None:
        self.executed_queries: list[str] = []

    async def execute(self, query: str, *args) -> str:
        self.executed_queries.append(query)
        return "OK"


class _FailingDB:
    async def execute(self, query: str, *args) -> str:
        raise RuntimeError("connection refused")


def _create_test_app(db_instance) -> FastAPI:
    app = FastAPI()

    async def _get_db_override():
        return db_instance

    app.dependency_overrides[get_db] = _get_db_override
    app.include_router(health_router, prefix="/api")
    return app


def test_health_check_success() -> None:
    db = _SuccessfulDB()
    app = _create_test_app(db)

    with TestClient(app) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload == {"status": "ok", "database": "ok"}
    assert db.executed_queries == ["SELECT 1"]


def test_health_check_database_failure() -> None:
    app = _create_test_app(_FailingDB())

    with TestClient(app) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["database"].startswith("error: connection refused")
