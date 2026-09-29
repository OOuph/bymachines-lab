"""S2 — Perplexity Agent API adapter (`perplexity/sonar` pinned + web_search tool).

Request/retry/throttle tests run everywhere on a minimal synthetic payload; parsing tests on the recorded live answer
run only where that local-only fixture exists (raw answers are never published, decision 29).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lab.config import Location
from lab.engines.base import EngineError
from lab.engines.perplexity import PerplexityEngine, build_request, parse_response

FIXTURES = Path(__file__).parent / "fixtures"
LIVE = FIXTURES / "perplexity_live.json"
needs_live = pytest.mark.skipif(not LIVE.exists(), reason="recorded live answer is local-only (never published, decision 29)")
PRICE = {"per_1m_input": {"perplexity/sonar": 0.25}, "per_1m_output": {"perplexity/sonar": 2.50}, "per_search_call": 0.0025}
LISBON = Location(key="lisbon", city="Lisbon", region="Lisbon", country="PT", timezone="Europe/Lisbon", dataforseo_location_code=1011742)

# minimal payload in the documented Agent API shape (docs/api-check-2026-09-28.md)
MINIMAL = {
    "id": "agent_min", "status": "completed", "model": "perplexity/sonar",
    "output": [
        {"type": "search_results", "results": [{"title": "A", "url": "https://www.example-firm.pt/d7"}, {"title": "B", "url": "https://another.example.com/x"}]},
        {"type": "message", "content": [{"type": "text", "text": "Example Firm and Another are named."}]},
    ],
    "usage": {"input_tokens": 1000, "output_tokens": 100, "tool_calls_details": {"search_web": {"cost_usd": 0.0025, "invocation": 1}},
              "cost": {"input_cost": 0.00025, "output_cost": 0.00025, "tool_calls_cost": 0.0025, "total_cost": 0.003, "currency": "USD"}},
}


@pytest.fixture
def live() -> dict:
    return json.loads(LIVE.read_text())


def test_build_request_pins_model_and_tool():
    body = build_request("q", LISBON, model="perplexity/sonar", search=True, search_context_size="medium")
    assert body["model"] == "perplexity/sonar" and body["input"] == "q"
    (tool,) = body["tools"]
    assert tool["type"] == "web_search" and tool["search_context_size"] == "medium"
    assert tool["user_location"] == {"country": "PT", "region": "Lisbon", "city": "Lisbon"}


def test_build_request_without_search_or_location():
    body = build_request("q", None, model="perplexity/sonar", search=False, search_context_size="medium")
    assert "tools" not in body
    body2 = build_request("q", None, model="perplexity/sonar", search=True, search_context_size="low")
    assert "user_location" not in body2["tools"][0]


def test_parse_minimal_payload():
    ans = parse_response(MINIMAL, model="perplexity/sonar", price=PRICE)
    assert ans.searched and ans.n_search == 1 and [c.domain for c in ans.citations] == ["example-firm.pt", "another.example.com"]
    assert ans.text == "Example Firm and Another are named." and ans.cost_usd == pytest.approx(0.003)


@needs_live
def test_parse_live_response(live):
    ans = parse_response(live, model="perplexity/sonar", price=PRICE)
    assert ans.model == "perplexity/sonar" or ans.model.endswith("sonar")
    assert ans.searched and ans.n_search == 1
    assert len(ans.citations) == 15 and ans.citations[0].url == "https://chagasadvogados.pt/en" and ans.citations[0].position == 1
    assert ans.citations[0].domain == "chagasadvogados.pt"
    assert ans.text.startswith("“Best” depends") and len(ans.text) > 1000
    assert ans.cost_usd == pytest.approx(0.00413, abs=1e-6)          # usage.cost.total_cost is authoritative
    assert ans.usage["input_tokens"] == 2593 and ans.usage["output_tokens"] == 391
    assert ans.raw is live


def test_parse_falls_back_to_price_table_when_cost_missing():
    data = json.loads(json.dumps(MINIMAL))
    del data["usage"]["cost"]
    ans = parse_response(data, model="perplexity/sonar", price=PRICE)
    assert ans.cost_usd == pytest.approx((1000 * 0.25 + 100 * 2.50) / 1e6 + 0.0025)


def test_parse_incomplete_keeps_raw():
    data = dict(MINIMAL)
    data["status"] = "failed"
    with pytest.raises(EngineError) as ei:
        parse_response(data, model="perplexity/sonar", price=PRICE)
    assert ei.value.raw is data and ei.value.cost_usd == pytest.approx(0.003)


class _Resp:
    def __init__(self, status, payload, headers=None):
        self.status_code, self._p, self.headers, self.text = status, payload, headers or {}, "x"

    def json(self):
        return self._p


def test_engine_throttles_to_one_request_per_second():
    clock = {"t": 100.0}
    sleeps = []

    def fake_sleep(s):
        sleeps.append(round(s, 3))
        clock["t"] += s

    def fake_post(url, json, headers, timeout):
        clock["t"] += 0.2   # the call itself takes 200 ms
        return _Resp(200, MINIMAL)

    eng = PerplexityEngine(api_key="k", model="perplexity/sonar", search_context_size="medium", price=PRICE, timeout_s=5,
                           retries=1, min_interval_s=1.0, post=fake_post, sleep=fake_sleep, clock=lambda: clock["t"])
    eng.ask("a", LISBON, {"search": True})
    eng.ask("b", LISBON, {"search": True})
    assert sleeps and sleeps[0] == pytest.approx(0.8, abs=0.01)   # second call waits for the 1 s window


def test_engine_retries_429_with_retry_after():
    calls = []

    def fake_post(url, json, headers, timeout):
        calls.append(1)
        return _Resp(429, {"error": {"message": "rate"}}, {"Retry-After": "2"}) if len(calls) == 1 else _Resp(200, MINIMAL)

    sleeps = []
    eng = PerplexityEngine(api_key="k", model="perplexity/sonar", search_context_size="medium", price=PRICE, timeout_s=5,
                           retries=2, min_interval_s=0.0, post=fake_post, sleep=sleeps.append, clock=lambda: 0.0)
    ans = eng.ask("a", LISBON, {"search": True})
    assert ans.searched and len(calls) == 2 and 2.0 in sleeps


def test_engine_does_not_retry_after_read_timeout():
    import httpx

    calls = []

    def fake_post(url, json, headers, timeout):
        calls.append(1)
        raise httpx.ReadTimeout("late")

    eng = PerplexityEngine(api_key="k", model="perplexity/sonar", search_context_size="medium", price=PRICE, timeout_s=5,
                           retries=3, min_interval_s=0.0, post=fake_post, sleep=lambda s: None, clock=lambda: 0.0)
    with pytest.raises(EngineError) as ei:
        eng.ask("a", LISBON, {"search": True})
    assert len(calls) == 1 and not ei.value.retryable
