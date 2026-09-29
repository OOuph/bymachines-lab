"""S2 — planner: location fallback for engines without a location, catch-up window, budget guard with the fixed cut order."""

from __future__ import annotations

import datetime as dt

import pytest
import yaml

from conftest import AGENT, ENGINES, deep, write_config
from lab.config import load_vertical
from lab.freeze import freeze
from lab.planner import BudgetExceeded, apply_budget_guard, estimate_cost, plan_day
from lab.store import RunKey, RunRecord, Store


def _engines_with_gemini():
    e = deep(ENGINES)
    e["engines"]["gemini"]["enabled"] = True
    e["engines"]["gemini"]["price"]["per_search_query"] = 0.014
    e["engines"]["gemini"]["price"]["free_search_queries_per_month"] = 5000
    return e


def _vertical(tmp_path, *, engines=None, with_twist=False, catch_up_days=1, cap=50, reduced=("lisbon",)):
    engines = engines or deep(ENGINES)
    engines["catch_up_days"] = catch_up_days
    engines["weekly_budget_usd"] = cap
    engines["provider_locations_reduced"] = list(reduced)
    agent = deep(AGENT)
    agent["start_date"] = "2026-10-08"
    cfg = write_config(tmp_path, engines=engines, agent=agent)
    if with_twist:
        twist = deep(AGENT)
        twist.update({"panel": "twist", "iso_week": "2026-W41", "start_date": "2026-10-08", "engines": ["openai"],
                      "engine_options": {"openai": {"search": False}}, "classes": {"twist": {"locations": ["lisbon"], "publish": False}}})
        twist["prompts"] = [{"id": "T41-01", "class": "twist", "twin_of": "A01", "country": "Portugal", "need": "immigration lawyer for a D7 visa"}]
        (cfg / "panels" / "testvert.twist.yaml").write_text(yaml.safe_dump(twist, sort_keys=False))
    for f in ("testvert.yaml", "testvert.agent.yaml", "testvert.twist.yaml"):
        if (cfg / "panels" / f).exists():
            freeze(cfg / "panels" / f, cfg)
    return load_vertical(cfg, "testvert")


def _ok(key: RunKey, cost=0.04) -> RunRecord:
    return RunRecord(key=key, ts_utc="2026-10-06T06:00:00Z", status="ok", raw_json="{}", answer_text="a", cost_usd=cost, error=None,
                     panel_sha="x" * 64, search=1, searched=1, catch_up=0, latency_ms=1, model="gpt-6-sol")


def test_engine_without_location_gets_single_na_cell(tmp_path):
    v = _vertical(tmp_path, engines=_engines_with_gemini(), catch_up_days=0)
    plan = plan_day(v, Store(":memory:"), dt.date(2026, 10, 1), panel_kinds=["human"])
    gem = [p for p in plan if p.key.engine_id == "gemini"]
    assert {p.key.location for p in gem} == {"n/a"} and all(p.location is None for p in gem)
    assert sum(1 for p in gem if p.key.prompt_id == "P01") == 1              # once per prompt, not per class location
    assert sum(1 for p in plan if p.key.engine_id == "openai" and p.key.prompt_id == "P01") == 2


def test_catch_up_window_plans_yesterday_with_flag(tmp_path):
    v = _vertical(tmp_path, catch_up_days=1)
    store = Store(":memory:")
    wed = dt.date(2026, 10, 7)                                                 # W41 weekday 3 (agent not due before 08.10)
    plan = plan_day(v, store, wed, panel_kinds=["human"], locations=["lisbon"])
    by_idx = {}
    for p in plan:
        by_idx.setdefault(p.key.run_idx, []).append(p)
    assert set(by_idx) == {2, 3}                                                # Tue (catch-up) + Wed; Mon outside the window
    assert all(p.options["catch_up"] == 1 for p in by_idx[2]) and all(p.options["catch_up"] == 0 for p in by_idx[3])


