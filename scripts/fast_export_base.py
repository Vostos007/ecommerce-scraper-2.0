"""Shared helpers for building fast catalog exporters."""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import contextvars
import fcntl
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Iterable, Iterator, List, Mapping, MutableSequence, Optional, Sequence, Set, TextIO, TypeVar
from urllib.parse import urlparse

import httpx

from utils.export_writers import ExportArtifacts, write_product_exports
from utils.firecrawl_summary import update_summary
from utils.helpers import looks_like_guard_html

LOGGER = logging.getLogger(__name__)

ProductHandler = Callable[[httpx.AsyncClient, str], Awaitable[Optional[Dict[str, Any]]]]
T = TypeVar("T")

RETRY_STATUSES = {429, 500, 502, 503, 504}

BASE_DIR = Path(__file__).resolve().parents[1]
SITES_DATA_DIR = BASE_DIR / "data" / "sites"
MAP_DIRECTORY_NAME = "maps"
MAP_STALENESS_THRESHOLD = timedelta(days=7)

CANONICAL_MAP_FILENAMES = {
    "mpyarn.ru": "mpyarn.ru.URL_map.json",
}

ANTIBOT_TRIGGER_STATUSES: Set[int] = {
    401,
    403,
    407,
    408,
    409,
    418,
    421,
    429,
    430,
    499,
    500,
    502,
    503,
    504,
    508,
    509,
    520,
    521,
    522,
    523,
    524,
    525,
    526,
    529,
    530,
}

DEFAULT_ANTIBOT_TIMEOUT = 90.0

if TYPE_CHECKING:  # pragma: no cover
    from core.antibot_manager import AntibotManager


_EXPORT_CONTEXT: contextvars.ContextVar[Optional["ExportContext"]] = contextvars.ContextVar(
    "export_context",
    default=None,
)


@dataclass(slots=True)
class AntibotRuntime:
    """Runtime wrapper around AntibotManager with concurrency control."""

    manager: "AntibotManager"
    semaphore: Optional[asyncio.Semaphore]
    timeout: float = DEFAULT_ANTIBOT_TIMEOUT
    trigger_statuses: Set[int] = field(
        default_factory=lambda: set(ANTIBOT_TRIGGER_STATUSES)
    )

    async def _invoke(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: Optional[Mapping[str, str]] = None,
        data: Any = None,
        json_payload: Any = None,
        cookies: Any = None,
    ) -> Optional[Dict[str, Any]]:
        async def _call() -> Optional[Dict[str, Any]]:
            return await self.manager.make_request_with_retry(
                url,
                method=method,
                headers=headers,
                data=data,
                json=json_payload,
                cookies=cookies,
            )

        if self.semaphore is None:
            return await asyncio.wait_for(_call(), timeout=self.timeout)

        async with self.semaphore:
            return await asyncio.wait_for(_call(), timeout=self.timeout)

    async def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: Optional[Mapping[str, str]] = None,
        data: Any = None,
        json_payload: Any = None,
        cookies: Any = None,
    ) -> Optional[Dict[str, Any]]:
        try:
            return await self._invoke(
                url,
                method=method,
                headers=headers,
                data=data,
                json_payload=json_payload,
                cookies=cookies,
            )
        except asyncio.TimeoutError:
            LOGGER.error("Antibot timeout for %s after %.0fs", url, self.timeout)
            return None

    async def cleanup(self) -> None:
        await self.manager.cleanup()


@dataclass
class ExportContext:
    """Holds export-wide runtime state (e.g., Antibot)."""

    antibot: Optional[AntibotRuntime] = None

    def __enter__(self) -> "ExportContext":
        self._token = _EXPORT_CONTEXT.set(self)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        _EXPORT_CONTEXT.reset(self._token)

    async def cleanup(self) -> None:
        if self.antibot:
            await self.antibot.cleanup()


@contextlib.contextmanager
def use_export_context(*, antibot: Optional[AntibotRuntime] = None) -> Iterator[ExportContext]:
    context = ExportContext(antibot=antibot)
    try:
        context.__enter__()
        yield context
    finally:
        context.__exit__(None, None, None)


