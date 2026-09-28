"""S1 — test_runner_idempotent_same_day / test_runner_resume: planning excludes done rows; the runner stores every result."""

from __future__ import annotations

import datetime as dt

import pytest

from conftest import write_config
from lab.config import load_vertical
from lab.engines.base import Answer, Citation, EngineError
from lab.freeze import freeze
from lab.planner import iso_week_of, plan_day
from lab.runner import run_plan
from lab.store import RunKey, Store


class FakeEngine:
    def __init__(self, fail_for=()):
        self.calls = []
        self.fail_for = set(fail_for)

    def ask(self, prompt_text, location, options):
        self.calls.append((prompt_text, location.key if location else None, dict(options)))
        if prompt_text in self.fail_for:
            raise EngineError("simulated failure", retryable=False)
        return Answer(text=f"answer to {prompt_text}", citations=[Citation(1, "https://www.example.com/a", "A", "example.com")],
                      raw={"echo": prompt_text}, cost_usd=0.01, searched=bool(options.get("search", True)), n_search=1,
                      latency_ms=5, model=options.get("model", "fake"), usage={}, sources=[])


def _frozen_vertical(config_dir):
    freeze(config_dir / "panels" / "testvert.yaml", config_dir)
    freeze(config_dir / "panels" / "testvert.agent.yaml", config_dir)
    return load_vertical(config_dir, "testvert")


def test_iso_week_format():
    assert iso_week_of(dt.date(2026, 10, 1)) == "2026-W40"
    assert iso_week_of(dt.date(2026, 12, 31)) == "2026-W53"
    assert iso_week_of(dt.date(2027, 1, 3)) == "2026-W53"
    assert iso_week_of(dt.date(2027, 1, 4)) == "2027-W01"


def test_panel_before_start_date_is_skipped_and_not_freeze_checked(tmp_path):
    from conftest import AGENT, deep
    from lab.planner import NotFrozenError

    agent = deep(AGENT)
    agent["start_date"] = "2026-10-08"            # agent form starts Thu 08.10 (SPEC §13)
    cfg = write_config(tmp_path, agent=agent)
    freeze(cfg / "panels" / "testvert.yaml", cfg)  # only the human panel is frozen
    v = load_vertical(cfg, "testvert")
    plan = plan_day(v, Store(":memory:"), dt.date(2026, 10, 5), require_frozen=True)   # Mon of W41, before the start
    assert {p.key.prompt_id for p in plan} == {"P01", "P02", "Q01", "B01"}
    with pytest.raises(NotFrozenError, match="agent"):
        plan_day(v, Store(":memory:"), dt.date(2026, 10, 8), require_frozen=True)       # start day → agent must be frozen
    freeze(cfg / "panels" / "testvert.agent.yaml", cfg)
    v = load_vertical(cfg, "testvert")
    plan = plan_day(v, Store(":memory:"), dt.date(2026, 10, 8), require_frozen=True)
    assert {p.key.prompt_id for p in plan} >= {"A01", "A02"} and {p.key.run_idx for p in plan} == {4}


def test_ignore_schedule_plans_draft_panels_before_start(tmp_path):
    from conftest import AGENT, deep

    agent = deep(AGENT)
    agent["start_date"] = "2026-10-08"
    cfg = write_config(tmp_path, agent=agent)
    v = load_vertical(cfg, "testvert")
    assert plan_day(v, Store(":memory:"), dt.date(2026, 9, 28), panel_kinds=["agent"]) == []
    smoke = plan_day(v, Store(":memory:"), dt.date(2026, 9, 28), panel_kinds=["agent"], run_idx_list=[1, 2], ignore_schedule=True)
    assert {p.key.prompt_id for p in smoke} == {"A01", "A02"} and {p.key.run_idx for p in smoke} == {1, 2}


def test_dry_plan_does_not_persist(config_dir):
    v = _frozen_vertical(config_dir)
    store = Store(":memory:")
    plan_day(v, store, dt.date(2026, 10, 1), persist=False)
    assert store.conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0] == 0
    assert store.conn.execute("SELECT COUNT(*) FROM panels").fetchone()[0] == 0


