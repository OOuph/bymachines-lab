"""DataForSEO SERP API — Google AI Mode (`/v3/serp/google/ai_mode/…`).

Live queue: one POST, the answer comes back in seconds ($4 per 1 000). Standard queue: `task_post` then poll
`task_get/advanced/{id}` (≈5 min, $1.20 per 1 000). The answer is `items[].markdown` of the `ai_overview` item; sources
are the `references[]` of its sub-elements, aggregated in order and de-duplicated by URL. The task's own `cost` field is
authoritative. Location = the city's `location_code` (config/locations.yaml). docs/api-check-2026-09-28.md.

Billing discipline: `task_post` and `live` are non-idempotent purchases → transport errors after delivery are never
retried (base.http_with_retries); a paid task that times out in the queue keeps its cost in the error row.
"""

from __future__ import annotations

import base64
import threading
import time
from typing import Any, Callable

import httpx

from lab.config import EngineSpec, Location
from lab.engines.base import Answer, Citation, EngineError, domain_of, http_with_retries

BASE = "https://api.dataforseo.com"
LIVE_PATH = "/v3/serp/google/ai_mode/live/advanced"
TASK_POST_PATH = "/v3/serp/google/ai_mode/task_post"
TASK_GET_PATH = "/v3/serp/google/ai_mode/task_get/advanced/{id}"
MODEL_LABEL = "google-ai-mode"
IN_QUEUE_CODES = {40601, 40602}           # "Task Handed" / "Task In Queue" — not ready yet
RETRYABLE_API_CODES = {40209, 50000, 50100, 50200, 50401}   # internal / temporary errors per DataForSEO docs


def build_task(prompt_text: str, location: Location | None, *, language_code: str, device: str) -> dict[str, Any]:
    if location is None or location.dataforseo_location_code is None:
        raise EngineError("dataforseo: a location with dataforseo_location_code is required", retryable=False)
    return {"keyword": prompt_text, "location_code": int(location.dataforseo_location_code), "language_code": language_code, "device": device}


def parse_response(data: dict[str, Any], *, price: dict[str, Any], queue: str) -> Answer:
    code = int(data.get("status_code") or 0)
    if code != 20000:
        raise EngineError(f"dataforseo: API status {code}: {data.get('status_message')}", retryable=code in RETRYABLE_API_CODES, raw=data)
    tasks = data.get("tasks") or []
    if not tasks:
        # seen once live on 2026-09-28 (Madrid): HTTP 200, status 20000, cost 0, empty tasks — transient, nothing was charged
        raise EngineError("dataforseo: response without tasks", retryable=True, raw=data)
    task = tasks[0]
    tcode = int(task.get("status_code") or 0)
    cost = float(task.get("cost") or data.get("cost") or (price.get("per_request") or {}).get(queue, 0.0) or 0.0)
    if tcode != 20000:
        raise EngineError(f"dataforseo: task status {tcode}: {task.get('status_message')}", retryable=tcode in RETRYABLE_API_CODES,
                          raw=data, cost_usd=cost)
    results = task.get("result") or []
    items = (results[0].get("items") or []) if results and isinstance(results[0], dict) else []
    texts: list[str] = []
    citations: list[Citation] = []
    seen: set[str] = set()
    for item in items:
        if item.get("type") != "ai_overview":
            continue
        texts.append(str(item.get("markdown") or ""))
        for sub in item.get("items") or []:
            for ref in (sub or {}).get("references") or []:
                url = str((ref or {}).get("url") or "")
                if not url or url in seen:
                    continue
                seen.add(url)
                domain = domain_of(url) or str(ref.get("domain") or "").lower().removeprefix("www.")
                citations.append(Citation(position=len(citations) + 1, url=url, title=str(ref.get("title") or ""), domain=domain))
    if items and not texts:
        # a paid answer in an unknown shape must surface as an error (kept with raw + cost), never as a silent empty "ok"
        types = sorted({str(i.get("type")) for i in items})
        raise EngineError(f"dataforseo: no ai_overview item in the answer (item types {types}) — parser needs an update",
                          retryable=False, raw=data, cost_usd=cost)
    searched = bool(texts)
    return Answer(text="\n\n".join(t for t in texts if t).strip(), citations=citations, raw=data, cost_usd=cost, searched=searched,
                  n_search=1 if searched else 0, latency_ms=0, model=MODEL_LABEL, usage={"cost": cost}, sources=[c.url for c in citations])


