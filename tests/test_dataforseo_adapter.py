"""S2 — DataForSEO Google AI Mode adapter: live endpoint and the standard task queue, on a doc-shaped fixture + a recorded error."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lab.config import Location
from lab.engines.base import EngineError
from lab.engines.dataforseo import DataForSEOEngine, build_task, parse_response

FIXTURES = Path(__file__).parent / "fixtures"
PRICE = {"per_request": {"standard": 0.0012, "priority": 0.0024, "live": 0.004}}
LISBON = Location(key="lisbon", city="Lisbon", region="Lisbon", country="PT", timezone="Europe/Lisbon", dataforseo_location_code=1011742)


@pytest.fixture
def ai_mode() -> dict:
    return json.loads((FIXTURES / "dataforseo_ai_mode.json").read_text())


@pytest.fixture
def error_40104() -> dict:
    return json.loads((FIXTURES / "dataforseo_error_40104.json").read_text())


def test_build_task_uses_city_code_language_device():
    task = build_task("q", LISBON, language_code="en", device="desktop")
    assert task == {"keyword": "q", "location_code": 1011742, "language_code": "en", "device": "desktop"}


def test_build_task_requires_location_code():
    with pytest.raises(EngineError):
        build_task("q", None, language_code="en", device="desktop")


def test_parse_ai_mode_fixture(ai_mode):
    ans = parse_response(ai_mode, price=PRICE, queue="live")
    assert ans.searched and ans.n_search == 1
    assert ans.text.startswith("For a Portugal D7 visa") and "[[1]]" in ans.text            # markdown kept verbatim
    assert [c.url for c in ans.citations] == [
        "https://www.example-firm.pt/d7-visa",
        "https://another-firm.example.com/immigration",
        "https://www.expat-guide.example/portugal-d7-lawyers",
    ]                                                                                     # in order, duplicate url dropped
    assert [c.domain for c in ans.citations] == ["example-firm.pt", "another-firm.example.com", "expat-guide.example"]
    assert ans.cost_usd == pytest.approx(0.004)                                            # the task's own cost field
    assert ans.model == "google-ai-mode"


def test_parse_unknown_item_type_is_an_error_with_raw_and_cost(ai_mode):
    data = json.loads(json.dumps(ai_mode))
    data["tasks"][0]["result"][0]["items"][0]["type"] = "ai_mode_answer"      # a shape the parser does not know
    with pytest.raises(EngineError) as ei:
        parse_response(data, price=PRICE, queue="live")
    assert "ai_mode_answer" in str(ei.value) and ei.value.raw is data and ei.value.cost_usd == pytest.approx(0.004) and not ei.value.retryable


def test_parse_no_ai_mode_block_is_empty_not_error(ai_mode):
    data = json.loads(json.dumps(ai_mode))
    data["tasks"][0]["result"][0]["items"] = []
    data["tasks"][0]["result"][0]["items_count"] = 0
    ans = parse_response(data, price=PRICE, queue="live")
    assert ans.searched is False and ans.text == "" and ans.citations == [] and ans.cost_usd == pytest.approx(0.004)


def test_parse_recorded_account_error_is_not_retryable(error_40104):
    with pytest.raises(EngineError) as ei:
        parse_response(error_40104, price=PRICE, queue="live")
    assert "40104" in str(ei.value) and ei.value.retryable is False and ei.value.raw is error_40104


def test_parse_task_level_error_keeps_raw(ai_mode):
    data = json.loads(json.dumps(ai_mode))
    data["tasks"][0].update({"status_code": 40501, "status_message": "Invalid Field", "result": None})
    with pytest.raises(EngineError) as ei:
        parse_response(data, price=PRICE, queue="live")
    assert ei.value.raw is data


class _Resp:
    def __init__(self, status, payload):
        self.status_code, self._p, self.headers, self.text = status, payload, {}, "x"

    def json(self):
        return self._p


def test_engine_live_queue_posts_one_task(ai_mode):
    seen = {}

    def fake_post(url, json, headers, timeout):
        seen.update(url=url, body=json, headers=headers)
        return _Resp(200, ai_mode)

    eng = DataForSEOEngine(login="l", password="p", queue="live", language_code="en", device="desktop", price=PRICE,
                           timeout_s=5, retries=1, post=fake_post, get=None, sleep=lambda s: None)
    ans = eng.ask("q", LISBON, {"search": True})
    assert seen["url"].endswith("/v3/serp/google/ai_mode/live/advanced") and seen["body"] == [build_task("q", LISBON, language_code="en", device="desktop")]
    assert seen["headers"]["Authorization"].startswith("Basic ") and ans.cost_usd == pytest.approx(0.004)


def test_engine_live_retries_transient_empty_tasks_once(ai_mode):
    calls = []

    def fake_post(url, json, headers, timeout):
        calls.append(1)
        if len(calls) == 1:
            return _Resp(200, {"status_code": 20000, "status_message": "Ok.", "tasks_count": 0, "tasks_error": 0, "cost": 0, "tasks": []})
        return _Resp(200, ai_mode)

    eng = DataForSEOEngine(login="l", password="p", queue="live", language_code="en", device="desktop", price=PRICE,
                           timeout_s=5, retries=2, post=fake_post, get=None, sleep=lambda s: None)
    ans = eng.ask("q", LISBON, {"search": True})
    assert len(calls) == 2 and ans.searched


def test_engine_live_does_not_retry_a_paid_task_error(ai_mode):
    calls = []
    paid_error = json.loads(json.dumps(ai_mode))
    paid_error["tasks"][0].update({"status_code": 50000, "status_message": "Internal Error", "result": None, "cost": 0.004})

    def fake_post(url, json, headers, timeout):
        calls.append(1)
        return _Resp(200, paid_error)

    eng = DataForSEOEngine(login="l", password="p", queue="live", language_code="en", device="desktop", price=PRICE,
                           timeout_s=5, retries=3, post=fake_post, get=None, sleep=lambda s: None)
    with pytest.raises(EngineError) as ei:
        eng.ask("q", LISBON, {"search": True})
    assert len(calls) == 1 and ei.value.cost_usd == pytest.approx(0.004)   # paid → stored as an error row, not re-bought


def test_task_post_is_not_retried_after_a_read_timeout(ai_mode):
    import httpx

    calls = []

    def fake_post(url, json, headers, timeout):
        calls.append(1)
        raise httpx.ReadTimeout("read timed out")      # the body may have been delivered → the task may exist and be billed

    eng = DataForSEOEngine(login="l", password="p", queue="standard", language_code="en", device="desktop", price=PRICE,
                           timeout_s=5, retries=3, post=fake_post, get=None, sleep=lambda s: None)
    with pytest.raises(EngineError) as ei:
        eng.ask("q", LISBON, {"search": True})
    assert len(calls) == 1 and not ei.value.retryable

    calls.clear()

    def fake_post_connect(url, json, headers, timeout):
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ConnectError("refused")         # never reached the server → safe to retry
        return _Resp(200, ai_mode)

    eng = DataForSEOEngine(login="l", password="p", queue="live", language_code="en", device="desktop", price=PRICE,
                           timeout_s=5, retries=3, post=fake_post_connect, get=None, sleep=lambda s: None)
    assert eng.ask("q", LISBON, {"search": True}).searched and len(calls) == 3


def test_poll_timeout_and_stop_keep_the_paid_task_cost(ai_mode):
    import threading

    posted = {"status_code": 20000, "cost": 0.0012, "tasks": [{"id": "task-9", "status_code": 20100, "status_message": "Task Created.", "cost": 0.0012}]}
    queued = {"status_code": 20000, "tasks": [{"id": "task-9", "status_code": 40602, "status_message": "Task In Queue.", "result": None}]}
    eng = DataForSEOEngine(login="l", password="p", queue="standard", language_code="en", device="desktop", price=PRICE, timeout_s=5,
                           retries=1, post=lambda url, json, headers, timeout: _Resp(200, posted),
                           get=lambda url, headers, timeout: _Resp(200, queued), sleep=lambda s: None, poll_interval_s=0, poll_timeout_s=0)
    with pytest.raises(EngineError) as ei:
        eng.ask("q", LISBON, {"search": True})
    assert ei.value.cost_usd == pytest.approx(0.0012) and ei.value.retryable and ei.value.raw["task_id"] == "task-9"

    eng.poll_timeout_s = 10
    eng.stop_event = threading.Event()
    eng.stop_event.set()
    with pytest.raises(EngineError) as ei2:
        eng.ask("q", LISBON, {"search": True})
    assert "stop requested" in str(ei2.value) and ei2.value.cost_usd == pytest.approx(0.0012)


def test_engine_standard_queue_posts_then_polls(ai_mode):
    posted = {"id": "task-1", "status_code": 20100, "status_message": "Task Created.", "cost": 0.0012}
    post_resp = {"status_code": 20000, "tasks": [posted], "cost": 0.0012}
    ready = json.loads(json.dumps(ai_mode))
    ready["tasks"][0]["id"] = "task-1"
    ready["tasks"][0]["cost"] = 0.0012
    calls = {"post": [], "get": []}

    def fake_post(url, json, headers, timeout):
        calls["post"].append(url)
        return _Resp(200, post_resp)

    def fake_get(url, headers, timeout):
        calls["get"].append(url)
        if len(calls["get"]) < 2:
            return _Resp(200, {"status_code": 20000, "tasks": [{"id": "task-1", "status_code": 40602, "status_message": "Task In Queue.", "result": None}]})
        return _Resp(200, ready)

    eng = DataForSEOEngine(login="l", password="p", queue="standard", language_code="en", device="desktop", price=PRICE,
                           timeout_s=5, retries=1, post=fake_post, get=fake_get, sleep=lambda s: None, poll_interval_s=0, poll_timeout_s=10)
    ans = eng.ask("q", LISBON, {"search": True})
    assert calls["post"][0].endswith("/v3/serp/google/ai_mode/task_post")
    assert calls["get"] == ["https://api.dataforseo.com/v3/serp/google/ai_mode/task_get/advanced/task-1"] * 2
    assert ans.cost_usd == pytest.approx(0.0012) and ans.searched
