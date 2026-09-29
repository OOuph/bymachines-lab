"""Gemini `generateContent` with Google Search grounding (legacy surface, fully supported; keeps `groundingMetadata`).

No location parameter exists in the Gemini API for search grounding (only Vertex AI has one) → the engine runs once per
prompt with location 'n/a'. Citations come from `groundingChunks[].web`: `title` carries the source domain, `uri` is a
Google redirect that is stored as-is (never resolved — the terms forbid collecting links). Search queries are billed
per executed query beyond a monthly free quota; the runner applies that surcharge from the monthly counter, the adapter
reports only token cost and `n_search` (docs/api-check-2026-09-28.md).
"""

from __future__ import annotations

import time
from typing import Any, Callable

import httpx

from lab.config import EngineSpec, Location
from lab.engines.base import Answer, Citation, EngineError, domain_of, http_with_retries

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
REDIRECT_HOST = "vertexaisearch.cloud.google.com"


def build_request(prompt_text: str, *, search: bool) -> dict[str, Any]:
    body: dict[str, Any] = {"contents": [{"parts": [{"text": prompt_text}]}]}
    if search:
        body["tools"] = [{"google_search": {}}]
    return body


def _token_cost(usage: dict[str, Any], model: str, price: dict[str, Any]) -> float:
    p_in = float((price.get("per_1m_input") or {}).get(model, 0.0))
    p_out = float((price.get("per_1m_output") or {}).get(model, 0.0))
    prompt = int(usage.get("promptTokenCount", 0) or 0)
    out = int(usage.get("candidatesTokenCount", 0) or 0) + int(usage.get("thoughtsTokenCount", 0) or 0)   # thinking billed as output
    return (prompt * p_in + out * p_out) / 1e6


def parse_response(data: dict[str, Any], *, model: str, price: dict[str, Any]) -> Answer:
    usage = dict(data.get("usageMetadata") or {})
    cost = _token_cost(usage, model, price)
    candidates = data.get("candidates") or []
    if not candidates:
        raise EngineError(f"gemini: no candidates: {data.get('promptFeedback') or data.get('error') or ''}", retryable=False, raw=data, cost_usd=cost)
    cand = candidates[0]
    parts = (cand.get("content") or {}).get("parts") or []
    text = "".join(str(p.get("text", "")) for p in parts if isinstance(p, dict) and not p.get("thought"))
    gm = cand.get("groundingMetadata") or {}
    queries = [q for q in (gm.get("webSearchQueries") or []) if q]
    citations: list[Citation] = []
    for chunk in gm.get("groundingChunks") or []:
        web = (chunk or {}).get("web") or {}
        uri, title = str(web.get("uri") or ""), str(web.get("title") or "")
        if not uri and not title:
            continue
        host = domain_of(uri)
        if host and host != REDIRECT_HOST:
            domain = host                                   # a direct URL: the host is the source domain
        else:
            t = title.strip().lower()
            domain = t.removeprefix("www.") if "." in t and " " not in t and "/" not in t else ""   # title is the domain, or unknown
        citations.append(Citation(position=len(citations) + 1, url=uri, title=title, domain=domain))
    finish = cand.get("finishReason")
    if finish not in (None, "STOP"):
        raise EngineError(f"gemini: finishReason {finish}", retryable=False, raw=data, cost_usd=cost)
    return Answer(text=text.strip(), citations=citations, raw=data, cost_usd=cost, searched=len(queries) > 0, n_search=len(queries),
                  latency_ms=0, model=model, usage=usage, sources=[c.url for c in citations])


class GeminiEngine:
    id = "gemini"

    def __init__(self, *, api_key: str, model: str, price: dict[str, Any], timeout_s: float, retries: int,
                 post: Callable[..., Any] | None = None, sleep: Callable[[float], None] = time.sleep):
        self.api_key = api_key
        self.model = model
        self.price = price
        self.timeout_s = timeout_s
        self.retries = retries
        self._post = post or httpx.post
        self._sleep = sleep

    @classmethod
    def from_spec(cls, spec: EngineSpec, tunables: dict[str, Any], api_key: str) -> "GeminiEngine":
        return cls(api_key=api_key, model=spec.model, price=spec.price, timeout_s=float(spec.options.get("timeout_s", 120)),
                   retries=int(tunables.get("retries", 3)))

    def ask(self, prompt_text: str, location: Location | None, options: dict[str, Any]) -> Answer:
        model = str(options.get("model") or self.model)
        body = build_request(prompt_text, search=bool(options.get("search", True)))
        headers = {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}
        url = API_URL.format(model=model)
        t0 = time.monotonic()
        resp = http_with_retries(lambda: self._post(url, json=body, headers=headers, timeout=self.timeout_s),
                                 retries=self.retries, sleep=self._sleep, what="gemini")
        latency_ms = int((time.monotonic() - t0) * 1000)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise EngineError(f"gemini: HTTP 200 with unparsable body: {exc}") from exc
        try:
            answer = parse_response(payload, model=model, price=self.price)
        except EngineError:
            raise
        except Exception as exc:  # noqa: BLE001 — a parser bug must not lose a paid answer
            raise EngineError(f"gemini: parse failure {type(exc).__name__}: {exc}", retryable=False, raw=payload) from exc
        answer.latency_ms = latency_ms
        return answer
