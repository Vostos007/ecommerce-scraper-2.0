"""Atmosphere Store export entry point."""

from __future__ import annotations

from .export_runner import run_export


def main() -> None:
    run_export('atmospherestore.ru')


def run_export() -> None:
    main()


if __name__ == '__main__':
    main()
