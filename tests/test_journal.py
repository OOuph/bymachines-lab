"""S2 — operational journal (`data/journal.md`, append-only) and the ALERT file of the budget guard."""

from __future__ import annotations

import re

from lab.journal import append, write_alert, clear_alert


def test_journal_appends_timestamped_lines(tmp_path):
    p = append(tmp_path, "budget cut: twist dropped (projected $52.10 > cap $50)")
    append(tmp_path, "agent experiment paused: two weeks without the agent core")
    lines = p.read_text().splitlines()
    assert p == tmp_path / "journal.md" and len(lines) == 2
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z budget cut: twist dropped", lines[0])


def test_alert_file_written_and_cleared(tmp_path):
    a = write_alert(tmp_path, "weekly cap exceeded", details={"projected": 61.2, "cap": 50})
    assert a == tmp_path / "ALERT" and "weekly cap exceeded" in a.read_text() and "61.2" in a.read_text()
    assert clear_alert(tmp_path) is True and not a.exists()
    assert clear_alert(tmp_path) is False
