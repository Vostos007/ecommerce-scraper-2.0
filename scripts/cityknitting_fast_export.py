"""Demo exporter for Sitting Knitting."""

from __future__ import annotations

from .demo_export import run_demo_export


def main() -> None:
    run_demo_export('sittingknitting.ru', default_total=60)


def run_export() -> None:
    main()


if __name__ == '__main__':
    main()
