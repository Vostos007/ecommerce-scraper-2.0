"""Utility helpers to ensure a local FlareSolverr instance is available."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from typing import Optional

import httpx

LOGGER = logging.getLogger(__name__)


def _is_truthy(value: str) -> bool:
    return value.lower() in {"1", "true", "yes", "on"}


def _check_health(url: str, timeout: float) -> bool:
    try:
        response = httpx.get(url.rstrip("/") + "/health", timeout=timeout)
        if response.status_code == 200 and "\"status\"" in response.text:
            return '"status"' in response.text and '"ok"' in response.text
    except httpx.HTTPError:
        return False
    return False


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        subprocess.run(
            ["docker", "info"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _compose_up(compose_file: str) -> bool:
    if shutil.which("docker-compose") is None:
        LOGGER.warning("FlareSolverr auto-start skipped: docker-compose not found")
        return False
    try:
        subprocess.run(
            ["docker-compose", "-f", compose_file, "up", "-d"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except subprocess.CalledProcessError as exc:
        LOGGER.warning("Failed to start FlareSolverr via docker-compose: %s", exc)
        return False


def ensure_flaresolverr(
    *,
    url: Optional[str] = None,
    compose_file: Optional[str] = None,
    wait_seconds: Optional[int] = None,
    auto_start: Optional[bool] = None,
) -> bool:
    """Ensure FlareSolverr is reachable.

    Returns ``True`` if the service is available or successfully started, otherwise
    returns ``False`` without raising.
    """

    url = url or os.getenv("FLARESOLVERR_URL", "http://localhost:8192")
    compose_file = compose_file or os.getenv(
        "FLARESOLVERR_COMPOSE_FILE", "docker-compose.flaresolverr.yml"
    )
    wait_seconds = wait_seconds or int(os.getenv("FLARESOLVERR_WAIT_SECONDS", "30"))
    auto_start = (
        _is_truthy(os.getenv("FLARESOLVERR_AUTO_START", "true"))
        if auto_start is None
        else auto_start
    )

    if _check_health(url, timeout=2):
        return True

    if not auto_start:
        LOGGER.debug("FlareSolverr auto-start disabled; skipping availability check")
        return False

    if not _docker_available():
        LOGGER.warning("Cannot auto-start FlareSolverr: Docker daemon unavailable")
        return False

    LOGGER.info("FlareSolverr health check failed; attempting to start container...")

    if not _compose_up(compose_file):
        return False

    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if _check_health(url, timeout=2):
            LOGGER.info("FlareSolverr is ready at %s", url)
            return True
        time.sleep(1)

    LOGGER.warning(
        "FlareSolverr did not report status=ok within %s seconds", wait_seconds
    )
    return False


__all__ = ["ensure_flaresolverr"]