def current_export_context() -> Optional[ExportContext]:
    return _EXPORT_CONTEXT.get()


def create_antibot_runtime(
    *,
    enabled: bool,
    config_path: Path,
    concurrency: Optional[int],
    timeout: float = DEFAULT_ANTIBOT_TIMEOUT,
) -> Optional[AntibotRuntime]:
    if not enabled:
        return None

    from core.antibot_manager import AntibotManager  # Local import to avoid cold-start cost

    manager = AntibotManager(str(config_path))
    semaphore = (
        asyncio.Semaphore(max(1, concurrency))
        if concurrency and concurrency > 0
        else None
    )
    return AntibotRuntime(manager=manager, semaphore=semaphore, timeout=timeout)


def finalize_antibot_runtime(runtime: Optional[AntibotRuntime]) -> None:
    if runtime is None:
        return
    asyncio.run(runtime.cleanup())


def add_antibot_arguments(
    parser: Any,
    *,
    default_enabled: bool = True,
    default_concurrency: Optional[int] = None,
    default_timeout: float = DEFAULT_ANTIBOT_TIMEOUT,
) -> None:
    group = parser.add_argument_group("Antibot")
    enable_dest = "use_antibot"
    group.add_argument(
        "--use-antibot",
        dest=enable_dest,
        action="store_true",
        default=default_enabled,
        help="Enable AntibotManager fallback (default: on)",
    )
    group.add_argument(
        "--no-antibot",
        dest=enable_dest,
        action="store_false",
        help="Disable AntibotManager fallback",
    )
    group.add_argument(
        "--antibot-concurrency",
        type=int,
        default=default_concurrency or 4,
        help="Max concurrent Antibot fallback requests (default: 4)",
    )
    group.add_argument(
        "--antibot-timeout",
        type=float,
        default=default_timeout,
        help="Timeout in seconds for Antibot fallback (default: 90)",
    )

_LOCK_REGISTRY: Dict[Path, TextIO] = {}
_LOCK_CLEANUP_REGISTERED = False


def _release_all_process_locks() -> None:
    for path in list(_LOCK_REGISTRY.keys()):
        release_process_lock(path)


def acquire_process_lock(lock_path: Path, *, logger: Optional[logging.Logger] = None) -> None:
    """Acquire an exclusive process-level file lock or exit with code 1."""

    global _LOCK_CLEANUP_REGISTERED

    if lock_path in _LOCK_REGISTRY:
        (logger or LOGGER).debug("Process lock already held: %s", lock_path)
        return

    try:
        handle = lock_path.open("w")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        handle.write(str(os.getpid()))
        handle.flush()
        _LOCK_REGISTRY[lock_path] = handle

        if not _LOCK_CLEANUP_REGISTERED:
            atexit.register(_release_all_process_locks)
            _LOCK_CLEANUP_REGISTERED = True

        (logger or LOGGER).info(
            "Acquired process lock %s (pid=%s)", lock_path, os.getpid()
        )
    except BlockingIOError:
        existing_pid = "unknown"
        try:
            existing_pid = lock_path.read_text(encoding="utf-8").strip() or "unknown"
        except OSError:
            pass
        (logger or LOGGER).error(
            "Process lock busy %s (pid=%s)", lock_path, existing_pid
        )
        raise SystemExit(1) from None
    except Exception as exc:  # noqa: BLE001
        (logger or LOGGER).error(
            "Failed to acquire process lock %s: %s", lock_path, exc
        )
        raise SystemExit(1) from exc


def release_process_lock(lock_path: Path, *, logger: Optional[logging.Logger] = None) -> None:
    """Release the process lock if held and remove the lock file."""

    handle = _LOCK_REGISTRY.pop(lock_path, None)
    if handle is not None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            handle.close()
        except OSError:
            pass

    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        (logger or LOGGER).debug(
            "Failed to remove lock file %s during release", lock_path, exc_info=True
        )



def _normalize_domain(domain: str) -> str:
    value = domain.strip().lower()
    if value.startswith("www."):
        value = value[4:]
    return value


