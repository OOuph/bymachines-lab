"""CLI guards that protect the production database and the timer (review S2 MAJOR 2 and 3)."""

from __future__ import annotations

import datetime as dt

import pytest

from conftest import ENGINES, deep, write_config
from lab.cli import EXIT_BUDGET, EXIT_USAGE, main
from lab.freeze import freeze


def _cfg(tmp_path, cap=50):
    e = deep(ENGINES)
    e["weekly_budget_usd"] = cap
    cfg = write_config(tmp_path, engines=e)
    freeze(cfg / "panels" / "testvert.yaml", cfg)
    freeze(cfg / "panels" / "testvert.agent.yaml", cfg)
    return cfg


@pytest.fixture
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setenv("LAB_DATA_DIR", str(data))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-never-used")      # build_engine needs a key; the guard stops before any call
    monkeypatch.chdir(tmp_path)
    return data


def test_date_outside_current_week_is_refused_on_production_db(tmp_path, env):
    cfg = _cfg(tmp_path)
    far = (dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=60)).isoformat()
    rc = main(["--config-dir", str(cfg), "--vertical", "testvert", "run", "--date", far])
    assert rc == EXIT_USAGE
    assert not (env / "lab.sqlite").exists()


def test_smoke_db_overrun_writes_no_alert_and_does_not_block_production(tmp_path, env):
    cfg = _cfg(tmp_path, cap=0.0001)
    rc = main(["--config-dir", str(cfg), "--vertical", "testvert", "run", "--db", str(tmp_path / "smoke.sqlite"), "--allow-unfrozen",
               "--limit", "1", "--runs", "1", "--engine", "openai"])
    assert rc == EXIT_BUDGET
    assert not (env / "ALERT").exists() and not (env / "journal.md").exists()


def test_production_overrun_writes_alert_and_next_run_refuses(tmp_path, env, capsys):
    cfg = _cfg(tmp_path, cap=0.0001)
    rc = main(["--config-dir", str(cfg), "--vertical", "testvert", "run", "--engine", "openai"])
    assert rc == EXIT_BUDGET and (env / "ALERT").exists() and "budget guard" in capsys.readouterr().err
    rc2 = main(["--config-dir", str(cfg), "--vertical", "testvert", "run", "--engine", "openai"])
    assert rc2 == EXIT_BUDGET and "exists (budget stop)" in capsys.readouterr().err     # ALERT present → refused before planning
    alert_text = (env / "ALERT").read_text()
    (env / "ALERT").unlink()
    rc3 = main(["--config-dir", str(cfg), "--vertical", "testvert", "run", "--engine", "openai", "--dry-run"])
    assert rc3 == EXIT_BUDGET and "budget guard" in capsys.readouterr().err               # still over the cap by config …
    assert not (env / "ALERT").exists() and "cap exceeded" in alert_text                  # … but a dry run writes nothing
