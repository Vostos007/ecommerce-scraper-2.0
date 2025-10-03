#!/usr/bin/env python3
"""Collect proxy infrastructure metrics for the dashboard."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import sys

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from network.NEW_PROJECT.proxy_stats.collector import collect_proxy_stats  # noqa: E402


async def gather_stats() -> Dict[str, Any]:
    """Gather proxy statistics using the new metrics collector."""
    stats = collect_proxy_stats(ROOT)
    generated = stats.get("generated_at")
    if not generated:
        stats["generated_at"] = datetime.now(timezone.utc).isoformat()
    return stats


def main() -> int:
    try:
        stats = asyncio.run(gather_stats())
    except Exception as exc:  # noqa: BLE001 - surface the error in response payload
        print(json.dumps({"error": str(exc)}))
        return 1

    print(json.dumps(stats, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
