"""Demo exporter for Atmosphere Store."""

from __future__ import annotations

from .demo_export import run_demo_export


def main() -> None:
    run_demo_export('atmospherestore.ru', default_total=80)


def run_export() -> None:  # API совместимость со старыми скриптами
    main()


if __name__ == '__main__':
    main()
