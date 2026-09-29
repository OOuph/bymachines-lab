"""Perplexity Agent API with the Sonar model pinned (`perplexity/sonar`) + `web_search` tool.

Why the Agent API: legacy Sonar chat completions lost support on 2026-09-27 and are re-routed to presets that run other
vendors' models; `POST /v1/agent` with `model: perplexity/sonar` is the only way to keep measuring Sonar itself
(docs/api-check-2026-09-28.md). Response: `output[]` steps — `search_results` (the sources) and `message` (the text);
`usage.cost.total_cost` is the authoritative cost. Tier 0 allows one request per second per organisation → throttled.
"""

from __future__ import annotations

import time
from typing import Any, Callable

import httpx

from lab.config import EngineSpec, Location
from lab.engines.base import Answer, Citation, EngineError, domain_of, http_with_retries

API_URL = "https://api.perplexity.ai/v1/agent"


def build_request(prompt_text: str, location: Location | None, *, model: str, search: bool, search_context_size: str) -> dict[str, Any]:
    body: dict[str, Any] = {"model": model, "input": prompt_text}
    if search:
        tool: dict[str, Any] = {"type": "web_search", "search_context_size": search_context_size}
        if location is not None:
            tool["user_location"] = {"country": location.country, "region": location.region, "city": location.city}
        body["tools"] = [tool]
    return body


def _token_cost(usage: dict[str, Any], n_search: int, model: str, price: dict[str, Any]) -> float:
    p_in = float((price.get("per_1m_input") or {}).get(model, 0.0))
    p_out = float((price.get("per_1m_output") or {}).get(model, 0.0))
    return (int(usage.get("input_tokens", 0) or 0) * p_in + int(usage.get("output_tokens", 0) or 0) * p_out) / 1e6 \
        + n_search * float(price.get("per_search_call", 0.0))


def parse_response(data: dict[str, Any], *, model: str, price: dict[str, Any]) -> Answer:
    texts: list[str] = []
    citations: list[Citation] = []
    n_search_steps = 0
    for step in data.get("output") or []:
        stype = step.get("type")
        if stype == "search_results":
            n_search_steps += 1
            for res in step.get("results") or []:
                url = str(res.get("url") or "")
                if url:
                    citations.append(Citation(position=len(citations) + 1, url=url, title=str(res.get("title") or ""), domain=domain_of(url)))
        elif stype == "message":
            for part in step.get("content") or []:
                if isinstance(part, dict) and part.get("text"):
                    texts.append(str(part["text"]))
    usage = dict(data.get("usage") or {})
    details = ((usage.get("tool_calls_details") or {}).get("search_web") or {})
    n_search = int(details.get("invocation") or 0) or n_search_steps
    cost_block = usage.get("cost") or {}
    if "total_cost" in cost_block:
        cost = float(cost_block["total_cost"])
    else:
        cost = _token_cost(usage, n_search, model, price)
    status = data.get("status")
    if status not in (None, "completed"):
        raise EngineError(f"agent response status {status!r}: {data.get('incomplete_details') or data.get('error') or ''}",
                          retryable=False, raw=data, cost_usd=cost)
    return Answer(text="".join(texts).strip(), citations=citations, raw=data, cost_usd=cost, searched=n_search > 0,
                  n_search=n_search, latency_ms=0, model=str(data.get("model") or model), usage=usage, sources=[c.url for c in citations])


class PerplexityEngine:
    id = "perplexity"

    def __init__(self, *, api_key: str, model: str, search_context_size: str, price: dict[str, Any], timeout_s: float,
                 retries: int, min_interval_s: float = 1.0, post: Callable[..., Any] | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        self.api_key = api_key
        self.model = model
        self.search_context_size = search_context_size
        self.price = price
        self.timeout_s = timeout_s
        self.retries = retries
        self.min_interval_s = min_interval_s
        self._post = post or httpx.post
        self._sleep = sleep
        self._clock = clock
        self._last_start: float | None = None
        import threading

        self._lock = threading.Lock()

    @classmethod
    def from_spec(cls, spec: EngineSpec, tunables: dict[str, Any], api_key: str) -> "PerplexityEngine":
        o = spec.options
        max_qps = float(o.get("max_qps", 1.0) or 1.0)
        return cls(api_key=api_key, model=spec.model,
                   search_context_size=str(o.get("search_context_size", tunables.get("default_search_context_size", "medium"))),
                   price=spec.price, timeout_s=float(o.get("timeout_s", 120)), retries=int(tunables.get("retries", 3)),
                   min_interval_s=1.0 / max_qps)

    def _throttle(self) -> None:
        with self._lock:
            now = self._clock()
            if self._last_start is not None:
                wait = self._last_start + self.min_interval_s - now
                if wait > 0:
                    self._sleep(wait)
            self._last_start = self._clock()

    def ask(self, prompt_text: str, location: Location | None, options: dict[str, Any]) -> Answer:
        model = str(options.get("model") or self.model)
        body = build_request(prompt_text, location, model=model, search=bool(options.get("search", True)),
                             search_context_size=str(options.get("search_context_size") or self.search_context_size))
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

        def do_request():
            self._throttle()
            return self._post(API_URL, json=body, headers=headers, timeout=self.timeout_s)

        t0 = time.monotonic()
        resp = http_with_retries(do_request, retries=self.retries, sleep=self._sleep, what="perplexity")
        latency_ms = int((time.monotonic() - t0) * 1000)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise EngineError(f"perplexity: HTTP 200 with unparsable body: {exc}") from exc
        try:
            answer = parse_response(payload, model=model, price=self.price)
        except EngineError:
            raise
        except Exception as exc:  # noqa: BLE001 — a parser bug must not lose a paid answer
            raise EngineError(f"perplexity: parse failure {type(exc).__name__}: {exc}", retryable=False, raw=payload) from exc
        answer.latency_ms = latency_ms
        return answer
