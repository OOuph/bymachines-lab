"""S1 — test_config_validates_panel: loading, template substitution, and every validation rule of SPEC §4/§6."""

from __future__ import annotations

import pytest

from conftest import AGENT, ENGINES, FIRMS, HUMAN, LOCATIONS, deep, write_config
from lab.config import ConfigError, load_vertical


def test_loads_vertical_and_substitutes_template(config_dir):
    v = load_vertical(config_dir, "testvert")
    assert [p.kind for p in v.panels] == ["human", "agent"]
    human, agent = v.panels
    assert len(human.prompts) == 4 and len(agent.prompts) == 2
    a01 = next(p for p in agent.prompts if p.id == "A01")
    assert "immigration lawyer for a D7 visa" in a01.text and "Portugal" in a01.text
    assert a01.twin_of == "P01"
    assert agent.refusal_patterns == ["can'?t recommend", "it depends"]
    assert set(v.locations) == {"lisbon", "madrid", "london"}
    assert v.engines.engines["openai"].enabled and not v.engines.engines["gemini"].enabled
    assert v.engines.tunables["catch_up_days"] == ENGINES["catch_up_days"]   # tunables load verbatim from engines.yaml
    assert human.sha256 and len(human.sha256) == 64
    assert {f.id for f in v.firms} == {"plmj", "bymachines", "joao"}


def test_model_by_class_resolves(config_dir):
    v = load_vertical(config_dir, "testvert")
    oa = v.engines.engines["openai"]
    assert oa.model_for_class("provider") == "gpt-6-sol"
    assert oa.model_for_class("problem") == "gpt-6-luna"


def test_duplicate_alias_rejected(tmp_path):
    firms = deep(FIRMS)
    firms["firms"][1]["aliases"].append("plmj advogados")  # collides case-insensitively with PLMJ's alias
    cfg = write_config(tmp_path, firms=firms)
    with pytest.raises(ConfigError, match="alias"):
        load_vertical(cfg, "testvert")


def test_duplicate_prompt_id_across_panel_files_rejected(tmp_path):
    agent = deep(AGENT)
    agent["prompts"][0]["id"] = "P01"
    cfg = write_config(tmp_path, agent=agent)
    with pytest.raises(ConfigError, match="P01"):
        load_vertical(cfg, "testvert")


def test_missing_class_rejected(tmp_path):
    human = deep(HUMAN)
    human["prompts"][0]["class"] = "compare"  # not declared in classes
    cfg = write_config(tmp_path, human=human)
    with pytest.raises(ConfigError, match="compare"):
        load_vertical(cfg, "testvert")


def test_unknown_location_rejected(tmp_path):
    human = deep(HUMAN)
    human["classes"]["provider"]["locations"].append("new_york")
    cfg = write_config(tmp_path, human=human)
    with pytest.raises(ConfigError, match="new_york"):
        load_vertical(cfg, "testvert")


def test_twin_mismatch_rejected(tmp_path):
    agent = deep(AGENT)
    agent["prompts"][1]["country"] = "Cyprus"  # twin of P02 (Spain)
    cfg = write_config(tmp_path, agent=agent)
    with pytest.raises(ConfigError, match="A02"):
        load_vertical(cfg, "testvert")


def test_unknown_twin_rejected(tmp_path):
    agent = deep(AGENT)
    agent["prompts"][1]["twin_of"] = "P99"
    cfg = write_config(tmp_path, agent=agent)
    with pytest.raises(ConfigError, match="P99"):
        load_vertical(cfg, "testvert")


def test_prompt_without_text_or_template_rejected(tmp_path):
    human = deep(HUMAN)
    del human["prompts"][2]["text"]
    cfg = write_config(tmp_path, human=human)
    with pytest.raises(ConfigError, match="Q01"):
        load_vertical(cfg, "testvert")


def test_unknown_parent_firm_rejected(tmp_path):
    firms = deep(FIRMS)
    firms["firms"][2]["parent"] = "nope"
    cfg = write_config(tmp_path, firms=firms)
    with pytest.raises(ConfigError, match="nope"):
        load_vertical(cfg, "testvert")


def test_missing_dataforseo_code_rejected_only_when_enabled(tmp_path):
    from conftest import ENGINES

    engines = deep(ENGINES)
    engines["engines"]["dataforseo"] = {
        "enabled": True, "name": "AI Mode", "api": "dataforseo_ai_mode", "model": "ai_mode",
        "supports_location": True, "price": {"per_request": {"standard": 0.0012}}, "env": ["DATAFORSEO_LOGIN"],
    }
    locations = deep(LOCATIONS)
    locations["locations"]["madrid"]["dataforseo_location_code"] = None
    cfg = write_config(tmp_path, engines=engines, locations=locations)
    with pytest.raises(ConfigError, match="madrid"):
        load_vertical(cfg, "testvert")
    engines["engines"]["dataforseo"]["enabled"] = False
    cfg2 = write_config(tmp_path / "b", engines=engines, locations=locations)
    load_vertical(cfg2, "testvert")  # disabled engine → no code needed


def test_model_without_price_rejected(tmp_path):
    from conftest import ENGINES

    engines = deep(ENGINES)
    engines["engines"]["openai"]["model_by_class"] = {"problem": "gpt-6-nova"}  # no price row → must fail before any spend
    cfg = write_config(tmp_path, engines=engines)
    with pytest.raises(ConfigError, match="gpt-6-nova"):
        load_vertical(cfg, "testvert")


def test_scalar_where_list_expected_rejected(tmp_path):
    firms = deep(FIRMS)
    firms["firms"][0]["aliases"] = "PLMJ Advogados"  # scalar, would otherwise iterate per character
    cfg = write_config(tmp_path, firms=firms)
    with pytest.raises(ConfigError, match="aliases"):
        load_vertical(cfg, "testvert")
    human = deep(HUMAN)
    human["prompts"].append("just a string")
    cfg2 = write_config(tmp_path / "b", human=human)
    with pytest.raises(ConfigError, match="prompt"):
        load_vertical(cfg2, "testvert")


def test_start_date_and_iso_week_validated(tmp_path):
    agent = deep(AGENT)
    agent["start_date"] = "08.10.2026"
    cfg = write_config(tmp_path, agent=agent)
    with pytest.raises(ConfigError, match="start_date"):
        load_vertical(cfg, "testvert")
    agent = deep(AGENT)
    agent.update({"panel": "twist", "iso_week": "2026-w41", "classes": {"twist": {"locations": ["lisbon"], "publish": False}}})
    for p in agent["prompts"]:
        p["class"] = "twist"
    cfg2 = write_config(tmp_path / "b", agent=agent)
    with pytest.raises(ConfigError, match="iso_week"):
        load_vertical(cfg2, "testvert")


def test_all_errors_reported_together(tmp_path):
    human = deep(HUMAN)
    human["prompts"][0]["class"] = "compare"
    human["classes"]["provider"]["locations"].append("mars")
    cfg = write_config(tmp_path, human=human)
    with pytest.raises(ConfigError) as ei:
        load_vertical(cfg, "testvert")
    assert "compare" in str(ei.value) and "mars" in str(ei.value)
