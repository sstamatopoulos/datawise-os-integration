"""
datagates.core.httpclient — the HTTP client every network gate uses.

One place for the things that decide whether an integration survives a
year in production:

- **retries on the transient failures only** (connection reset, timeout,
  429, 5xx). A 401 or a 404 is a configuration error and must fail the
  task loudly instead of being retried into a rate limit.
- **`Retry-After` is honoured.** Legacy portals answer 429 with a
  Retry-After of minutes; exponential backoff alone gets the client
  banned for the day.
- **A minimum interval between requests** (`min_interval_s`). Several
  municipal and utility APIs allow a handful of calls per second per
  token and answer the rest with 403, not 429.
- **Session reuse**, so a backfill walking 200 windows does not open 200
  TLS connections.

`max_attempts` and `min_interval_s` are gate-level YAML keys handled by
the framework (see docs/gates.md), so no gate implements them again.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Any

import requests

log = logging.getLogger(__name__)

RETRY_STATUS = frozenset({408, 423, 425, 429, 500, 502, 503, 504, 509, 520, 521, 522, 524})
MAX_BACKOFF_S = 60.0


class HttpClient:
    """A requests.Session with the retry, throttle and auth policy above."""

    def __init__(self, *, headers: dict[str, str] | None = None, auth: tuple[str, str] | None = None,
                 timeout: float = 60.0, max_attempts: int = 4, min_interval_s: float = 0.0,
                 verify: bool = True, base_url: str = "") -> None:
        self.session = requests.Session()
        if headers:
            self.session.headers.update({k: str(v) for k, v in headers.items() if v is not None})
        if auth:
            self.session.auth = auth
        self.timeout = float(timeout)
        self.max_attempts = max(1, int(max_attempts))
        self.min_interval_s = max(0.0, float(min_interval_s))
        self.verify = verify
        self.base_url = base_url.rstrip("/")
        self._last_call = 0.0

    # -- plumbing ----------------------------------------------------------
    def _throttle(self) -> None:
        if self.min_interval_s:
            wait = self.min_interval_s - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
        self._last_call = time.monotonic()

    @staticmethod
    def _retry_after(response: requests.Response, attempt: int) -> float:
        raw = response.headers.get("Retry-After", "")
        try:
            return min(float(raw), MAX_BACKOFF_S)
        except ValueError:
            pass
        return min(2.0 ** attempt + random.random(), MAX_BACKOFF_S)   # noqa: S311 - jitter, not crypto

    def _url(self, url: str) -> str:
        if url.startswith(("http://", "https://")) or not self.base_url:
            return url
        return f"{self.base_url}/{url.lstrip('/')}"

    # -- the one entry point -----------------------------------------------
    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        kwargs.setdefault("verify", self.verify)
        target = self._url(url)
        last: BaseException | None = None
        for attempt in range(1, self.max_attempts + 1):
            self._throttle()
            try:
                response = self.session.request(method.upper(), target, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last = exc
                if attempt == self.max_attempts:
                    raise
                time.sleep(min(2.0 ** attempt, MAX_BACKOFF_S))
                continue
            if response.status_code in RETRY_STATUS and attempt < self.max_attempts:
                delay = self._retry_after(response, attempt)
                log.warning("%s %s -> %s, retrying in %.1fs (attempt %d/%d)",
                            method.upper(), target, response.status_code, delay, attempt, self.max_attempts)
                time.sleep(delay)
                continue
            response.raise_for_status()
            return response
        raise last or RuntimeError(f"{method} {target}: no attempt was made")   # pragma: no cover

    def get_json(self, url: str, **kwargs: Any) -> Any:
        return self._json(self.request("GET", url, **kwargs))

    def post_json(self, url: str, **kwargs: Any) -> Any:
        return self._json(self.request("POST", url, **kwargs))

    def get_text(self, url: str, **kwargs: Any) -> str:
        r = self.request("GET", url, **kwargs)
        r.encoding = r.encoding or "utf-8"
        return r.text

    @staticmethod
    def _json(response: requests.Response) -> Any:
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            head = response.text[:200].replace("\n", " ")
            raise RuntimeError(f"{response.url}: expected JSON, got {response.headers.get('Content-Type')}: "
                               f"{head}") from exc

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def auth_from_options(options: dict[str, Any]) -> tuple[tuple[str, str] | None, dict[str, str]]:
    """`auth:` block -> (requests auth tuple, extra headers).

        auth: {type: basic, username: ${USER}, password: ${PASS}}
        auth: {type: bearer, token: ${TOKEN}}
        auth: {type: header, name: X-API-Key, value: ${KEY}}
        auth: {type: query, name: apikey, value: ${KEY}}   # handled by the gate

    Legacy systems are overwhelmingly basic auth; the rest exist because
    one partner per pilot always differs.
    """
    spec = options.get("auth") or {}
    if not spec:
        return None, {}
    kind = str(spec.get("type", "basic")).lower()
    if kind == "basic":
        return (str(spec.get("username", "")), str(spec.get("password", ""))), {}
    if kind == "bearer":
        return None, {"Authorization": f"{spec.get('scheme', 'Bearer')} {spec.get('token', '')}"}
    if kind == "header":
        return None, {str(spec["name"]): str(spec.get("value", ""))}
    if kind == "query":
        return None, {}
    raise ValueError(f"unknown auth type {kind!r}; use basic, bearer, header or query")


def client_for(gate: Any, *, headers: dict[str, str] | None = None, base_url: str = "") -> HttpClient:
    """Build the client for a gate from its options and the framework-wide
    `timeout`, `max_attempts`, `min_interval_s` and `verify_tls` keys."""
    options = gate.options
    auth, auth_headers = auth_from_options(options)
    merged = {**(headers or {}), **dict(options.get("headers") or {}), **auth_headers}
    return HttpClient(headers=merged, auth=auth,
                      timeout=float(options.get("timeout", 60)),
                      max_attempts=int(getattr(gate, "max_attempts", 4)),
                      min_interval_s=float(getattr(gate, "min_interval_s", 0.0)),
                      verify=bool(options.get("verify_tls", True)),
                      base_url=base_url)


def query_auth(options: dict[str, Any]) -> dict[str, str]:
    """The `auth: {type: query}` case, as request params."""
    spec = options.get("auth") or {}
    if str(spec.get("type", "")).lower() == "query":
        return {str(spec["name"]): str(spec.get("value", ""))}
    return {}
