"""S1 — test_freeze_journal: sha256 journal, idempotent freeze, edited file detected, runner refuses unfrozen."""

from __future__ import annotations

import datetime as dt
import re

import pytest

from lab.freeze import FreezeState, check, freeze, read_journal, sha256_file
from lab.config import load_vertical

LINE = re.compile(r"^\d{4}-\d{2}-\d{2} [0-9a-f]{64} \S+$")


def test_freeze_appends_journal_line_and_is_idempotent(config_dir):
    panel = config_dir / "panels" / "testvert.yaml"
    sha, appended = freeze(panel, config_dir, today=dt.date(2026, 9, 30))
    assert appended and sha == sha256_file(panel)
    journal = (config_dir / "panel-hashes.txt").read_text().splitlines()
    assert len(journal) == 1 and LINE.match(journal[0])
    assert journal[0] == f"2026-09-30 {sha} panels/testvert.yaml"
    sha2, appended2 = freeze(panel, config_dir, today=dt.date(2026, 10, 1))
    assert sha2 == sha and not appended2
    assert len((config_dir / "panel-hashes.txt").read_text().splitlines()) == 1


def test_check_states(config_dir):
    panel = config_dir / "panels" / "testvert.yaml"
    state, latest = check(panel, config_dir)
    assert state is FreezeState.NOT_FROZEN and latest is None
    freeze(panel, config_dir)
    state, latest = check(panel, config_dir)
    assert state is FreezeState.OK and latest == sha256_file(panel)
    panel.write_text(panel.read_text() + "\n# edited after freeze\n")
    state, latest = check(panel, config_dir)
    assert state is FreezeState.MODIFIED and latest != sha256_file(panel)


def test_refreeze_after_edit_appends_new_line(config_dir):
    panel = config_dir / "panels" / "testvert.yaml"
    freeze(panel, config_dir, today=dt.date(2026, 9, 30))
    panel.write_text(panel.read_text() + "\n# v2\n")
    _, appended = freeze(panel, config_dir, today=dt.date(2026, 10, 5))
    assert appended
    entries = read_journal(config_dir)
    assert [e.name for e in entries] == ["panels/testvert.yaml", "panels/testvert.yaml"]
    assert entries[-1].date == "2026-10-05"


def test_freeze_external_file_uses_basename(config_dir, tmp_path):
    prereg = tmp_path / "agent-form-prereg.md"
    prereg.write_text("# prereg\n")
    freeze(prereg, config_dir)
    assert read_journal(config_dir)[-1].name == "agent-form-prereg.md"


def test_planner_refuses_unfrozen_panel(config_dir):
    from lab.planner import NotFrozenError, plan_day
    from lab.store import Store

    v = load_vertical(config_dir, "testvert")
    store = Store(":memory:")
    with pytest.raises(NotFrozenError, match="testvert.yaml"):
        plan_day(v, store, dt.date(2026, 10, 1), require_frozen=True)
    freeze(config_dir / "panels" / "testvert.yaml", config_dir)
    freeze(config_dir / "panels" / "testvert.agent.yaml", config_dir)
    assert plan_day(v, store, dt.date(2026, 10, 1), require_frozen=True)


def test_planner_refuses_modified_panel(config_dir):
    from lab.planner import NotFrozenError, plan_day
    from lab.store import Store

    freeze(config_dir / "panels" / "testvert.yaml", config_dir)
    freeze(config_dir / "panels" / "testvert.agent.yaml", config_dir)
    (config_dir / "panels" / "testvert.yaml").write_text((config_dir / "panels" / "testvert.yaml").read_text() + "\n# x\n")
    v = load_vertical(config_dir, "testvert")
    with pytest.raises(NotFrozenError, match="modified"):
        plan_day(v, Store(":memory:"), dt.date(2026, 10, 1), require_frozen=True)
