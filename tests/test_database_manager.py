"""Unit tests for DatabaseManager using async stubs."""

from pathlib import Path
import sys
import types
from typing import Any

import pytest


class _DummyConnection:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, query: str, *args: Any) -> str:
        self.executed.append((query, args))
        return "OK"

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.executed.append((query, args))
        return {"id": "demo"}

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.executed.append((query, args))
        return [{"id": "demo"}]


class _DummyAcquire:
    def __init__(self, connection: "_DummyConnection") -> None:
        self.connection = connection

    async def __aenter__(self) -> "_DummyConnection":
        return self.connection

    async def __aexit__(self, exc_type, exc, tb) -> None:  # pragma: no cover - noop cleanup
        return None


class _DummyPool:
    def __init__(self) -> None:
        self.connection = _DummyConnection()
        self.closed = False

    def acquire(self) -> _DummyAcquire:
        return _DummyAcquire(self.connection)

    async def close(self) -> None:
        self.closed = True


_fake_asyncpg = types.SimpleNamespace(PostgresError=Exception)


async def _unconfigured_create_pool(*_args, **_kwargs):  # pragma: no cover
    raise AssertionError("asyncpg.create_pool not stubbed in test")


_fake_asyncpg.create_pool = _unconfigured_create_pool

sys.modules.setdefault("asyncpg", _fake_asyncpg)
sys.path.append(str(Path(__file__).resolve().parent.parent))

from database.manager import (  # noqa: E402  pylint: disable=wrong-import-position
    DatabaseManager,
    DatabasePoolNotInitializedError,
)


@pytest.mark.asyncio
async def test_execute_requires_initialized_pool() -> None:
    manager = DatabaseManager("postgresql://user:pass@localhost/db")

    with pytest.raises(DatabasePoolNotInitializedError):
        await manager.execute("SELECT 1")


@pytest.mark.asyncio
async def test_execute_uses_connection_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    dummy_pool = _DummyPool()

    async def _fake_create_pool(*_args, **_kwargs):
        return dummy_pool

    monkeypatch.setattr("asyncpg.create_pool", _fake_create_pool)

    manager = DatabaseManager("postgresql://user:pass@localhost/db")
    await manager.init_pool(min_size=1, max_size=2)

    status = await manager.execute("SELECT 1", 42)
    assert status == "OK"
    assert dummy_pool.connection.executed == [("SELECT 1", (42,))]

    await manager.close()
    assert dummy_pool.closed is True
