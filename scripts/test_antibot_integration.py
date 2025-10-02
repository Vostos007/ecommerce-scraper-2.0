#!/usr/bin/env python3
"""Run integration checks for the antibot stack with optional FlareSolverr bypass."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

if __package__ is None or __package__ == "":  # pragma: no cover - direct execution support
    sys.path.append(str(Path(__file__).resolve().parents[1]))
from typing import Any, Dict

from core.antibot_manager import AntibotManager
from core.flaresolverr_client import FlareSolverrClient


def load_config(config_path: Path) -> Dict[str, Any]:
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"Configuration file not found: {config_path}")


async def check_flaresolverr(config: Dict[str, Any]) -> int:
    flaresolverr_cfg = config.get("flaresolverr", {})
    client = FlareSolverrClient(flaresolverr_cfg)
    healthy = await client.health_check()
    if healthy:
        print(f"FlareSolverr reachable at {client.endpoint}")
        return 0
    print("FlareSolverr health check failed")
    return 1


async def run_request(config_path: Path, url: str, method: str) -> int:
    manager = AntibotManager(str(config_path))
    try:
        await manager.start()
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to start AntibotManager: {exc}", file=sys.stderr)
        return 2

    try:
        response = await manager.make_ethical_request(url, method=method.upper())
        if not response:
            print("Request failed or returned no content", file=sys.stderr)
            return 1
        status = response.get("status")
        content_preview = (response.get("content") or "")[:200]
        print(f"HTTP {status} from antibot pipeline ({len(content_preview)} chars preview)")
        return 0
    finally:
        # Attempt to gracefully close proxy/session resources if available
        try:
            if manager.proxy_rotator and hasattr(manager.proxy_rotator, "close"):
                await manager.proxy_rotator.close()
        except Exception:  # noqa: BLE001
            pass


async def main_async(args: argparse.Namespace) -> int:
    config_path = Path(args.config).resolve()
    config = load_config(config_path)

    if args.command == "health":
        return await check_flaresolverr(config)
    if args.command == "request":
        return await run_request(config_path, args.url, args.method)
    print("Unknown command", file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Antibot integration smoke tests")
    parser.add_argument(
        "command",
        choices=["health", "request"],
        help="health: check FlareSolverr availability; request: run a guarded request",
    )
    parser.add_argument(
        "--config",
        default="config/settings.json",
        help="Path to scraper configuration file",
    )
    parser.add_argument(
        "--url",
        default="https://6wool.ru/catalog/pryazha/jawoll-magic-degrade/",
        help="Target URL when running the request command",
    )
    parser.add_argument(
        "--method",
        default="GET",
        help="HTTP method to use for the request command",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        exit_code = asyncio.run(main_async(args))
    except KeyboardInterrupt:
        exit_code = 130
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
