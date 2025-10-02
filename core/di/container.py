from __future__ import annotations

import sqlite3
from functools import lru_cache
from typing import Any, Callable, Dict

from core.db.repository.product_repository import ProductRepository
from core.db.service.product_service import ProductService


class Container:
    def __init__(self) -> None:
        self._providers: Dict[str, Callable[["Container"], Any]] = {}
        self._cache: Dict[str, Any] = {}

    def register(self, key: str, provider: Callable[["Container"], Any]) -> None:
        self._providers[key] = provider

    def resolve(self, key: str) -> Any:
        if key in self._cache:
            return self._cache[key]
        provider = self._providers[key]
        instance = provider(self)
        self._cache[key] = instance
        return instance


@lru_cache(maxsize=1)
def build_container(db_path: str = ":memory:") -> Container:
    container = Container()

    container.register("db_conn", lambda _: sqlite3.connect(db_path))
    container.register("product_repository", lambda c: ProductRepository(c.resolve("db_conn")))
    container.register("product_service", lambda c: ProductService(c.resolve("product_repository")))

    return container
