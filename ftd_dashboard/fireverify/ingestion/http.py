"""HTTP client with retry/backoff and secret redaction in error messages."""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable

import httpx

from fireverify.ingestion.base import SourceError

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}


class HttpClient:
    def __init__(self, timeout_s: float = 60.0, user_agent: str = "fireverify", max_retries: int = 3,
                 backoff_s: float = 1.5, secrets: Iterable[str | None] = ()) -> None:
        self._client = httpx.Client(timeout=timeout_s, headers={"User-Agent": user_agent}, follow_redirects=True)
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self._secrets = [s for s in secrets if s]

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        last_error: str = ""
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._client.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code < 400:
                    return resp
                last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
                if resp.status_code not in RETRY_STATUSES:
                    break
                retry_after = resp.headers.get("Retry-After", "")
                if retry_after.isdigit() and attempt < self.max_retries:
                    time.sleep(min(int(retry_after), 60))
                    continue
            if attempt < self.max_retries:
                delay = self.backoff_s * (2 ** attempt)
                log.debug("retrying %s %s in %.1fs (%s)", method, self.redact(url), delay, self.redact(last_error))
                time.sleep(delay)
        raise SourceError(self.redact(f"{method} {url} failed: {last_error}"))

    def get_text(self, url: str, **kwargs: Any) -> str:
        return self.request("GET", url, **kwargs).text

    def get_json(self, url: str, **kwargs: Any) -> Any:
        return self._json(self.request("GET", url, **kwargs), url)

    def post_json(self, url: str, body: Any, **kwargs: Any) -> Any:
        return self._json(self.request("POST", url, json=body, **kwargs), url)

    def post_form(self, url: str, data: dict[str, str], **kwargs: Any) -> Any:
        return self._json(self.request("POST", url, data=data, **kwargs), url)

    def _json(self, resp: httpx.Response, url: str) -> Any:
        try:
            return resp.json()
        except ValueError as exc:
            raise SourceError(self.redact(f"non-JSON response from {url}: {resp.text[:200]}")) from exc

    def close(self) -> None:
        self._client.close()
