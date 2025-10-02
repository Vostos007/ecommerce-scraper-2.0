"""Utility helpers for running export scripts in the demo stack."""
from __future__ import annotations

import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data" / "sites"

PROGRESS_EVENT = "progress"
COMPLETE_EVENT = "complete"

MIN_DELAY = float(os.environ.get("EXPORT_MIN_DELAY", 0.05))
MAX_DELAY = float(os.environ.get("EXPORT_MAX_DELAY", 0.2))


def _emit(event: str, **payload: object) -> None:
    message = {"event": event, **payload, "timestamp": time.time()}
    json.dump(message, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    sys.stdout.flush()


def _load_urls(domain: str) -> list[str]:
    map_path = DATA_DIR / domain / "maps" / f"{domain}.URL-map.json"
    if not map_path.exists():
        raise FileNotFoundError(f"Map file не найден: {map_path}")
    raw = json.loads(map_path.read_text(encoding="utf-8"))
    links = raw.get("links")
    if not isinstance(links, list):
        raise ValueError(f"Некорректный формат карты сайта: {map_path}")
    urls: list[str] = []
    for entry in links:
        if isinstance(entry, str):
            urls.append(entry)
        elif isinstance(entry, dict) and isinstance(entry.get("url"), str):
            urls.append(entry["url"])
    return urls


def _iterate(urls: list[str]) -> Iterable[tuple[int, str, int]]:
    total = len(urls)
    for index, url in enumerate(urls, start=1):
        time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
        yield index, url, total


def run_export(domain: str, *, resume: bool = True, concurrency: int | None = None) -> None:
    urls = _load_urls(domain)
    total = len(urls)

    _emit(PROGRESS_EVENT, site=domain, processed=0, total=total, resume=resume, concurrency=concurrency)

    processed = 0
    for processed, url, total in _iterate(urls):
        _emit(
            PROGRESS_EVENT,
            site=domain,
            processed=processed,
            total=total,
            current_url=url,
            resume=resume,
            concurrency=concurrency,
        )

    _emit(
        COMPLETE_EVENT,
        site=domain,
        processed=processed,
        total=total,
        resume=resume,
        concurrency=concurrency,
    )


__all__ = ["run_export"]
