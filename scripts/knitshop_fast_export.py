"""Demo exporter for Knitshop."""

from __future__ import annotations

from .demo_export import run_demo_export


def main() -> None:
    run_demo_export('knitshop.ru', default_total=40)


def run_export() -> None:
    main()


if __name__ == '__main__':
    main()
