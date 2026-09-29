"""OpenAI Responses API + `web_search` (docs read 2026-09-28, see docs/api-check-2026-09-28.md).

Request: model, input, reasoning.effort (none = no reasoning), tools=[web_search{search_context_size, user_location}],
include=["web_search_call.action.sources"]. Without search: no tools at all (twist "prior without search").
Response: output[] with web_search_call items (action.type == "search" → billed) and a message whose
content[].annotations[] of type url_citation are the citations. Cost per SPEC §10.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Callable

import httpx

from lab.config import EngineSpec, Location
from lab.engines.base import Answer, Citation, EngineError, _connect_phase_error, domain_of

API_URL = "https://api.openai.com/v1/responses"
RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}
MAX_BACKOFF_S = 60.0
log = logging.getLogger("lab.engines.openai")


def build_request(prompt_text: str, location: Location | None, *, model: str, effort: str, search: bool,
                  search_context_size: str, include_sources: bool) -> dict[str, Any]:
    body: dict[str, Any] = {"model": model, "input": prompt_text, "reasoning": {"effort": effort}}
    if search:
        # OpenAI defaults to the United States when the object is omitted; passing {"type": "approximate"} without
        # fields is the documented way to avoid that fallback, so the object is always sent (SPEC §4).
        user_location: dict[str, str] = {"type": "approximate"}
        if location is not None:
            user_location.update({"country": location.country, "city": location.city, "region": location.region,
                                  "timezone": location.timezone})
        body["tools"] = [{"type": "web_search", "search_context_size": search_context_size, "user_location": user_location}]
        if include_sources:
            body["include"] = ["web_search_call.action.sources"]
    return body


def _price_for(price: dict[str, Any], table: str, model: str) -> float:
    tbl = price.get(table) or {}
    if model not in tbl:
        raise EngineError(f"no price '{table}' for model {model} in engines.yaml")
    return float(tbl[model])


def compute_cost(usage: dict[str, Any], n_search: int, model: str, price: dict[str, Any]) -> float:
    input_tokens = int(usage.get("input_tokens", 0) or 0)
    cached = int((usage.get("input_tokens_details") or {}).get("cached_tokens", 0) or 0)
    output_tokens = int(usage.get("output_tokens", 0) or 0)
    p_in = _price_for(price, "per_1m_input", model)
    p_cached = float((price.get("per_1m_cached_input") or {}).get(model, p_in))
    p_out = _price_for(price, "per_1m_output", model)
    per_search = float(price.get("per_search_call", 0.0))
    return ((input_tokens - cached) * p_in + cached * p_cached + output_tokens * p_out) / 1e6 + n_search * per_search


def _safe_cost(usage: dict[str, Any], n_search: int, model: str, fallback_model: str, price: dict[str, Any]) -> float:
    """Never lose a paid answer over a missing price row: fall back to the configured model's price, then to the search fee."""
    for m in (model, fallback_model):
        try:
            return compute_cost(usage, n_search, m, price)
        except EngineError:
            continue
    log.warning("no price for model %s (fallback %s): cost counts only %d search call(s)", model, fallback_model, n_search)
    return n_search * float(price.get("per_search_call", 0.0))


def parse_response(data: dict[str, Any], *, model: str, price: dict[str, Any]) -> Answer:
    texts: list[str] = []
    citations: list[Citation] = []
    sources: list[str] = []
    n_search = 0
    for item in data.get("output") or []:
        itype = item.get("type")
        if itype == "web_search_call":
            action = item.get("action") or {}
            if action.get("type") == "search":
                n_search += 1
            for src in action.get("sources") or []:
                if isinstance(src, dict) and src.get("url"):
                    sources.append(str(src["url"]))
        elif itype == "message":
            for part in item.get("content") or []:
                ptype = part.get("type")
                if ptype == "refusal":                     # keep refusals in the text so refusal_patterns can match (S-I)
                    texts.append(str(part.get("refusal", "")))
                    continue
                if ptype != "output_text":
                    continue
                texts.append(str(part.get("text", "")))
                for ann in part.get("annotations") or []:
                    if ann.get("type") != "url_citation" or not ann.get("url"):
                        continue
                    url = str(ann["url"])
                    citations.append(Citation(position=len(citations) + 1, url=url, title=str(ann.get("title", "") or ""),
                                              domain=domain_of(url)))
    usage = dict(data.get("usage") or {})
    if not usage:
        log.warning("response %s has no usage block: token cost unknown, counting only the search fee", data.get("id"))
    used_model = str(data.get("model") or model)
    cost = _safe_cost(usage, n_search, used_model, model, price)
    status = data.get("status")
    if status != "completed":
        detail = data.get("incomplete_details") or data.get("error") or {}
        raise EngineError(f"response status {status!r}: {detail}", retryable=False, raw=data, cost_usd=cost)
    return Answer(text="".join(texts).strip(), citations=citations, raw=data, cost_usd=cost, searched=n_search > 0,
                  n_search=n_search, latency_ms=0, model=used_model, usage=usage, sources=sources)


