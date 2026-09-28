"""S1 — test_store_unique_run: one row per key; error rows are replaced by later success; ok rows are never overwritten."""

from __future__ import annotations

from lab.store import RunKey, RunRecord, Store


def _rec(status="ok", **kw):
    base = dict(
        key=RunKey("2026-W40", "P01", "openai", "lisbon", 4),
        ts_utc="2026-10-01T06:00:00Z", status=status, raw_json='{"a":1}', answer_text="hello",
        cost_usd=0.036, error=None, panel_sha="a" * 64, search=1, searched=1, catch_up=0, latency_ms=1200,
        model="gpt-6-sol", citations=[(1, "https://www.example-firm.pt/d7", "example-firm.pt", "D7")],
    )
    base.update(kw)
    return RunRecord(**base)


def test_unique_run_same_key_twice_one_row():
    s = Store(":memory:")
    assert s.save_run(_rec()) == "inserted"
    assert s.save_run(_rec(answer_text="second")) == "skipped"
    rows = list(s.iter_runs("2026-W40"))
    assert len(rows) == 1 and rows[0]["answer_text"] == "hello"
    assert s.ok_keys("2026-W40") == {RunKey("2026-W40", "P01", "openai", "lisbon", 4)}


def test_error_row_is_replaced_by_success_and_never_the_reverse():
    s = Store(":memory:")
    assert s.save_run(_rec(status="error", error="timeout", answer_text=None, raw_json=None, cost_usd=0.0, citations=[])) == "inserted"
    assert s.ok_keys("2026-W40") == set()
    assert s.save_run(_rec()) == "updated"
    rows = list(s.iter_runs("2026-W40"))
    assert len(rows) == 1 and rows[0]["status"] == "ok" and rows[0]["error"] is None
    assert s.save_run(_rec(status="error", error="later failure", citations=[])) == "skipped"
    assert list(s.iter_runs("2026-W40"))[0]["status"] == "ok"


def test_citations_stored_with_run_and_replaced_on_update():
    s = Store(":memory:")
    s.save_run(_rec(status="error", error="x", citations=[]))
    s.save_run(_rec())
    cites = s.citations_for(RunKey("2026-W40", "P01", "openai", "lisbon", 4))
    assert [(c["position"], c["domain"]) for c in cites] == [(1, "example-firm.pt")]


def test_status_summary_counts_and_cost():
    s = Store(":memory:")
    s.save_run(_rec())
    s.save_run(_rec(key=RunKey("2026-W40", "P02", "openai", "lisbon", 4), status="error", error="e", cost_usd=0.0, citations=[], answer_text=None))
    summary = s.summary("2026-W40")
    by_status = {(r["engine_id"], r["status"]): r for r in summary}
    assert by_status[("openai", "ok")]["n"] == 1 and by_status[("openai", "error")]["n"] == 1
    assert by_status[("openai", "ok")]["cost_usd"] == 0.036


def test_schema_has_contract_tables():
    s = Store(":memory:")
    tables = {r[0] for r in s.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"panels", "prompts", "engines", "runs", "citations"} <= tables
    idx = s.conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='ux_runs_key'").fetchone()[0]
    assert "iso_week" in idx and "run_idx" in idx