def test_unknown_filter_ids_rejected(config_dir):
    from lab.planner import PlanError

    v = _frozen_vertical(config_dir)
    with pytest.raises(PlanError, match="openia"):
        plan_day(v, Store(":memory:"), dt.date(2026, 10, 1), engines=["openia"])
    with pytest.raises(PlanError, match="lisboa"):
        plan_day(v, Store(":memory:"), dt.date(2026, 10, 1), locations=["lisboa"])


def test_runner_keeps_raw_and_cost_of_incomplete_answer(config_dir):
    v = _frozen_vertical(config_dir)
    store = Store(":memory:")

    class IncompleteEngine:
        id = "openai"

        def ask(self, prompt_text, location, options):
            raise EngineError("response status 'incomplete'", raw={"status": "incomplete", "usage": {"input_tokens": 9000}}, cost_usd=0.028)

    plan = plan_day(v, store, dt.date(2026, 10, 1), limit=1, panel_kinds=["human"], locations=["lisbon"])
    stats = run_plan(plan, {"openai": IncompleteEngine()}, store, concurrency=1)
    row = list(store.iter_runs("2026-W40"))[0]
    assert stats.errors == 1 and row["status"] == "error" and row["raw_json"] is not None and row["cost_usd"] == 0.028


def test_runner_survives_store_failure_and_keeps_going(config_dir, monkeypatch):
    v = _frozen_vertical(config_dir)
    store = Store(":memory:")
    plan = plan_day(v, store, dt.date(2026, 10, 1))
    real_save = store.save_run
    state = {"n": 0}

    def flaky(rec):
        state["n"] += 1
        if state["n"] == 2:
            raise RuntimeError("database is locked")
        return real_save(rec)

    monkeypatch.setattr(store, "save_run", flaky)
    stats = run_plan(plan, {"openai": FakeEngine()}, store, concurrency=2)
    assert stats.store_failures == 1 and stats.inserted == 7
    assert len(list(store.iter_runs("2026-W40"))) == 7


def test_plan_covers_classes_locations_and_weekday(config_dir):
    v = _frozen_vertical(config_dir)
    plan = plan_day(v, Store(":memory:"), dt.date(2026, 10, 1))  # Thursday → run_idx 4
    keys = {(p.key.prompt_id, p.key.location, p.key.run_idx) for p in plan}
    # human: P01/P02 × lisbon,madrid ; Q01, B01 × lisbon ; agent: A01/A02 × lisbon — openai only (gemini disabled)
    assert keys == {
        ("P01", "lisbon", 4), ("P01", "madrid", 4), ("P02", "lisbon", 4), ("P02", "madrid", 4),
        ("Q01", "lisbon", 4), ("B01", "lisbon", 4), ("A01", "lisbon", 4), ("A02", "lisbon", 4),
    }
    assert {p.key.engine_id for p in plan} == {"openai"}
    assert all(p.key.iso_week == "2026-W40" for p in plan)
    q01 = next(p for p in plan if p.key.prompt_id == "Q01")
    assert q01.options["model"] == "gpt-6-luna" and q01.options["search"] is True
    assert q01.panel_sha == v.panels[0].sha256


def test_runner_idempotent_same_day(config_dir):
    v = _frozen_vertical(config_dir)
    store = Store(":memory:")
    eng = FakeEngine()
    plan = plan_day(v, store, dt.date(2026, 10, 1))
    stats = run_plan(plan, {"openai": eng}, store, concurrency=2)
    assert stats.inserted == 8 and stats.errors == 0 and len(eng.calls) == 8
    assert stats.cost_usd == 0.08
    plan2 = plan_day(v, store, dt.date(2026, 10, 1))
    assert plan2 == []
    stats2 = run_plan(plan2, {"openai": eng}, store, concurrency=2)
    assert stats2.inserted == 0 and len(eng.calls) == 8
    assert len(list(store.iter_runs("2026-W40"))) == 8