class DataForSEOEngine:
    id = "dataforseo"

    def __init__(self, *, login: str, password: str, queue: str, language_code: str, device: str, price: dict[str, Any],
                 timeout_s: float, retries: int, post: Callable[..., Any] | None = None, get: Callable[..., Any] | None = None,
                 sleep: Callable[[float], None] = time.sleep, poll_interval_s: float = 20.0, poll_timeout_s: float = 2700.0):
        self.auth = base64.b64encode(f"{login}:{password}".encode()).decode()
        self.queue = queue
        self.language_code = language_code
        self.device = device
        self.price = price
        self.timeout_s = timeout_s
        self.retries = retries
        self._post = post or httpx.post
        self._get = get or httpx.get
        self._sleep = sleep
        self.poll_interval_s = poll_interval_s
        self.poll_timeout_s = poll_timeout_s
        self.stop_event: threading.Event | None = None   # set by the runner; a graceful stop ends polling early

    @classmethod
    def from_spec(cls, spec: EngineSpec, tunables: dict[str, Any], login: str, password: str) -> "DataForSEOEngine":
        o = spec.options
        return cls(login=login, password=password, queue=str(o.get("queue", "standard")), language_code=str(o.get("language_code", "en")),
                   device=str(o.get("device", "desktop")), price=spec.price, timeout_s=float(o.get("timeout_s", 120)),
                   retries=int(tunables.get("retries", 3)), poll_interval_s=float(o.get("poll_interval_s", 20)),
                   poll_timeout_s=float(o.get("poll_timeout_s", 2700)))

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Basic {self.auth}", "Content-Type": "application/json"}

    def ask(self, prompt_text: str, location: Location | None, options: dict[str, Any]) -> Answer:
        task = build_task(prompt_text, location, language_code=self.language_code, device=self.device)
        t0 = time.monotonic()
        if self.queue == "live":
            for attempt in range(self.retries + 1):
                resp = http_with_retries(lambda: self._post(BASE + LIVE_PATH, json=[task], headers=self._headers(), timeout=self.timeout_s),
                                         retries=self.retries, sleep=self._sleep, what="dataforseo live")
                try:
                    answer = self._parse(self._json(resp), queue="live")
                    break
                except EngineError as exc:
                    # a transient empty/failed live answer costs nothing (cost 0 in the body) → one more POST is safe
                    if not exc.retryable or attempt >= self.retries or float(exc.cost_usd or 0.0) > 0:
                        raise
                    self._sleep(min(30.0, 2.0 * (attempt + 1)))
        else:
            task["priority"] = 2 if self.queue == "priority" else 1
            resp = http_with_retries(lambda: self._post(BASE + TASK_POST_PATH, json=[task], headers=self._headers(), timeout=self.timeout_s),
                                     retries=self.retries, sleep=self._sleep, what="dataforseo task_post")
            posted = self._json(resp)
            if int(posted.get("status_code") or 0) != 20000 or not posted.get("tasks"):
                raise EngineError(f"dataforseo: task_post status {posted.get('status_code')}: {posted.get('status_message')}", retryable=False, raw=posted)
            t = posted["tasks"][0]
            if int(t.get("status_code") or 0) not in (20000, 20100):
                raise EngineError(f"dataforseo: task not created: {t.get('status_code')} {t.get('status_message')}", retryable=False, raw=posted)
            paid = float(t.get("cost") or posted.get("cost") or 0.0)
            answer = self._poll(str(t["id"]), paid=paid)
        answer.latency_ms = int((time.monotonic() - t0) * 1000)
        return answer

    def _parse(self, payload: dict[str, Any], *, queue: str) -> Answer:
        try:
            return parse_response(payload, price=self.price, queue=queue)
        except EngineError:
            raise
        except Exception as exc:  # noqa: BLE001 — a parser bug must not lose a paid answer
            raise EngineError(f"dataforseo: parse failure {type(exc).__name__}: {exc}", retryable=False, raw=payload) from exc

    def _poll(self, task_id: str, *, paid: float) -> Answer:
        url = BASE + TASK_GET_PATH.format(id=task_id)
        deadline = time.monotonic() + self.poll_timeout_s
        while True:
            resp = http_with_retries(lambda: self._get(url, headers=self._headers(), timeout=self.timeout_s),
                                     retries=self.retries, sleep=self._sleep, what="dataforseo task_get")
            data = self._json(resp)
            tasks = data.get("tasks") or []
            tcode = int(tasks[0].get("status_code") or 0) if tasks else 0
            if tcode in IN_QUEUE_CODES or (tcode == 0 and int(data.get("status_code") or 0) == 20000):
                if self.stop_event is not None and self.stop_event.is_set():
                    raise EngineError(f"dataforseo: stop requested while task {task_id} was in the queue (paid ${paid:.4f}, task id kept in raw)",
                                      retryable=True, raw={"task_id": task_id, "last": data}, cost_usd=paid)
                if time.monotonic() > deadline:
                    raise EngineError(f"dataforseo: task {task_id} not ready after {self.poll_timeout_s:.0f}s (paid ${paid:.4f})",
                                      retryable=True, raw={"task_id": task_id, "last": data}, cost_usd=paid)
                self._sleep(self.poll_interval_s)
                continue
            answer = self._parse(data, queue=self.queue)
            if paid and not answer.cost_usd:
                answer.cost_usd = paid
            return answer

    @staticmethod
    def _json(resp: Any) -> dict[str, Any]:
        try:
            return resp.json()
        except ValueError as exc:
            raise EngineError(f"dataforseo: HTTP 200 with unparsable body: {exc}") from exc
