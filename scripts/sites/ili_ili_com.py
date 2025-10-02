#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

if __package__ is None or __package__ == "":  # pragma: no cover
    sys.path.append(str(Path(__file__).resolve().parents[2]))

from scripts.site_runner import run_site_cli  # noqa: E402


if __name__ == "__main__":
    default_args = sys.argv[1:] or ["--batch-size", "100"]
    run_site_cli("ili-ili.com", display_name="Ili-Ili", argv=default_args)
