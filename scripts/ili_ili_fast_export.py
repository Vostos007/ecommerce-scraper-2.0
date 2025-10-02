"""Demo exporter for Ili-ili."""

from __future__ import annotations

from .demo_export import run_demo_export


def main() -> None:
    run_demo_export('ili-ili.com', default_total=55)


def run_export() -> None:
    main()


if __name__ == '__main__':
    main()
