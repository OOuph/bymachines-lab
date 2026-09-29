"""S2 — Gemini generateContent + google_search grounding (no location parameter).

Request/retry tests run everywhere on a minimal synthetic payload; parsing tests on the recorded live answer run only
where that local-only fixture exists (raw answers are never published, decision 29).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lab.engines.base import EngineError
from lab.engines.gemini import GeminiEngine, build_request, parse_response

FIXTURES = Path(__file__).parent / "fixtures"
LIVE = FIXTURES / "gemini_live.json"
needs_live = pytest.mark.skipif(not LIVE.exists(), reason="recorded live answer is local-only (never published, decision 29)")
PRICE = {"per_1m_input": {"gemini-3.8-flash": 0.75}, "per_1m_output": {"gemini-3.8-flash": 3.75},
         "per_search_query": 0.014, "free_search_queries_per_month": 5000}

# minimal payload in the documented generateContent shape (docs/api-check-2026-09-28.md)
MINIMAL = {
    "candidates": [{
        "content": {"parts": [{"text": "Example Firm is often named. "}, {"text": "hidden reasoning", "thought": True}, {"text": "Another Firm too."}], "role": "model"},
        "finishReason": "STOP",
        "groundingMetadata": {
            "webSearchQueries": ["best D7 lawyer Portugal", "D7 visa law firm Lisbon"],
            "groundingChunks": [
                {"web": {"uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc", "title": "example-firm.pt"}},
                {"web": {"uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/def", "title": "www.another.example.com"}},
            ],
            "groundingSupports": [],
        },
    }],
    "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 200, "thoughtsTokenCount": 300, "totalTokenCount": 600},
    "modelVersion": "gemini-3.8-flash",
}


@pytest.fixture
def live() -> dict:
    return json.loads(LIVE.read_text())


def test_build_request_grounding_and_no_location():
    body = build_request("q", search=True)
    assert body["contents"] == [{"parts": [{"text": "q"}]}]
    assert body["tools"] == [{"google_search": {}}]
    assert "toolConfig" not in body


def test_build_request_without_search_has_no_tools():
    assert "tools" not in build_request("q", search=False)


def test_parse_minimal_payload_skips_thoughts_and_reads_domains_from_titles():
    ans = parse_response(MINIMAL, model="gemini-3.8-flash", price=PRICE)
    assert ans.text == "Example Firm is often named. Another Firm too."
    assert ans.searched and ans.n_search == 2
    assert [c.domain for c in ans.citations] == ["example-firm.pt", "another.example.com"]
    assert ans.citations[0].url.startswith("https://vertexaisearch.cloud.google.com/")   # redirect kept, not resolved
    assert ans.cost_usd == pytest.approx((100 * 0.75 + (200 + 300) * 3.75) / 1e6)          # thinking tokens billed as output


@needs_live
def test_parse_live_response(live):
    ans = parse_response(live, model="gemini-3.8-flash", price=PRICE)
    assert ans.searched and ans.n_search == 2                                  # two executed search queries → two billable searches
    assert len(ans.citations) == 8
    assert ans.citations[0].domain == "reddit.com" and ans.citations[0].position == 1
    assert ans.citations[0].url.startswith("https://vertexaisearch.cloud.google.com/")
    assert ans.text.startswith("There is no single") and len(ans.text) > 3000
    assert ans.usage["promptTokenCount"] == 388 and ans.usage["thoughtsTokenCount"] == 1011
    assert ans.cost_usd == pytest.approx((388 * 0.75 + (1119 + 1011) * 3.75) / 1e6)
    assert ans.model == "gemini-3.8-flash"


def test_parse_answer_from_weights_without_search():
    data = json.loads(json.dumps(MINIMAL))
    data["candidates"][0].pop("groundingMetadata")
    ans = parse_response(data, model="gemini-3.8-flash", price=PRICE)
    assert ans.searched is False and ans.n_search == 0 and ans.citations == []


def test_parse_blocked_or_truncated_keeps_raw():
    data = json.loads(json.dumps(MINIMAL))
    data["candidates"][0]["finishReason"] = "SAFETY"
    with pytest.raises(EngineError) as ei:
        parse_response(data, model="gemini-3.8-flash", price=PRICE)
    assert ei.value.raw is data and ei.value.cost_usd > 0


def test_chunk_title_that_is_not_a_domain_gives_no_domain():
    data = json.loads(json.dumps(MINIMAL))
    data["candidates"][0]["groundingMetadata"]["groundingChunks"][0]["web"]["title"] = "Best D7 lawyers in Portugal"   # a page title
    ans = parse_response(data, model="gemini-3.8-flash", price=PRICE)
    assert ans.citations[0].domain == "" and ans.citations[1].domain == "another.example.com"


def test_parse_no_candidates_is_error():
    data = {"promptFeedback": {"blockReason": "OTHER"}, "usageMetadata": {"promptTokenCount": 10}}
    with pytest.raises(EngineError):
        parse_response(data, model="gemini-3.8-flash", price=PRICE)


class _Resp:
    def __init__(self, status, payload, headers=None):
        self.status_code, self._p, self.headers, self.text = status, payload, headers or {}, "x"

    def json(self):
        return self._p


def test_engine_uses_model_url_and_key_header():
    seen = {}

    def fake_post(url, json, headers, timeout):
        seen.update(url=url, headers=headers, body=json)
        return _Resp(200, MINIMAL)

    eng = GeminiEngine(api_key="k", model="gemini-3.8-flash", price=PRICE, timeout_s=5, retries=1, post=fake_post, sleep=lambda s: None)
    ans = eng.ask("q", None, {"search": True})
    assert seen["url"].endswith("/models/gemini-3.8-flash:generateContent")
    assert seen["headers"]["x-goog-api-key"] == "k" and ans.n_search == 2


def test_engine_retries_on_503_then_ok():
    calls = []

    def fake_post(url, json, headers, timeout):
        calls.append(1)
        return _Resp(503, {"error": {"message": "overloaded"}}) if len(calls) < 3 else _Resp(200, MINIMAL)

    eng = GeminiEngine(api_key="k", model="gemini-3.8-flash", price=PRICE, timeout_s=5, retries=3, post=fake_post, sleep=lambda s: None)
    assert eng.ask("q", None, {"search": True}).searched and len(calls) == 3
