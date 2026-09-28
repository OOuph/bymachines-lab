"""S1 — test_openai_adapter_parses_fixture: request shape, response parsing, cost per SPEC §10, retries."""

from __future__ import annotations

import pytest

from lab.config import Location
from lab.engines.base import EngineError
from lab.engines.openai import OpenAIEngine, build_request, compute_cost, parse_response

PRICE = {
    "per_1m_input": {"gpt-6-sol": 2.00, "gpt-6-luna": 0.10},
    "per_1m_cached_input": {"gpt-6-sol": 0.20, "gpt-6-luna": 0.01},
    "per_1m_output": {"gpt-6-sol": 10.00, "gpt-6-luna": 0.50},
    "per_search_call": 0.010,
}
LISBON = Location(key="lisbon", city="Lisbon", region="Lisbon", country="PT", timezone="Europe/Lisbon", dataforseo_location_code=1011742)


def test_build_request_with_search():
    body = build_request("best immigration lawyer for a Portugal D7 visa", LISBON, model="gpt-6-sol", effort="none",
                         search=True, search_context_size="medium", include_sources=True)
    assert body["model"] == "gpt-6-sol"
    assert body["input"] == "best immigration lawyer for a Portugal D7 visa"
    assert body["reasoning"] == {"effort": "none"}
    (tool,) = body["tools"]
    assert tool["type"] == "web_search" and tool["search_context_size"] == "medium"
    assert tool["user_location"] == {"type": "approximate", "country": "PT", "city": "Lisbon", "region": "Lisbon", "timezone": "Europe/Lisbon"}
    assert body["include"] == ["web_search_call.action.sources"]


def test_build_request_without_search_has_no_tools():
    body = build_request("choose exactly one firm", LISBON, model="gpt-6-sol", effort="none",
                         search=False, search_context_size="medium", include_sources=True)
    assert "tools" not in body and "include" not in body
    assert body["reasoning"] == {"effort": "none"}


def test_build_request_location_none_sends_explicit_neutral_type():
    # SPEC §4: omitted user_location defaults to the US at OpenAI → the adapter always sends the object
    body = build_request("q", None, model="gpt-6-sol", effort="none", search=True, search_context_size="low", include_sources=False)
    assert body["tools"][0]["user_location"] == {"type": "approximate"}
    assert "include" not in body


def test_parse_search_fixture(openai_search_fixture):
    ans = parse_response(openai_search_fixture, model="gpt-6-sol", price=PRICE)
    assert ans.text.startswith("For a Portugal D7 visa")
    assert ans.searched is True and ans.n_search == 1
    assert [c.position for c in ans.citations] == [1, 2, 3]
    assert [c.url for c in ans.citations] == [
        "https://www.example-firm.pt/d7-visa",
        "https://another-firm.example.com/immigration",
        "https://www.expat-guide.example/portugal-d7-lawyers",
    ]
    assert [c.domain for c in ans.citations] == ["example-firm.pt", "another-firm.example.com", "expat-guide.example"]
    assert ans.citations[0].title == "D7 visa lawyers — Example Firm"
    assert ans.usage["input_tokens"] == 9000 and ans.usage["output_tokens"] == 800
    assert ans.cost_usd == pytest.approx(0.036, abs=1e-9)   # SPEC §10 reference
    assert ans.raw is openai_search_fixture
    assert len(ans.sources) == 3


LIVE_FIXTURE = __import__("pathlib").Path(__file__).parent / "fixtures" / "openai_search_live.json"


@pytest.mark.skipif(not LIVE_FIXTURE.exists(), reason="recorded live answer is local-only (never published, decision 29)")
def test_parse_live_recorded_fixture():
    """Recorded live response (S1 smoke 2026-09-28): the parser must cope with the full field set of a real answer."""
    import json

    data = json.loads(LIVE_FIXTURE.read_text())
    ans = parse_response(data, model="gpt-6-sol", price=PRICE)
    assert ans.searched and ans.n_search >= 1
    assert len(ans.citations) >= 3 and all(c.domain and c.url.startswith("http") for c in ans.citations)
    assert len(ans.text) > 500
    assert ans.usage["input_tokens"] > 10000 and ans.usage["input_tokens_details"]["cached_tokens"] > 0
    assert 0.02 < ans.cost_usd < 0.08
    assert len(ans.sources) > len(ans.citations)


def test_parse_nosearch_fixture(openai_nosearch_fixture):
    ans = parse_response(openai_nosearch_fixture, model="gpt-6-sol", price=PRICE)
    assert ans.searched is False and ans.n_search == 0
    assert ans.citations == [] and ans.sources == []
    assert ans.text == "Example Firm Advogados — https://www.example-firm.pt"
    assert ans.cost_usd == pytest.approx(120 * 2 / 1e6 + 20 * 10 / 1e6, abs=1e-12)


