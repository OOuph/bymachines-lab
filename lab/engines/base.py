"""Shared types for engine adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol
from urllib.parse import urlparse


class EngineError(Exception):
    """An engine call failed. `raw` and `cost_usd` carry a paid-but-unusable response so the runner can still store it."""

    def __init__(self, message: str, *, retryable: bool = False, status: int | None = None,
                 raw: dict[str, Any] | None = None, cost_usd: float = 0.0):
        super().__init__(message)
        self.retryable = retryable
        self.status = status
        self.raw = raw
        self.cost_usd = cost_usd


@dataclass(frozen=True)
class Citation:
    position: int
    url: str
    title: str
    domain: str


@dataclass
class Answer:
    text: str
    citations: list[Citation]
    raw: dict[str, Any]
    cost_usd: float
    searched: bool
    n_search: int
    latency_ms: int
    model: str
    usage: dict[str, Any] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)


class EngineLike(Protocol):
    id: str

    def ask(self, prompt_text: str, location: Any, options: dict[str, Any]) -> Answer: ...


def domain_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}
MAX_BACKOFF_S = 60.0


def _connect_phase_error(exc: BaseException) -> bool:
    """True when the request certainly never reached the server, so a retry cannot double-spend."""
    import httpx

    return isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))


def http_with_retries(do_request: Callable[[], Any], *, retries: int, sleep: Callable[[float], None], what: str) -> Any:
    """Call `do_request` until it returns HTTP 200; retry 408/409/429/5xx and connect-phase transport errors with capped backoff.

    Every engine POST is billed once the request is delivered, so a read timeout or a broken response is NOT retried:
    it becomes a non-retryable EngineError (an error row, re-planned by the catch-up window) instead of a second purchase.
    Returns the response object. Raises EngineError (non-retryable) at once for other status codes, and the last
    retryable error after `retries` retries. `Retry-After` is honoured, capped at MAX_BACKOFF_S.
    """
    import httpx

    last: EngineError | None = None
    for attempt in range(retries + 1):
        try:
            resp = do_request()
        except httpx.HTTPError as exc:
            if not _connect_phase_error(exc):
                raise EngineError(f"{what}: {type(exc).__name__} after the request may have been delivered — not retried "
                                  f"(a retry could buy the answer twice); the cell is re-planned by the catch-up window", retryable=False) from exc
            last = EngineError(f"{what}: connect error: {exc}", retryable=True)
            _backoff(attempt, retries, None, sleep)
            continue
        status = int(resp.status_code)
        if status == 200:
            return resp
        try:
            payload = resp.json()
            message = str((payload.get("error") or {}).get("message") or payload.get("status_message") or payload)[:300]
        except Exception:  # noqa: BLE001
            message = str(getattr(resp, "text", ""))[:300]
        last = EngineError(f"{what}: HTTP {status}: {message}", retryable=status in RETRYABLE_STATUS, status=status)
        if not last.retryable:
            raise last
        _backoff(attempt, retries, resp.headers.get("Retry-After"), sleep)
    assert last is not None
    raise last


def _backoff(attempt: int, retries: int, retry_after: str | None, sleep: Callable[[float], None]) -> None:
    import random

    if attempt >= retries:
        return
    if retry_after is not None:
        try:
            sleep(min(MAX_BACKOFF_S, max(0.0, float(retry_after))))
            return
        except ValueError:
            pass
    sleep(min(MAX_BACKOFF_S, (2 ** attempt) + random.uniform(0, 0.5)))