def test_catch_up_skips_done_and_replans_errors(tmp_path):
    v = _vertical(tmp_path, catch_up_days=1)
    store = Store(":memory:")
    for pid in ("P01", "P02", "Q01", "B01"):
        store.save_run(_ok(RunKey("2026-W41", pid, "openai", "lisbon", 2)))    # Tuesday fully done
    err = _ok(RunKey("2026-W41", "P01", "openai", "lisbon", 2))
    plan = plan_day(v, store, dt.date(2026, 10, 7), panel_kinds=["human"], locations=["lisbon"])
    assert {p.key.run_idx for p in plan} == {3}
    store2 = Store(":memory:")
    for pid in ("P02", "Q01", "B01"):
        store2.save_run(_ok(RunKey("2026-W41", pid, "openai", "lisbon", 2)))   # Tuesday done except P01, which errored
    bad = RunRecord(**{**err.__dict__, "status": "error", "error": "boom", "raw_json": None, "answer_text": None, "cost_usd": 0.0})
    store2.save_run(bad)
    plan2 = plan_day(v, store2, dt.date(2026, 10, 7), panel_kinds=["human"], locations=["lisbon"])
    tue = [p for p in plan2 if p.key.run_idx == 2]
    assert [p.key.prompt_id for p in tue] == ["P01"] and tue[0].options["catch_up"] == 1


def test_catch_up_zero_and_week_start(tmp_path):
    v0 = _vertical(tmp_path / "a", catch_up_days=0)
    plan = plan_day(v0, Store(":memory:"), dt.date(2026, 10, 7), panel_kinds=["human"], locations=["lisbon"])
    assert {p.key.run_idx for p in plan} == {3}
    v6 = _vertical(tmp_path / "b", catch_up_days=6)
    plan = plan_day(v6, Store(":memory:"), dt.date(2026, 10, 5), panel_kinds=["human"], locations=["lisbon"])   # Monday
    assert {p.key.run_idx for p in plan} == {1}                                # never reaches into the previous ISO week


def test_explicit_run_idx_disables_catch_up(tmp_path):
    v = _vertical(tmp_path, catch_up_days=6)
    plan = plan_day(v, Store(":memory:"), dt.date(2026, 10, 7), run_idx_list=[5], panel_kinds=["human"], locations=["lisbon"])
    assert {p.key.run_idx for p in plan} == {5} and all(p.options["catch_up"] == 0 for p in plan)


def test_catch_up_never_reaches_before_start_date(tmp_path):
    """First live day Thu 2026-10-01 with a 1-day window: Wed 30.09 is before the human panel's start → only run_idx 4."""
    from conftest import HUMAN

    e = deep(ENGINES)
    e["catch_up_days"] = 1
    human = deep(HUMAN)
    human["start_date"] = "2026-10-01"
    cfg = write_config(tmp_path, engines=e, human=human)
    freeze(cfg / "panels" / "testvert.yaml", cfg)
    freeze(cfg / "panels" / "testvert.agent.yaml", cfg)
    v = load_vertical(cfg, "testvert")
    plan = plan_day(v, Store(":memory:"), dt.date(2026, 10, 1), panel_kinds=["human"], locations=["lisbon"])
    assert {p.key.run_idx for p in plan} == {4}
    plan2 = plan_day(v, Store(":memory:"), dt.date(2026, 10, 2), panel_kinds=["human"], locations=["lisbon"])
    assert {p.key.run_idx for p in plan2} == {4, 5}                       # Friday catches up Thursday, the start day


def _full_plan(tmp_path, cap):
    v = _vertical(tmp_path, with_twist=True, cap=cap, reduced=("lisbon",), catch_up_days=0)
    store = Store(":memory:")
    plan = plan_day(v, store, dt.date(2026, 10, 8))     # Thu W41: human 6 cells + agent 2 + twist 1 (no search)
    return v, store, plan


def test_estimate_matches_hand_computation(tmp_path):
    v, store, plan = _full_plan(tmp_path, cap=50)
    assert len(plan) == 9
    # searched gpt-6-sol cell: 10000×2 + 1000×10 per 1M + 1 search × 0.01 = 0.04 (P01×2 loc, P02×2 loc, B01, A01, A02 = 7 cells);
    # Q01 is class `problem` → gpt-6-luna: 10000×0.10 + 1000×0.5 per 1M + 0.01 = 0.0115; twist without search: 200×2 + 1000×10 per 1M = 0.0104
    assert estimate_cost(plan, v) == pytest.approx(7 * 0.04 + 0.0115 + 0.0104, abs=1e-6)


