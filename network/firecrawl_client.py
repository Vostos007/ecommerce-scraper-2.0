"""Thin Firecrawl API client used as a controlled fallback during scraping."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import requests


logger = logging.getLogger(__name__)


class FirecrawlClient:
    """HTTP wrapper around the Firecrawl `/v2/scrape` endpoint with rate limiting."""

    def __init__(self, config: Dict[str, Any]) -> None:
        self.enabled: bool = bool(config.get("enabled", False))

        raw_keys = config.get("api_keys")
        api_keys: list[str] = []
        if isinstance(raw_keys, list):
            for key in raw_keys:
                if isinstance(key, str) and key.strip():
                    api_keys.append(key.strip())

        primary_key = str(config.get("api_key") or "").strip()
        if primary_key:
            if primary_key not in api_keys:
                api_keys.insert(0, primary_key)

        self.api_keys: list[str] = api_keys
        self._key_index: int = 0
        self.api_key: str = api_keys[0] if api_keys else ""
        self.base_url: str = str(
            config.get("base_url", "https://api.firecrawl.dev/v2/scrape")
        )
        self.map_url: str = str(
            config.get("map_url", "https://api.firecrawl.dev/v2/map")
        )
        self.timeout: int = int(config.get("timeout_seconds", 30))
        self.max_requests: int = int(config.get("max_requests_per_run", 60))
        self.only_main_content: bool = bool(config.get("only_main_content", True))
        self.max_age_ms: Optional[int] = (
            int(config["max_age_ms"]) if config.get("max_age_ms") else None
        )
        self.store_in_cache: Optional[bool] = (
            bool(config.get("store_in_cache"))
            if config.get("store_in_cache") is not None
            else None
        )
        self.location: Optional[Dict[str, Any]] = (
            config.get("location") if isinstance(config.get("location"), dict) else None
        )

        formats = config.get("formats", ["markdown"])
        self.formats: list[Any] = formats if isinstance(formats, list) else [formats]

        parsers = config.get("parsers", [])
        self.parsers: list[str] = parsers if isinstance(parsers, list) else [parsers]

        self._request_count = 0
        self._cache: Dict[str, Optional[str]] = {}

        self._session = requests.Session()
        self._apply_auth_header()
        self._session.headers.update({"Content-Type": "application/json"})

        self._insufficient_credits = False
        self._credit_info_checked = False

        if not self.enabled or not self.api_key:
            logger.info("Firecrawl client initialised in disabled state")

    # Public API -----------------------------------------------------------------
    def scrape_markdown(self, url: str) -> Optional[str]:
        """Return markdown payload for *url* or None when disabled/failed."""

        if not self.enabled or not self.api_key or self._insufficient_credits:
            return None

        self._maybe_log_credit_status()

        if url in self._cache:
            return self._cache[url]

        if self._request_count >= self.max_requests:
            logger.warning(
                "Firecrawl request limit reached (%s); skipping %s",
                self.max_requests,
                url,
            )
            return None

        payload: Dict[str, Any] = {
            "url": url,
            "onlyMainContent": self.only_main_content,
        }

        if self.max_age_ms is not None:
            payload["maxAge"] = self.max_age_ms

        if self.store_in_cache is not None:
            payload["storeInCache"] = self.store_in_cache

        if self.location:
            payload["location"] = self.location

        if self.parsers:
            payload["parsers"] = self.parsers

        if self.formats:
            payload["formats"] = self.formats

        try:
            self._request_count += 1
            response = self._session.post(
                self.base_url, json=payload, timeout=self.timeout
            )
            response.raise_for_status()
            markdown = self._extract_markdown(response.json())
            if markdown:
                self._cache[url] = markdown
            else:
                self._cache[url] = None
            return markdown
        except requests.RequestException as exc:
            if self._should_rotate_key(exc):
                if self._rotate_key():
                    return self.scrape_markdown(url)
            logger.warning("Firecrawl request failed for %s: %s", url, exc)
            self._cache[url] = None
        except ValueError as exc:
            logger.warning("Firecrawl JSON decoding failed for %s: %s", url, exc)
            self._cache[url] = None

        return None

    # Internal helpers -----------------------------------------------------------
    @staticmethod
    def _extract_markdown(payload: Any) -> Optional[str]:
        """Extract markdown content from arbitrary Firecrawl payload shape."""

        if not isinstance(payload, dict):
            return None

        if isinstance(payload.get("markdown"), str):
            return payload["markdown"]

        data = payload.get("data")
        if isinstance(data, dict):
            if isinstance(data.get("markdown"), str):
                return data["markdown"]

            nested_data = data.get("data")
            if isinstance(nested_data, dict) and isinstance(
                nested_data.get("markdown"), str
            ):
                return nested_data["markdown"]

            document = (
                data.get("document")
                or data.get("documents")
                or (nested_data.get("document") if isinstance(nested_data, dict) else None)
            )
            if isinstance(document, dict) and isinstance(document.get("markdown"), str):
                return document["markdown"]

        outputs = payload.get("outputs")
        if isinstance(outputs, dict):
            if isinstance(outputs.get("markdown"), str):
                return outputs["markdown"]

        return None

    # Firecrawl map endpoint ---------------------------------------------------
    def map_urls(
        self,
        url: str,
        *,
        search: Optional[str] = None,
        limit: Optional[int] = None,
        sitemap_mode: Optional[str] = None,
        include_subdomains: Optional[bool] = None,
        include_paths: Optional[list[str]] = None,
        exclude_paths: Optional[list[str]] = None,
    ) -> list[str]:
        """Return discovered URLs using the Firecrawl `/v2/map` endpoint."""

        if not self.enabled or not self.api_key or self._insufficient_credits:
            return []

        self._maybe_log_credit_status()

        if self._request_count >= self.max_requests:
            logger.warning(
                "Firecrawl request limit reached (%s); skipping map for %s",
                self.max_requests,
                url,
            )
            return []

        payload: Dict[str, Any] = {"url": url}

        attempted_limit: Optional[int] = None

        if search:
            payload["search"] = search
        if limit is not None:
            try:
                attempted_limit = int(limit)
                payload["limit"] = attempted_limit
            except (TypeError, ValueError):
                logger.debug("Invalid Firecrawl map limit provided: %s", limit)
        if sitemap_mode:
            payload["sitemap"] = sitemap_mode
        if include_subdomains is not None:
            payload["includeSubdomains"] = bool(include_subdomains)
        if include_paths:
            payload["includePaths"] = include_paths
        if exclude_paths:
            payload["excludePaths"] = exclude_paths

        try:
            self._request_count += 1
            response = self._session.post(
                self.map_url, json=payload, timeout=self.timeout
            )
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            if self._should_rotate_key(exc, attempted_limit):
                if self._rotate_key():
                    return self.map_urls(
                        url,
                        search=search,
                        limit=limit,
                        sitemap_mode=sitemap_mode,
                        include_subdomains=include_subdomains,
                        include_paths=include_paths,
                        exclude_paths=exclude_paths,
                    )
            body = getattr(exc.response, "text", "") if hasattr(exc, "response") else ""
            if body:
                logger.warning(
                    "Firecrawl map request failed for %s: %s; body=%s",
                    url,
                    exc,
                    body[:200],
                )
            else:
                logger.warning("Firecrawl map request failed for %s: %s", url, exc)
            return []
        except ValueError as exc:
            logger.warning("Firecrawl map JSON decoding failed for %s: %s", url, exc)
            return []

        links: list[str] = []

        if isinstance(data, dict):
            if isinstance(data.get("links"), list):
                links = _extract_links_list(data["links"])
            elif isinstance(data.get("data"), dict) and isinstance(
                data["data"].get("links"), list
            ):
                links = _extract_links_list(data["data"]["links"])
            elif isinstance(data.get("data"), list):
                links = _extract_links_list(data["data"])

        if not links:
            logger.debug("Firecrawl map returned no links for %s", url)

        return links

    def extract(
        self,
        urls: list[str],
        *,
        prompt: Optional[str] = None,
        schema: Optional[Dict[str, Any]] = None,
        extra_payload: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Call the Firecrawl `/v2/extract` endpoint (uses token balance)."""

        if not self.enabled or not self.api_key or self._insufficient_credits:
            return None

        self._maybe_log_credit_status()

        payload: Dict[str, Any] = {"urls": urls}
        if prompt:
            payload["prompt"] = prompt
        if schema:
            payload["schema"] = schema
        if extra_payload:
            payload.update(extra_payload)

        try:
            self._request_count += 1
            response = self._session.post(
                "https://api.firecrawl.dev/v2/extract",
                json=payload,
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            if self._should_rotate_key(exc):
                if self._rotate_key():
                    return self.extract(urls, prompt=prompt, schema=schema, extra_payload=extra_payload)
            logger.warning("Firecrawl extract request failed: %s", exc)
            return None

    def _maybe_log_credit_status(self) -> None:
        if self._credit_info_checked or not self.enabled or not self.api_key:
            return

        self._credit_info_checked = True
        try:
            response = self._session.get(
                "https://api.firecrawl.dev/v2/team/credit-usage",
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json().get("data", {}) if response.content else {}
            remaining = data.get("remainingCredits")
            plan_total = data.get("planCredits")
            if remaining is not None:
                logger.info(
                    "Firecrawl credits remaining: %s (plan=%s)",
                    remaining,
                    plan_total,
                )
        except requests.RequestException as exc:
            logger.debug("Failed to query Firecrawl credit usage: %s", exc)

    def _apply_auth_header(self) -> None:
        if self.api_key:
            self._session.headers.update({"Authorization": f"Bearer {self.api_key}"})
        else:
            self._session.headers.pop("Authorization", None)

    def _rotate_key(self) -> bool:
        if self._key_index + 1 >= len(self.api_keys):
            self._insufficient_credits = True
            logger.warning("Firecrawl API keys exhausted; further requests disabled")
            return False

        self._key_index += 1
        self.api_key = self.api_keys[self._key_index]
        self._apply_auth_header()
        self._insufficient_credits = False
        self._credit_info_checked = False
        logger.info(
            "Rotated Firecrawl API key (index %s/%s)",
            self._key_index + 1,
            len(self.api_keys),
        )
        return True

    def _should_rotate_key(
        self,
        exc: requests.RequestException,
        attempted_limit: Optional[int] = None,
    ) -> bool:
        response = getattr(exc, "response", None)
        if response is None:
            return False

        status_code = getattr(response, "status_code", None)
        body = ""
        try:
            body = response.text or ""
        except Exception:  # pragma: no cover - very defensive
            body = ""

        is_quota = False
        if isinstance(status_code, int) and status_code == 402:
            is_quota = True
        if body and "insufficient credits" in body.lower():
            is_quota = True

        if not is_quota:
            return False

        if attempted_limit is not None and attempted_limit > 10:
            # allow fallback limits to try first before rotating keys
            return False

        if self._key_index + 1 >= len(self.api_keys):
            self._insufficient_credits = True
            return False

        return True


def _extract_links_list(raw_links: list[Any]) -> list[str]:
    urls: list[str] = []
    for item in raw_links:
        if isinstance(item, str):
            urls.append(item)
        elif isinstance(item, dict):
            candidate = item.get("url") or item.get("link")
            if isinstance(candidate, str):
                urls.append(candidate)
    return urls


__all__ = ["FirecrawlClient"]
