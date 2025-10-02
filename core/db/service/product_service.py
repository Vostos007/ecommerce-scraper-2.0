from __future__ import annotations

from typing import Iterable, Tuple

from core.db.repository.product_repository import ProductRepository


class ProductService:
    """Business слой вокруг ProductRepository."""

    def __init__(self, repo: ProductRepository) -> None:
        self._repo = repo

    def upsert_variations(self, variations: Iterable[Tuple[str, float, bool]]) -> int:
        return self._repo.upsert_many(variations)

    def get_sku(self, sku: str):
        return self._repo.get(sku)