def test_budget_guard_cuts_in_fixed_order(tmp_path):
    v, store, plan = _full_plan(tmp_path / "a", cap=0.30)          # full plan 0.3019 > 0.30; without the twist 0.2915 fits
    kept, cuts = apply_budget_guard(plan, v, store, "2026-W41")
    assert cuts == ["twist"] and len(kept) == 8 and not any(p.panel_name.endswith("twist.yaml") for p in kept)

    v, store, plan = _full_plan(tmp_path / "b", cap=0.25)
    kept, cuts = apply_budget_guard(plan, v, store, "2026-W41")
    assert cuts == ["twist", "provider_locations_3"] and len(kept) == 6
    assert not any(p.prompt.cls == "provider" and p.key.location == "madrid" for p in kept)

    v, store, plan = _full_plan(tmp_path / "c", cap=0.20)
    kept, cuts = apply_budget_guard(plan, v, store, "2026-W41")
    assert cuts == ["twist", "provider_locations_3", "agent_core"] and len(kept) == 4
    assert not any(p.key.prompt_id.startswith("A") for p in kept)


def test_budget_guard_counts_week_spend_and_raises_when_nothing_left_to_cut(tmp_path):
    v, store, plan = _full_plan(tmp_path / "a", cap=0.50)
    store.save_run(_ok(RunKey("2026-W41", "Q01", "openai", "lisbon", 1), cost=0.30))   # already spent this week
    kept, cuts = apply_budget_guard(plan, v, store, "2026-W41")
    assert cuts and estimate_cost(kept, v) + 0.30 <= 0.50 + 1e-9
    v, store, plan = _full_plan(tmp_path / "b", cap=0.10)
    with pytest.raises(BudgetExceeded) as ei:
        apply_budget_guard(plan, v, store, "2026-W41")
    assert ei.value.cap == 0.10 and ei.value.projected > 0.10 and ei.value.cuts == ["twist", "provider_locations_3", "agent_core"]


def test_per_engine_estimate_overrides_global(tmp_path):
    e = _engines_with_gemini()
    e["engines"]["gemini"]["estimate_tokens"] = {"input": 500, "output": 2200}
    v = _vertical(tmp_path, engines=e, catch_up_days=0)
    plan = plan_day(v, Store(":memory:"), dt.date(2026, 10, 1), engines=["gemini"], panel_kinds=["human"])
    assert len(plan) == 4
    assert estimate_cost(plan, v) == pytest.approx(4 * (500 * 0.75 + 2200 * 3.75) / 1e6, abs=1e-6)   # not the global 10000/1000


def test_budget_guard_empty_plan_never_raises(tmp_path):
    v = _vertical(tmp_path, cap=0.01, catch_up_days=0)
    store = Store(":memory:")
    store.save_run(_ok(RunKey("2026-W41", "Q01", "openai", "lisbon", 1), cost=5.0))     # already far over the cap
    assert apply_budget_guard([], v, store, "2026-W41") == ([], [])


def test_budget_guard_reports_only_effective_cuts(tmp_path):
    v = _vertical(tmp_path, cap=0.10, reduced=("lisbon",), catch_up_days=0)
    plan = plan_day(v, Store(":memory:"), dt.date(2026, 10, 1), panel_kinds=["human"])   # no twist, no agent cells
    with pytest.raises(BudgetExceeded) as ei:
        apply_budget_guard(plan, v, Store(":memory:"), "2026-W40")
    assert ei.value.cuts == ["provider_locations_3"]                                     # twist / agent cuts removed nothing → not listed


def test_estimate_projects_gemini_surcharge_beyond_free_quota(tmp_path):
    e = _engines_with_gemini()
    e["engines"]["gemini"]["estimate_tokens"] = {"input": 500, "output": 2200}
    e["engines"]["gemini"]["estimate_search_calls"] = 2
    e["engines"]["gemini"]["price"]["free_search_queries_per_month"] = 3
    v = _vertical(tmp_path, engines=e, catch_up_days=0)
    store = Store(":memory:")
    plan = plan_day(v, store, dt.date(2026, 10, 1), engines=["gemini"], panel_kinds=["human"])   # 4 cells × 2 queries = 8
    tokens = 4 * (500 * 0.75 + 2200 * 3.75) / 1e6
    assert estimate_cost(plan, v) == pytest.approx(tokens, abs=1e-6)                               # without a store: no surcharge
    assert estimate_cost(plan, v, store, "2026-10") == pytest.approx(tokens + 5 * 0.014, abs=1e-6)  # 8 projected − 3 free = 5 billable


def test_budget_guard_noop_under_cap(tmp_path):
    v, store, plan = _full_plan(tmp_path, cap=50)
    kept, cuts = apply_budget_guard(plan, v, store, "2026-W41")
    assert cuts == [] and kept == plan