def test_runner_resume_only_missing_rows(config_dir):
    v = _frozen_vertical(config_dir)
    store = Store(":memory:")
    full = plan_day(v, store, dt.date(2026, 10, 1))
    eng1 = FakeEngine()
    run_plan(full[:3], {"openai": eng1}, store, concurrency=1)          # "crash" after 3 rows
    remaining = plan_day(v, store, dt.date(2026, 10, 1))
    assert len(remaining) == 5 and {p.key for p in remaining}.isdisjoint({p.key for p in full[:3]})
    eng2 = FakeEngine()
    run_plan(remaining, {"openai": eng2}, store, concurrency=3)
    assert len(eng2.calls) == 5 and len(list(store.iter_runs("2026-W40"))) == 8


def test_runner_stores_error_rows_and_replans_them(config_dir):
    v = _frozen_vertical(config_dir)
    store = Store(":memory:")
    bad_text = next(p.text for p in v.panels[0].prompts if p.id == "Q01")
    eng = FakeEngine(fail_for={bad_text})
    plan = plan_day(v, store, dt.date(2026, 10, 1))
    stats = run_plan(plan, {"openai": eng}, store, concurrency=2)
    assert stats.inserted == 7 and stats.errors == 1
    rows = {r["prompt_id"]: r for r in store.iter_runs("2026-W40")}
    assert rows["Q01"]["status"] == "error" and "simulated failure" in rows["Q01"]["error"]
    replan = plan_day(v, store, dt.date(2026, 10, 1))
    assert [p.key.prompt_id for p in replan] == ["Q01"]
    stats2 = run_plan(replan, {"openai": FakeEngine()}, store, concurrency=1)
    assert stats2.updated == 1 and store.ok_keys("2026-W40") == {p.key for p in plan}


def test_plan_explicit_run_indices_and_limit(config_dir):
    v = _frozen_vertical(config_dir)
    plan = plan_day(v, Store(":memory:"), dt.date(2026, 9, 29), run_idx_list=[1, 2], limit=3, panel_kinds=["human"], locations=["lisbon"])
    # first 3 prompts of the human panel × 1 location × 2 runs — the S1 smoke shape
    assert [p.key.prompt_id for p in plan] == ["P01", "P01", "P02", "P02", "Q01", "Q01"]
    assert {p.key.run_idx for p in plan} == {1, 2}
    assert {p.key.location for p in plan} == {"lisbon"}


def test_plan_search_override_flag(config_dir):
    v = _frozen_vertical(config_dir)
    plan = plan_day(v, Store(":memory:"), dt.date(2026, 10, 1), search_override=False, panel_kinds=["human"])
    assert plan and all(p.options["search"] is False for p in plan)


def test_plan_search_override_and_twist_engine_filter(tmp_path):
    from conftest import AGENT, deep

    twist = deep(AGENT)
    twist.update({"panel": "twist", "iso_week": "2026-W41", "engines": ["openai"], "engine_options": {"openai": {"search": False}}})
    twist["classes"] = {"twist": {"locations": ["lisbon"], "publish": False}}
    twist["prompts"] = [{"id": "T41-01", "class": "twist", "twin_of": "A01", "country": "Portugal", "need": "immigration lawyer for a D7 visa"}]
    cfg = write_config(tmp_path)
    (cfg / "panels" / "testvert.twist.yaml").write_text(__import__("yaml").safe_dump(twist, sort_keys=False))
    for f in ("testvert.yaml", "testvert.agent.yaml", "testvert.twist.yaml"):
        freeze(cfg / "panels" / f, cfg)
    v = load_vertical(cfg, "testvert")
    plan = plan_day(v, Store(":memory:"), dt.date(2026, 10, 8), panel_kinds=["twist"])
    assert [p.key.prompt_id for p in plan] == ["T41-01"] and plan[0].options["search"] is False
    store = Store(":memory:")
    eng = FakeEngine()
    run_plan(plan, {"openai": eng}, store, concurrency=1)
    row = list(store.iter_runs("2026-W41"))[0]
    assert row["search"] == 0 and row["searched"] == 0 and eng.calls[0][2]["search"] is False
