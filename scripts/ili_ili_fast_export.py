"""Ili-ili export entry point."""

from __future__ import annotations

from .export_runner import run_export as _run_export_impl


def main() -> None:
    _run_export_impl('ili-ili.com')


def run_export(*args, **kwargs) -> None:
    """Compatibility wrapper preserving legacy import signature."""
    _run_export_impl('ili-ili.com', *args, **kwargs)


if __name__ == '__main__':
    main()