class NotFoundError(Exception):
    """Raised when a requested resource returns HTTP 404."""

    def __init__(self, url: str, status_code: int = 404) -> None:
        super().__init__(f"Resource not found ({status_code}): {url}")
        self.url = url
        self.status_code = status_code


def _default_normalize(url: str) -> Optional[str]:
    candidate = url.strip()
    if not candidate:
        return None
    if "#" in candidate:
        candidate = candidate.split("#", 1)[0]
    if "?" in candidate:
        candidate = candidate.split("?", 1)[0]
    return candidate.rstrip("/")


def load_url_map(
    map_path: Path,
    *,
    allowed_domains: Optional[Iterable[str]] = None,
    include_predicate: Optional[Callable[[str], bool]] = None,
    normalize: Optional[Callable[[str], Optional[str]]] = None,
) -> List[str]:
    """Load URL entries from a sitemap-style JSON and filter product links."""

    if not map_path.exists():
        raise FileNotFoundError(f"URL map not found: {map_path}")

    try:
        data = json.loads(map_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:  # pragma: no cover - rare config error
        raise ValueError(f"Invalid JSON in {map_path}: {exc}") from exc

    entries = data.get("links") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise ValueError(f"Unexpected URL map structure in {map_path}")

    domain_filter = {domain.lower() for domain in allowed_domains} if allowed_domains else None
    seen: set[str] = set()
    result: List[str] = []

    for entry in entries:
        if isinstance(entry, dict):
            raw_url = entry.get("url")
        else:
            raw_url = entry
        if not isinstance(raw_url, str):
            continue

        cleaned = _default_normalize(raw_url)
        if not cleaned:
            continue

        normalized = normalize(cleaned) if normalize else cleaned
        if not normalized:
            continue

        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"}:
            continue
        host = (parsed.netloc or "").lower()
        if domain_filter and host not in domain_filter:
            continue

        if include_predicate and not include_predicate(normalized):
            continue

        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)

    return result


def get_canonical_map_path(site_domain: str) -> Path:
    domain = _normalize_domain(site_domain)
    file_name = CANONICAL_MAP_FILENAMES.get(domain, f"{domain}.URL-map.json")
    return SITES_DATA_DIR / domain / file_name