class OpenAIEngine:
    id = "openai"

    def __init__(self, *, api_key: str, model: str, effort: str, search_context_size: str, include_sources: bool,
                 price: dict[str, Any], timeout_s: float, retries: int,
                 post: Callable[..., Any] | None = None, sleep: Callable[[float], None] = time.sleep):
        self.api_key = api_key
        self.model = model
        self.effort = effort
        self.search_context_size = search_context_size
        self.include_sources = include_sources
        self.price = price
        self.timeout_s = timeout_s
        self.retries = retries
        self._post = post or httpx.post
        self._sleep = sleep

    @classmethod
    def from_spec(cls, spec: EngineSpec, tunables: dict[str, Any], api_key: str) -> "OpenAIEngine":
        o = spec.options
        return cls(
            api_key=api_key, model=spec.model, effort=str(o.get("reasoning_effort", "none")),
            search_context_size=str(o.get("search_context_size", tunables.get("default_search_context_size", "medium"))),
            include_sources=bool(o.get("include_sources", True)), price=spec.price,
            timeout_s=float(o.get("timeout_s", 180)), retries=int(tunables.get("retries", 3)),
        )

    def ask(self, prompt_text: str, location: Location | None, options: dict[str, Any]) -> Answer:
        model = str(options.get("model") or self.model)
        body = build_request(
            prompt_text, location, model=model, effort=self.effort, search=bool(options.get("search", True)),
            search_context_size=str(options.get("search_context_size") or self.search_context_size),
            include_sources=self.include_sources,
        )
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        attempts = self.retries + 1
        last_error: EngineError | None = None
        for attempt in range(attempts):
            t0 = time.monotonic()
            try:
                resp = self._post(API_URL, json=body, headers=headers, timeout=self.timeout_s)
            except httpx.HTTPError as exc:  # transport errors and timeouts
                if not _connect_phase_error(exc):
                    # the request may have been delivered and billed: never buy the same answer twice
                    raise EngineError(f"openai: {type(exc).__name__} after the request may have been delivered — not retried", retryable=False) from exc
                last_error = EngineError(f"openai: connect error: {exc}", retryable=True)
                self._backoff(attempt, None)
                continue
            latency_ms = int((time.monotonic() - t0) * 1000)
            status = int(resp.status_code)
            if status == 200:
                try:
                    payload = resp.json()
                except ValueError as exc:
                    raise EngineError(f"HTTP 200 with unparsable body: {exc}", retryable=False) from exc
                try:
                    answer = parse_response(payload, model=model, price=self.price)   # EngineError here carries raw + cost
                except EngineError:
                    raise
                except Exception as exc:  # noqa: BLE001 — a parser bug must not lose a paid answer
                    raise EngineError(f"openai: parse failure {type(exc).__name__}: {exc}", retryable=False, raw=payload) from exc
                answer.latency_ms = latency_ms
                return answer
            message = self._error_message(resp)
            last_error = EngineError(f"HTTP {status}: {message}", retryable=status in RETRYABLE_STATUS, status=status)
            if not last_error.retryable:
                raise last_error
            self._backoff(attempt, resp.headers.get("Retry-After"))
        assert last_error is not None
        raise last_error

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        if attempt >= self.retries:
            return  # no sleep after the final attempt
        if retry_after is not None:
            try:
                self._sleep(min(MAX_BACKOFF_S, max(0.0, float(retry_after))))
                return
            except ValueError:
                pass
        self._sleep(min(MAX_BACKOFF_S, (2 ** attempt) + random.uniform(0, 0.5)))

    @staticmethod
    def _error_message(resp: Any) -> str:
        try:
            payload = resp.json()
            return str((payload.get("error") or {}).get("message") or payload)[:300]
        except Exception:  # noqa: BLE001
            return str(getattr(resp, "text", ""))[:300]