def test_compute_cost_reference_and_cached_tokens():
    usage = {"input_tokens": 9000, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": 800}
    assert compute_cost(usage, 1, "gpt-6-sol", PRICE) == pytest.approx(0.036)
    usage_cached = {"input_tokens": 9000, "input_tokens_details": {"cached_tokens": 4000}, "output_tokens": 800}
    expected = 5000 * 2 / 1e6 + 4000 * 0.20 / 1e6 + 800 * 10 / 1e6 + 2 * 0.010
    assert compute_cost(usage_cached, 2, "gpt-6-sol", PRICE) == pytest.approx(expected)
    assert compute_cost(usage, 1, "gpt-6-luna", PRICE) == pytest.approx(9000 * 0.10 / 1e6 + 800 * 0.5 / 1e6 + 0.010)


def test_parse_rejects_incomplete_response_but_keeps_raw_and_cost(openai_search_fixture):
    bad = dict(openai_search_fixture)
    bad["status"] = "incomplete"
    with pytest.raises(EngineError) as ei:
        parse_response(bad, model="gpt-6-sol", price=PRICE)
    assert ei.value.raw is bad and ei.value.cost_usd == pytest.approx(0.036)   # paid → stored by the runner, never lost


def test_parse_includes_refusal_part_in_text(openai_nosearch_fixture):
    data = dict(openai_nosearch_fixture)
    data["output"] = [{"type": "message", "role": "assistant", "status": "completed",
                       "content": [{"type": "refusal", "refusal": "I can't recommend a specific firm."}]}]
    ans = parse_response(data, model="gpt-6-sol", price=PRICE)
    assert ans.text == "I can't recommend a specific firm."


def test_parse_without_usage_costs_only_the_search(openai_search_fixture, caplog):
    data = dict(openai_search_fixture)
    del data["usage"]
    ans = parse_response(data, model="gpt-6-sol", price=PRICE)
    assert ans.cost_usd == pytest.approx(0.010) and ans.usage == {}


def test_retry_after_is_capped(openai_search_fixture):
    calls = []

    def fake_post(url, json, headers, timeout):
        calls.append(1)
        if len(calls) == 1:
            return _FakeResponse(429, {"error": {"message": "slow down"}}, {"Retry-After": "86400"})
        return _FakeResponse(200, openai_search_fixture)

    sleeps = []
    engine = OpenAIEngine(api_key="k", model="gpt-6-sol", effort="none", search_context_size="medium",
                          include_sources=True, price=PRICE, timeout_s=5, retries=3, post=fake_post, sleep=sleeps.append)
    engine.ask("q", LISBON, {"search": True})
    assert sleeps == [60.0]


class _FakeResponse:
    def __init__(self, status_code, payload, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = "x"

    def json(self):
        return self._payload


def test_ask_retries_on_429_then_succeeds(openai_search_fixture, monkeypatch):
    calls = []

    def fake_post(url, json, headers, timeout):
        calls.append(json)
        if len(calls) == 1:
            return _FakeResponse(429, {"error": {"message": "slow down"}}, {"Retry-After": "0"})
        return _FakeResponse(200, openai_search_fixture)

    sleeps = []
    engine = OpenAIEngine(api_key="k", model="gpt-6-sol", effort="none", search_context_size="medium",
                          include_sources=True, price=PRICE, timeout_s=5, retries=3, post=fake_post, sleep=sleeps.append)
    ans = engine.ask("q", LISBON, {"search": True})
    assert len(calls) == 2 and ans.searched and ans.latency_ms >= 0
    assert sleeps == [0.0]  # honoured Retry-After: 0


def test_ask_gives_up_after_retries(monkeypatch):
    def fake_post(url, json, headers, timeout):
        return _FakeResponse(500, {"error": {"message": "boom"}})

    engine = OpenAIEngine(api_key="k", model="gpt-6-sol", effort="none", search_context_size="medium",
                          include_sources=True, price=PRICE, timeout_s=5, retries=2, post=fake_post, sleep=lambda s: None)
    with pytest.raises(EngineError) as ei:
        engine.ask("q", LISBON, {"search": True})
    assert "500" in str(ei.value)


def test_ask_does_not_retry_client_errors():
    calls = []

    def fake_post(url, json, headers, timeout):
        calls.append(1)
        return _FakeResponse(400, {"error": {"message": "bad model"}})

    engine = OpenAIEngine(api_key="k", model="gpt-6-sol", effort="none", search_context_size="medium",
                          include_sources=True, price=PRICE, timeout_s=5, retries=3, post=fake_post, sleep=lambda s: None)
    with pytest.raises(EngineError, match="400"):
        engine.ask("q", LISBON, {"search": True})
    assert len(calls) == 1


def test_ask_uses_model_option_override(openai_search_fixture):
    seen = {}

    def fake_post(url, json, headers, timeout):
        seen.update(json)
        return _FakeResponse(200, openai_search_fixture)

    engine = OpenAIEngine(api_key="k", model="gpt-6-sol", effort="none", search_context_size="medium",
                          include_sources=True, price=PRICE, timeout_s=5, retries=1, post=fake_post, sleep=lambda s: None)
    engine.ask("q", LISBON, {"search": True, "model": "gpt-6-luna"})
    assert seen["model"] == "gpt-6-luna"