def find_available_maps(site_domain: str) -> List[Path]:
    domain = _normalize_domain(site_domain)
    site_dir = SITES_DATA_DIR / domain
    if not site_dir.exists():
        return []

    maps: List[Path] = []
    seen: set[Path] = set()

    canonical_path = get_canonical_map_path(domain)
    if canonical_path.exists():
        maps.append(canonical_path)
        seen.add(canonical_path)

    maps_dir = site_dir / MAP_DIRECTORY_NAME
    if maps_dir.exists():
        uploaded = sorted(
            (path for path in maps_dir.iterdir() if path.is_file() and path.suffix.lower() == ".json"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        for path in uploaded:
            if path not in seen:
                maps.append(path)
                seen.add(path)

    legacy_candidates = sorted(
        (
            path
            for path in site_dir.iterdir()
            if path.is_file() and path.suffix.lower() == ".json"
        ),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )

    for path in legacy_candidates:
        if path in seen:
            continue
        if path.parent.name == MAP_DIRECTORY_NAME:
            continue
        maps.append(path)
        seen.add(path)

    return maps


def load_url_map_with_fallback(
    site_domain: str,
    *,
    allowed_domains: Optional[Iterable[str]] = None,
    include_predicate: Optional[Callable[[str], bool]] = None,
    normalize: Optional[Callable[[str], Optional[str]]] = None,
) -> List[str]:
    domain = _normalize_domain(site_domain)
    site_dir = SITES_DATA_DIR / domain
    site_dir.mkdir(parents=True, exist_ok=True)

    canonical_path = get_canonical_map_path(domain)
    candidates: List[Path] = []

    if canonical_path.exists():
        canonical_mtime = datetime.fromtimestamp(canonical_path.stat().st_mtime, timezone.utc)
        if datetime.now(timezone.utc) - canonical_mtime <= MAP_STALENESS_THRESHOLD:
            candidates.append(canonical_path)

    for path in find_available_maps(domain):
        if path not in candidates:
            candidates.append(path)

    for candidate in candidates:
        try:
            urls = load_url_map(
                candidate,
                allowed_domains=allowed_domains,
                include_predicate=include_predicate,
                normalize=normalize,
            )
        except FileNotFoundError:
            LOGGER.warning("URL map not found: %s", candidate)
            continue
        except ValueError as exc:
            LOGGER.warning("Invalid URL map %s: %s", candidate, exc)
            continue

        if urls:
            LOGGER.info("Using URL map %s (%d URLs)", candidate, len(urls))
            return urls
        LOGGER.warning("URL map %s не содержит подходящих ссылок", candidate)

    LOGGER.warning("Не удалось найти валидный JSON карту для %s", site_domain)
    return []


@dataclass(slots=True)
class HTTPClientConfig:
    """Configuration for shared httpx.AsyncClient usage."""

    concurrency: int = 32
    timeout: float = 30.0
    max_retries: int = 3
    backoff_base: float = 0.5
    headers: Optional[Mapping[str, str]] = field(default_factory=dict)
    base_url: Optional[str] = None
    transport: Any = None
    verify: bool = True

    def build_limits(self) -> httpx.Limits:
        limit = max(self.concurrency, 1)
        return httpx.Limits(
            max_connections=limit,
            max_keepalive_connections=limit,
        )

    def build_timeout(self) -> httpx.Timeout:
        return httpx.Timeout(self.timeout, read=self.timeout)


async def request_with_retries(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    max_retries: int = 3,
    backoff_base: float = 0.5,
    antibot: Optional[AntibotRuntime] = None,
    fallback_statuses: Optional[Set[int]] = None,
    **kwargs: Any,
) -> httpx.Response:
    """Perform an HTTP request with retry + optional Antibot fallback."""

    attempt = 0
    last_error: Optional[Exception] = None

    if fallback_statuses is None:
        fallback_statuses = ANTIBOT_TRIGGER_STATUSES

    if antibot is None:
        context = current_export_context()
        if context is not None:
            antibot = context.antibot

    while attempt < max_retries:
        attempt += 1
        try:
            response = await client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            last_error = exc
            LOGGER.warning("HTTP request error (%s %s): %s", method, url, exc)
        else:
            status = response.status_code

            if antibot and _should_trigger_antibot(response, fallback_statuses):
                fallback_response = await _try_antibot_fallback(
                    antibot,
                    url,
                    method=method,
                    original_headers=kwargs.get("headers"),
                    data=kwargs.get("data"),
                    json_payload=kwargs.get("json"),
                    cookies=kwargs.get("cookies"),
                )
                if fallback_response is not None:
                    return fallback_response

            if status == 404:
                LOGGER.warning("HTTP 404 (%s %s) — skipping", method, url)
                raise NotFoundError(url, status_code=status)
            if status in RETRY_STATUSES and attempt < max_retries:
                await asyncio.sleep(backoff_base * attempt)
                continue
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                last_error = exc
                LOGGER.error("HTTP status error (%s %s): %s", method, url, exc)
            else:
                return response
        await asyncio.sleep(backoff_base * attempt)

    if last_error is not None:
        raise last_error
    raise RuntimeError(f"Failed to execute request after {max_retries} attempts: {method} {url}")


def _should_trigger_antibot(
    response: httpx.Response,
    fallback_statuses: Set[int],
) -> bool:
    status = response.status_code
    if status in fallback_statuses:
        return True

    content_type = response.headers.get("content-type", "")
    if "text/html" in content_type.lower():
        try:
            body = response.text
        except UnicodeDecodeError:
            return False
        return looks_like_guard_html(body)

    return False


async def _try_antibot_fallback(
    runtime: AntibotRuntime,
    url: str,
    *,
    method: str,
    original_headers: Optional[Mapping[str, str]],
    data: Any,
    json_payload: Any,
    cookies: Any,
) -> Optional[httpx.Response]:
    fallback_payload = await runtime.fetch(
        url,
        method=method,
        headers=original_headers,
        data=data,
        json_payload=json_payload,
        cookies=cookies,
    )

    if not fallback_payload:
        LOGGER.warning("Antibot fallback failed for %s", url)
        return None

    LOGGER.info("Antibot fallback succeeded for %s", url)
    return _response_from_antibot(url, method, fallback_payload)


def _response_from_antibot(
    url: str, method: str, payload: Mapping[str, Any]
) -> httpx.Response:
    status = int(payload.get("status") or 200)
    content = payload.get("content") or b""
    headers = payload.get("headers") or {}

    if isinstance(content, str):
        body = content.encode("utf-8", errors="replace")
    elif isinstance(content, (bytes, bytearray)):
        body = bytes(content)
    else:
        body = str(content).encode("utf-8", errors="replace")

    request = httpx.Request(method, url, headers=headers or None)
    response = httpx.Response(
        status_code=status,
        content=body,
        headers=headers,
        request=request,
    )
    return response


def binary_stock(flag: Any) -> float:
    """Convert an availability flag to binary stock."""

    if isinstance(flag, (int, float)):
        return 1.0 if float(flag) > 0 else 0.0
    return 1.0 if bool(flag) else 0.0


@dataclass(slots=True)
class IncrementalWriter:
    """Append-only writer for partial export results with resume support."""

    partial_path: Path
    resume: bool
    resume_window_hours: Optional[int] = None
    _file: Optional[TextIO] = field(default=None, init=False)
    processed_urls: set[str] = field(default_factory=set, init=False)

    def load_existing(self) -> List[Dict[str, Any]]:
        if not self.partial_path.exists():
            return []
        entries: List[Dict[str, Any]] = []
        with self.partial_path.open('r', encoding='utf-8') as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    product = json.loads(line)
                except json.JSONDecodeError:
                    continue
                url = product.get('url')
                if isinstance(url, str):
                    self.processed_urls.add(url)
                original_url = product.get('original_url')
                if isinstance(original_url, str):
                    self.processed_urls.add(original_url)
                entries.append(product)
        return entries

    def open(self) -> None:
        self.partial_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.partial_path.open('a', encoding='utf-8')

    def append(self, product: Dict[str, Any]) -> None:
        if self._file is None:
            raise RuntimeError('incremental writer is not opened')
        url = product.get('url')
        if isinstance(url, str):
            self.processed_urls.add(url)
        original_url = product.get('original_url')
        if isinstance(original_url, str):
            self.processed_urls.add(original_url)
        self._file.write(json.dumps(product, ensure_ascii=False) + '\n')
        self._file.flush()

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

    def finalize(self) -> List[Dict[str, Any]]:
        self.close()
        return self.load_existing()

    def cleanup(self) -> None:
        self.close()
        self.processed_urls.clear()
        try:
            self.partial_path.unlink()
        except FileNotFoundError:
            return


def make_cli_progress_callback(
    *, site: str, script: str, total: int
) -> Optional[Callable[[Dict[str, int]], None]]:
    """Return stdout progress emitter when EXPORT_PROGRESS_EVENTS is enabled."""

    flag = os.environ.get("EXPORT_PROGRESS_EVENTS", "").strip().lower()
    if flag not in {"1", "true", "yes", "on"}:
        return None

    normalized_total = max(int(total), 1) if total > 0 else 0

    def _emit(event: Dict[str, int]) -> None:
        processed = int(event.get("processed", 0) or 0)
        success = int(event.get("success", 0) or 0)
        failed = int(event.get("failed", 0) or 0)
        total_value = int(event.get("total", normalized_total) or normalized_total)
        if total_value <= 0:
            total_value = max(normalized_total or processed or 1, 1)

        processed = max(processed, 0)
        success = max(success, 0)
        failed = max(failed, 0)

        progress_percent = 0.0
        if total_value > 0:
            progress_percent = min(100.0, max(0.0, (processed / total_value) * 100.0))

        payload = {
            "event": "progress",
            "site": site,
            "script": script,
            "processed": processed,
            "success": success,
            "failed": failed,
            "total": total_value,
            "progressPercent": round(progress_percent, 2),
            "timestamp": time.time(),
        }
        print(json.dumps(payload, ensure_ascii=False), flush=True)

    return _emit if normalized_total or flag else None


@dataclass(slots=True)
class AsyncFetcher:
    """Run asynchronous product fetch tasks with bounded concurrency."""

    config: HTTPClientConfig

    async def run(
        self,
        urls: Sequence[str],
        handler: ProductHandler,
        *,
        progress_callback: Optional[Callable[[Dict[str, int]], None]] = None,
        progress_total: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        if not urls:
            return []

        concurrency = max(self.config.concurrency, 1)
        semaphore = asyncio.Semaphore(concurrency)
        timeout = self.config.build_timeout()
        limits = self.config.build_limits()

        results: MutableSequence[Optional[Dict[str, Any]]] = [None] * len(urls)
        total_tasks = progress_total if progress_total is not None else len(urls)
        counters = {"processed": 0, "success": 0, "failed": 0}
        progress_lock = asyncio.Lock()

        async def _emit_progress(success: bool) -> None:
            if progress_callback is None:
                return
            payload: Dict[str, int]
            async with progress_lock:
                counters["processed"] += 1
                if success:
                    counters["success"] += 1
                else:
                    counters["failed"] += 1
                base_total = total_tasks if total_tasks is not None else len(urls)
                total_value = max(base_total, counters["processed"], 1)
                payload = {
                    "processed": counters["processed"],
                    "success": counters["success"],
                    "failed": counters["failed"],
                    "total": total_value,
                }
            try:
                progress_callback(payload)
            except Exception:  # pragma: no cover - defensive guard
                LOGGER.debug("Progress callback raised an exception", exc_info=True)

        client_kwargs: Dict[str, Any] = {
            "headers": dict(self.config.headers) if self.config.headers else None,
            "limits": limits,
            "timeout": timeout,
            "transport": self.config.transport,
            "verify": self.config.verify,
        }
        if self.config.base_url:
            client_kwargs["base_url"] = self.config.base_url

        async with httpx.AsyncClient(**client_kwargs) as client:
            async def _wrapped(index: int, url: str) -> None:
                result: Optional[Dict[str, Any]] = None
                async with semaphore:
                    try:
                        result = await handler(client, url)
                    except Exception as exc:  # pragma: no cover - defensive
                        LOGGER.exception("Handler error for %s: %s", url, exc)
                        result = None
                results[index] = result
                await _emit_progress(result is not None)

            tasks = [
                asyncio.create_task(_wrapped(index, url))
                for index, url in enumerate(urls)
            ]
            if tasks:
                await asyncio.gather(*tasks)

        return [item for item in results if item is not None]


def export_products(
    domain: str,
    export_path: Path,
    products: Sequence[Dict[str, Any]],
    *,
    success_rate: Optional[float] = None,
) -> ExportArtifacts:
    """Write exports and refresh Firecrawl metrics summary.

    Args:
        domain: Домен площадки.
        export_path: Путь к основному JSON экспортy (latest.json зеркалируется автоматически).
        products: Коллекция собранных карточек.
        success_rate: Доля успешно обработанных URL (0.0–1.0). None, если метрика недоступна.
    """

    sorted_products = sorted(products, key=lambda item: item.get("url", ""))
    artifacts = write_product_exports(sorted_products, export_path)
    update_summary(
        domain,
        list(sorted_products),
        export_file=artifacts.json_path.name,
        status="ok",
        success_rate=success_rate,
    )
    return artifacts


def load_export_products(export_path: Path) -> List[Dict[str, Any]]:
    """Load product dictionaries from an export file."""

    if not export_path.exists():
        return []

    try:
        data = json.loads(export_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        LOGGER.warning("Failed to load existing export %s: %s", export_path, exc)
        return []

    if isinstance(data, dict):
        candidates = data.get("products")
        if isinstance(candidates, list):
            return [item for item in candidates if isinstance(item, dict)]
        LOGGER.warning("Unexpected export object structure in %s", export_path)
        return []
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]

    LOGGER.warning("Unexpected export structure in %s", export_path)
    return []


def prime_writer_from_export(
    writer: "IncrementalWriter",
    export_path: Path,
    products: Optional[Sequence[Dict[str, Any]]] = None,
) -> int:
    """Seed writer.processed_urls from an existing export file."""

    items = list(products) if products is not None else load_export_products(export_path)
    seeded = 0
    for product in items:
        added = False
        for key in ("url", "original_url"):
            value = product.get(key)
            if isinstance(value, str) and value:
                if value not in writer.processed_urls:
                    writer.processed_urls.add(value)
                    added = True
        if added:
            seeded += 1

    if seeded:
        LOGGER.info(
            "Seeded %s products from existing export %s", seeded, export_path
        )
    return seeded


def merge_products(
    existing: Sequence[Dict[str, Any]],
    new: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Merge product lists, avoiding duplicates by url/original_url."""

    merged: List[Dict[str, Any]] = []
    seen: set[str] = set()

    def _register(product: Dict[str, Any]) -> None:
        keys = False
        for key in ("url", "original_url"):
            value = product.get(key)
            if isinstance(value, str) and value:
                seen.add(value)
                keys = True
        if not keys:
            seen.add(json.dumps(product, sort_keys=True))

    for product in existing:
        merged.append(product)
        _register(product)

    for product in new:
        duplicate = False
        for key in ("url", "original_url"):
            value = product.get(key)
            if isinstance(value, str) and value in seen:
                duplicate = True
                break
        if duplicate:
            continue
        merged.append(product)
        _register(product)

    return merged


def record_error_product(
    writer: "IncrementalWriter",
    *,
    domain: str,
    url: str,
    original_url: Optional[str] = None,
    status_code: Optional[int] = None,
    message: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
) -> Dict[str, Any]:
    """Append an error placeholder product and log the incident."""

    error_message = message or "unavailable"
    payload: Dict[str, Any] = {
        "url": url,
        "original_url": original_url or url,
        "site_domain": domain,
        "name": None,
        "price": None,
        "base_price": None,
        "currency": None,
        "stock": 0.0,
        "stock_quantity": 0.0,
        "in_stock": False,
        "variations": [],
        "error": error_message,
        "status_code": status_code,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }
    writer.append(payload)

    if logger is not None:
        if status_code is not None:
            logger.warning(
                "Marking %s as unavailable (status %s): %s",
                url,
                status_code,
                error_message,
            )
        else:
            logger.warning("Marking %s as unavailable: %s", url, error_message)

    return payload


def prepare_incremental_writer(
    partial_path: Path,
    *,
    resume: bool,
    resume_window_hours: Optional[int] = None,
    now: Optional[datetime] = None,
) -> tuple[IncrementalWriter, List[Dict[str, Any]]]:
    """Configure incremental writer, applying resume policies."""

    if resume_window_hours is not None and resume_window_hours < 0:
        raise ValueError("resume_window_hours must be non-negative")

    writer = IncrementalWriter(
        partial_path=partial_path,
        resume=resume,
        resume_window_hours=resume_window_hours,
    )

    if not resume:
        writer.cleanup()
    else:
        if partial_path.exists() and resume_window_hours is not None:
            current_time = now or datetime.now(timezone.utc)
            modified = datetime.fromtimestamp(partial_path.stat().st_mtime, timezone.utc)
            if current_time - modified > timedelta(hours=resume_window_hours):
                age_hours = (current_time - modified).total_seconds() / 3600
                LOGGER.info(
                    "Discarding partial export %s: age %.2f h exceeds resume_window %s h",
                    partial_path,
                    age_hours,
                    resume_window_hours,
                )
                writer.cleanup()

    existing = writer.load_existing()
    writer.open()
    return writer, existing


__all__ = [
    "AsyncFetcher",
    "HTTPClientConfig",
    "NotFoundError",
    "IncrementalWriter",
    "ExportArtifacts",
    "prepare_incremental_writer",
    "binary_stock",
    "export_products",
    "prime_writer_from_export",
    "load_export_products",
    "merge_products",
    "update_summary",
    "record_error_product",
    "load_url_map",
    "load_url_map_with_fallback",
    "get_canonical_map_path",
    "find_available_maps",
    "request_with_retries",
    "make_cli_progress_callback",
]
