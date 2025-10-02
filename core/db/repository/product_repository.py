from __future__ import annotations

import sqlite3
from typing import Iterable, List, Optional, Tuple


class ProductRepository:
    """Thin CRUD слой над таблицей product."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._ensure_table()

    def _ensure_table(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS product (
                sku TEXT PRIMARY KEY,
                price REAL NOT NULL,
                available INTEGER NOT NULL
            )
            """
        )
        self._conn.commit()

    def upsert_many(self, rows: Iterable[Tuple[str, float, bool]]) -> int:
        payload = [(sku, price, 1 if available else 0) for sku, price, available in rows]
        self._conn.executemany(
            """
            INSERT INTO product (sku, price, available)
            VALUES (?, ?, ?)
            ON CONFLICT(sku) DO UPDATE SET
                price = excluded.price,
                available = excluded.available
            """,
            payload,
        )
        self._conn.commit()
        return self._conn.total_changes

    def get(self, sku: str) -> Optional[Tuple[str, float, bool]]:
        cur = self._conn.execute(
            "SELECT sku, price, available FROM product WHERE sku = ?",
            (sku,),
        )
        row = cur.fetchone()
        if not row:
            return None
        sku, price, available = row
        return sku, price, bool(available)

    def all(self) -> List[Tuple[str, float, bool]]:
        cur = self._conn.execute("SELECT sku, price, available FROM product")
        return [(sku, price, bool(available)) for sku, price, available in cur.fetchall()]
