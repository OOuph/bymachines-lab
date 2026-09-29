"""S1 — test_store_unique_run: one row per key; error rows are replaced by later success; ok rows are never overwritten."""

from __future__ import annotations

import pytest

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


def test_replacing_an_error_row_keeps_the_money_already_paid():
    s = Store(":memory:")
    s.save_run(_rec(status="error", error="incomplete", cost_usd=0.028, citations=[]))   # paid, unusable
    assert s.save_run(_rec(cost_usd=0.045)) == "updated"
    row = list(s.iter_runs("2026-W40"))[0]
    assert row["status"] == "ok" and row["cost_usd"] == pytest.approx(0.073)               # both purchases count for the week
    assert s.week_cost("2026-W40") == pytest.approx(0.073)


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


def test_opening_an_older_database_adds_missing_columns(tmp_path):
    import sqlite3

    db = tmp_path / "old.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT NOT NULL, iso_week TEXT NOT NULL, prompt_id TEXT NOT NULL,
            engine_id TEXT NOT NULL, location TEXT NOT NULL, run_idx INTEGER NOT NULL, status TEXT NOT NULL, raw_json TEXT, answer_text TEXT,
            cost_usd REAL NOT NULL DEFAULT 0, error TEXT, panel_sha TEXT, search INTEGER NOT NULL DEFAULT 1, searched INTEGER NOT NULL DEFAULT 0,
            catch_up INTEGER NOT NULL DEFAULT 0, latency_ms INTEGER, model TEXT);
        INSERT INTO runs (ts_utc, iso_week, prompt_id, engine_id, location, run_idx, status) VALUES ('2026-09-28T15:00:00Z','2026-W40','P01','openai','lisbon',4,'ok');
    """)
    conn.commit()
    conn.close()
    s = Store(db)
    cols = {r[1] for r in s.conn.execute("PRAGMA table_info(runs)")}
    assert "n_search" in cols
    assert s.month_search_queries("openai", "2026-09") == 0
    assert s.save_run(_rec()) == "skipped"      # the old row is intact and still counts as done


def test_schema_has_contract_tables():
    s = Store(":memory:")
    tables = {r[0] for r in s.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"panels", "prompts", "engines", "runs", "citations"} <= tables
    idx = s.conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='ux_runs_key'").fetchone()[0]
    assert "iso_week" in idx and "run_idx" in idx
