"""FlareSolverr client abstraction for antibot integrations."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Dict, Optional

import aiohttp


class FlareSolverrError(RuntimeError):
    """Raised when FlareSolverr returns an error response or is unreachable."""


class FlareSolverrClient:
    """Asynchronous client for interacting with a FlareSolverr instance."""

    def __init__(self, config: Dict[str, Any]) -> None:
        self.logger = logging.getLogger(__name__)

        self.enabled: bool = bool(config.get("enabled", False))
        self.endpoint: str = config.get("endpoint", "http://localhost:8192").rstrip("/")
        self.max_timeout_ms: int = int(config.get("max_timeout_ms", 180000))
        self.request_defaults: Dict[str, Any] = config.get("request_defaults", {})
        self.retry_policy: Dict[str, Any] = config.get("retry_policy", {})
        self.session_settings: Dict[str, Any] = config.get("session_management", {})
        self.performance_settings: Dict[str, Any] = config.get("performance_settings", {})

        self._default_timeout_seconds: float = float(
            self.request_defaults.get("timeout_seconds", self.max_timeout_ms / 1000.0)
        )
        self._max_retries: int = int(self.retry_policy.get("max_retries", 2))
        self._retry_delay: float = float(self.retry_policy.get("retry_delay_seconds", 2.0))
        self._backoff_multiplier: float = float(
            self.retry_policy.get("backoff_multiplier", 1.5)
        )

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------
    def is_enabled(self) -> bool:
        return self.enabled

    async def health_check(self) -> bool:
        """Perform a health check against the configured endpoint."""

        if not self.enabled:
            return False

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            ) as session:
                async with session.get(f"{self.endpoint}/health") as response:
                    if response.status != 200:
                        return False
                    data = await response.json(content_type=None)
                    return data.get("status") == "ok"
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("FlareSolverr health check failed: %s", exc)
            return False

    async def create_session(self, name: Optional[str] = None) -> Optional[str]:
        if not self.enabled:
            return None

        session_name = name or f"ws-{uuid.uuid4().hex[:12]}"

        payload = {
            "cmd": "sessions.create",
            "session": session_name,
            "maxTimeout": self.max_timeout_ms,
        }

        if await self._post_with_retry(payload, return_raw=True):
            return session_name
        return None

    async def destroy_session(self, name: str) -> bool:
        if not (self.enabled and name):
            return False

        payload = {
            "cmd": "sessions.destroy",
            "session": name,
        }
        return bool(await self._post_with_retry(payload, return_raw=True))

    async def solve_get_request(
        self,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        cookies: Optional[Dict[str, str]] = None,
        proxy: Optional[str] = None,
        session: Optional[str] = None,
        max_timeout: Optional[int] = None,
        return_raw: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Solve a GET request via FlareSolverr."""

        payload = self._build_request_payload(
            cmd="request.get",
            url=url,
            headers=headers,
            cookies=cookies,
            proxy=proxy,
            session=session,
            max_timeout=max_timeout,
        )
        return await self._post_with_retry(payload, return_raw=return_raw)

    async def solve_post_request(
        self,
        url: str,
        *,
        data: Optional[Any] = None,
        headers: Optional[Dict[str, str]] = None,
        cookies: Optional[Dict[str, str]] = None,
        proxy: Optional[str] = None,
        session: Optional[str] = None,
        max_timeout: Optional[int] = None,
        return_raw: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Solve a POST request via FlareSolverr."""

        payload = self._build_request_payload(
            cmd="request.post",
            url=url,
            headers=headers,
            cookies=cookies,
            proxy=proxy,
            session=session,
            max_timeout=max_timeout,
            data=data,
        )
        return await self._post_with_retry(payload, return_raw=return_raw)

    # ------------------------------------------------------------------
    # Internal mechanics
    # ------------------------------------------------------------------
    def _build_request_payload(
        self,
        *,
        cmd: str,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        cookies: Optional[Dict[str, str]] = None,
        proxy: Optional[str] = None,
        session: Optional[str] = None,
        max_timeout: Optional[int] = None,
        data: Optional[Any] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "cmd": cmd,
            "url": url,
            "maxTimeout": max_timeout or self.max_timeout_ms,
        }

        default_headers = self.request_defaults.get("headers", {})
        if default_headers:
            payload["headers"] = dict(default_headers)
        if headers:
            payload.setdefault("headers", {}).update(headers)

        if cookies:
            payload["cookies"] = [
                {"name": name, "value": value} for name, value in cookies.items()
            ]

        if proxy:
            payload["proxy"] = {"url": proxy}

        if session:
            payload["session"] = session

        if data is not None:
            payload["postData"] = self._normalise_post_data(data)

        user_agent = self.request_defaults.get("user_agent")
        if user_agent:
            payload.setdefault("headers", {}).setdefault("User-Agent", user_agent)

        return payload

    def _normalise_post_data(self, data: Any) -> str:
        if isinstance(data, bytes):
            return data.decode("utf-8", errors="ignore")
        if isinstance(data, str):
            return data
        if isinstance(data, dict):
            from urllib.parse import urlencode

            return urlencode(data)
        return str(data)

    async def _post_with_retry(
        self,
        payload: Dict[str, Any],
        *,
        return_raw: bool = False,
    ) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            self.logger.debug("FlareSolverr client disabled; skipping payload %s", payload)
            return None

        attempt = 0
        delay = self._retry_delay
        last_error: Optional[Exception] = None

        while attempt <= self._max_retries:
            try:
                result = await self._post(payload)
                if return_raw:
                    return result
                return self._normalize_solution(result)
            except FlareSolverrError as exc:
                last_error = exc
                self.logger.warning(
                    "FlareSolverr request failed (attempt %s/%s): %s",
                    attempt + 1,
                    self._max_retries + 1,
                    exc,
                )
                if attempt >= self._max_retries:
                    break
                await asyncio.sleep(delay)
                delay *= self._backoff_multiplier
                attempt += 1
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                self.logger.error("Unexpected error contacting FlareSolverr: %s", exc)
                break

        if last_error:
            raise FlareSolverrError(str(last_error))
        return None

    async def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=self._default_timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{self.endpoint}/v1", json=payload) as response:
                if response.status != 200:
                    text = await response.text()
                    raise FlareSolverrError(
                        f"HTTP {response.status} from FlareSolverr: {text[:200]}"
                    )
                data = await response.json(content_type=None)
                if data.get("status") != "ok":
                    raise FlareSolverrError(str(data))
                return data

    def _normalize_solution(self, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        solution = data.get("solution")
        if not solution:
            return None

        raw_headers = solution.get("headers") or {}
        headers: Dict[str, str] = {}

        if isinstance(raw_headers, dict):
            for name, value in raw_headers.items():
                if name and value is not None:
                    headers[str(name)] = str(value)
        elif isinstance(raw_headers, list):
            for header in raw_headers:
                if isinstance(header, dict):
                    name = header.get("name")
                    value = header.get("value")
                    if name and value is not None:
                        headers[str(name)] = str(value)
                elif isinstance(header, str) and ":" in header:
                    name, value = header.split(":", 1)
                    headers[name.strip()] = value.strip()

        cookies = {
            cookie.get("name"): cookie.get("value")
            for cookie in solution.get("cookies", [])
            if cookie.get("name") is not None
        }

        response_time = solution.get("responseTime")
        if response_time is not None:
            try:
                response_time = float(response_time) / 1000.0
            except (TypeError, ValueError):
                response_time = None

        return {
            "status": solution.get("status", 0),
            "html": solution.get("response", ""),
            "headers": headers,
            "cookies": cookies,
            "user_agent": solution.get("userAgent"),
            "url": solution.get("url"),
            "session": data.get("session"),
            "response_time": response_time,
        }


async def wait_for_service(client: FlareSolverrClient, timeout: float = 30.0) -> bool:
    """Utility helper that waits for the service to become healthy."""

    start = time.time()
    while time.time() - start < timeout:
        if await client.health_check():
            return True
        await asyncio.sleep(1.0)
    return False
