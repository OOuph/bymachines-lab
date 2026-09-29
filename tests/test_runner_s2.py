"""S2 — runner: graceful stop, Gemini monthly free-quota surcharge, n_search stored per run."""

from __future__ import annotations

import datetime as dt
import threading

from conftest import ENGINES, deep, write_config
from lab.config import load_vertical
from lab.engines.base import Answer, Citation
from lab.freeze import freeze
from lab.planner import plan_day
from lab.runner import run_plan
from lab.store import Store


class CountingEngine:
    def __init__(self, n_search=2, stop_on_call=None, stop_after=1):
        self.calls = 0
        self.n_search = n_search
        self._stop_on_call = stop_on_call      # not named `stop_event`: that attribute is the runner's hook for polling adapters
        self.stop_after = stop_after

    def ask(self, prompt_text, location, options):
        self.calls += 1
        if self._stop_on_call is not None and self.calls >= self.stop_after:
            self._stop_on_call.set()
        return Answer(text="ok", citations=[Citation(1, "https://example.com", "e", "example.com")], raw={"n": self.calls},
                      cost_usd=0.001, searched=True, n_search=self.n_search, latency_ms=1, model="m", usage={}, sources=[])


def _vertical(tmp_path, gemini_free=3):
    e = deep(ENGINES)
    e["engines"]["gemini"].update({"enabled": True})
    e["engines"]["gemini"]["price"].update({"per_search_query": 0.014, "free_search_queries_per_month": gemini_free})
    cfg = write_config(tmp_path, engines=e)
    freeze(cfg / "panels" / "testvert.yaml", cfg)
    freeze(cfg / "panels" / "testvert.agent.yaml", cfg)
    return load_vertical(cfg, "testvert")


def test_graceful_stop_starts_nothing_new(tmp_path):
    v = _vertical(tmp_path)
    store = Store(":memory:")
    plan = plan_day(v, store, dt.date(2026, 10, 1), engines=["openai"], panel_kinds=["human"], locations=["lisbon"])
    assert len(plan) == 4
    stop = threading.Event()
    eng = CountingEngine(stop_on_call=stop, stop_after=1)
    stats = run_plan(plan, {"openai": eng}, store, concurrency=1, stop_event=stop)
    assert stats.stopped_early and eng.calls == 1 and stats.inserted == 1
    assert len(plan_day(v, store, dt.date(2026, 10, 1), engines=["openai"], panel_kinds=["human"], locations=["lisbon"])) == 3


def test_gemini_search_surcharge_beyond_monthly_free_quota(tmp_path):
    v = _vertical(tmp_path, gemini_free=3)
    store = Store(":memory:")
    plan = plan_day(v, store, dt.date(2026, 10, 1), engines=["gemini"], panel_kinds=["human"])   # 4 prompts × n/a
    eng = CountingEngine(n_search=2)
    stats = run_plan(plan, {"gemini": eng}, store, concurrency=1, specs=v.engines.engines)
    rows = sorted(store.iter_runs("2026-W40"), key=lambda r: r["ts_utc"] + r["prompt_id"])
    costs = sorted(round(r["cost_usd"], 6) for r in rows)
    # cumulative searches 2, 4, 6, 8 against a free quota of 3 → surcharges 0, 1, 2, 2 queries × $0.014 on top of $0.001
    assert costs == sorted([0.001, 0.001 + 0.014, 0.001 + 0.028, 0.001 + 0.028])
    assert all(r["n_search"] == 2 for r in rows)
    assert stats.cost_usd == round(sum(costs), 6)
    month = rows[0]["ts_utc"][:7]                       # the quota month is the wall-clock month of the call, not the planned day
    assert store.month_search_queries("gemini", month) == 8


def test_no_surcharge_for_engines_without_quota(tmp_path):
    v = _vertical(tmp_path)
    store = Store(":memory:")
    plan = plan_day(v, store, dt.date(2026, 10, 1), engines=["openai"], panel_kinds=["human"], locations=["lisbon"])
    run_plan(plan, {"openai": CountingEngine(n_search=3)}, store, concurrency=2, specs=v.engines.engines)
    assert {round(r["cost_usd"], 6) for r in store.iter_runs("2026-W40")} == {0.001}
