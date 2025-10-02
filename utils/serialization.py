"""Shared helpers for serialising complex objects to JSON."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any


def _looks_like_rename_action(value: Any) -> bool:
    return all(hasattr(value, attr) for attr in ("source", "target_name", "target_path"))


def prepare_for_json(value: Any) -> Any:
    """Recursively normalise objects into JSON-serialisable primitives."""

    if isinstance(value, dict):
        return {str(key): prepare_for_json(val) for key, val in value.items()}

    if isinstance(value, (list, tuple, set)):
        return [prepare_for_json(item) for item in value]

    if isinstance(value, Path):
        return str(value)

    if is_dataclass(value):
        return prepare_for_json(asdict(value))

    if _looks_like_rename_action(value):
        return {
            "operation": "rename",
            "source": str(getattr(value, "source")),
            "target_name": getattr(value, "target_name"),
            "target_path": str(getattr(value, "target_path")),
        }

    return value


def json_dumps(
    value: Any,
    *,
    ensure_ascii: bool = False,
    sort_keys: bool = False,
    indent: int | None = None,
) -> str:
    """Serialise ``value`` to JSON after normalisation."""

    normalised = prepare_for_json(value)
    return json.dumps(normalised, ensure_ascii=ensure_ascii, sort_keys=sort_keys, indent=indent)


__all__ = ["prepare_for_json", "json_dumps"]
