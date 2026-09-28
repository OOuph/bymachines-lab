"""Shared types for engine adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol
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
