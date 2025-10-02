#!/usr/bin/env python3
"""Reorder ili-ili.com cache so priority categories are scraped first."""

from __future__ import annotations

from pathlib import Path

CACHE_PATH = Path("data/sites/ili-ili.com/cache/iliili_urls.txt")
BACKUP_PATH = CACHE_PATH.with_suffix(".all.txt")
PRIORITY_PATH = CACHE_PATH.with_suffix(".priority.txt")

PRIORITY_KEYWORDS = [
    "pryazha",
    "yarn",
    "lang",
    "lana-grossa",
    "lykke",
    "chiaogoo",
    "instrument/spitsy",
    "instrument/spitsi",
    "instrument/spicy",
]


def load_urls(path: Path) -> list[str]:
    if not path.exists():
        raise SystemExit(f"Cache file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        return [line.strip() for line in handle if line.strip()]


def save_urls(path: Path, urls: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(urls) + "\n", encoding="utf-8")


def matches_priority(url: str) -> bool:
    lowered = url.lower()
    return any(keyword in lowered for keyword in PRIORITY_KEYWORDS)


def main() -> None:
    urls = load_urls(CACHE_PATH)
    priority = []
    rest = []
    seen = set()

    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        if matches_priority(url):
            priority.append(url)
        else:
            rest.append(url)

    reordered = priority + rest
    if not reordered:
        raise SystemExit("No URLs found to reorder")

    # backup original
    save_urls(BACKUP_PATH, urls)
    save_urls(PRIORITY_PATH, reordered)
    save_urls(CACHE_PATH, reordered)

    print(f"Total URLs: {len(reordered)}")
    print(f"Priority segment: {len(priority)}")
    print(f"Backup saved to {BACKUP_PATH}")
    print(f"Priority cache saved to {PRIORITY_PATH}")


if __name__ == "__main__":
    main()
