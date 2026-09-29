"""Shared helpers: build a small, valid vertical config in a temp dir and mutate it to inject defects."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

FIXTURES = Path(__file__).parent / "fixtures"

ENGINES = {
    "schema_version": 1,
    "runs_per_week": 7,
    "weekly_budget_usd": 50,
    "budget_cut_order": ["twist", "provider_locations_3", "agent_core"],
    "provider_locations_reduced": ["lisbon", "madrid", "london"],
    "concurrency": 2,
    "retries": 3,
    "daily_hour_utc": 6,
    "catch_up_days": 0,   # S1 tests assume "today only"; S2 catch-up tests set the window explicitly
    "default_search_context_size": "medium",
    "estimate_tokens": {"input": 10000, "output": 1000},
    "engines": {
        "openai": {
            "enabled": True,
            "name": "ChatGPT (OpenAI Responses API, web_search)",
            "api": "openai_responses",
            "model": "gpt-6-sol",
            "model_by_class": {"problem": "gpt-6-luna"},
            "reasoning_effort": "none",
            "tool": "web_search",
            "search_context_size": "medium",
            "include_sources": True,
            "supports_location": True,
            "timeout_s": 30,
            "price": {
                "per_1m_input": {"gpt-6-sol": 2.00, "gpt-6-luna": 0.10},
                "per_1m_cached_input": {"gpt-6-sol": 0.20, "gpt-6-luna": 0.01},
                "per_1m_output": {"gpt-6-sol": 10.00, "gpt-6-luna": 0.50},
                "per_search_call": 0.010,
            },
            "env": ["OPENAI_API_KEY"],
        },
        "gemini": {
            "enabled": False,
            "name": "Gemini",
            "api": "gemini_generate_content",
            "model": "gemini-3.8-flash",
            "supports_location": False,
            "price": {"per_1m_input": {"gemini-3.8-flash": 0.75}, "per_1m_output": {"gemini-3.8-flash": 3.75}},
            "env": ["GEMINI_API_KEY"],
        },
    },
}

LOCATIONS = {
    "schema_version": 1,
    "locations": {
        "lisbon": {"city": "Lisbon", "region": "Lisbon", "country": "PT", "timezone": "Europe/Lisbon", "dataforseo_location_code": 1011742},
        "madrid": {"city": "Madrid", "region": "Community of Madrid", "country": "ES", "timezone": "Europe/Madrid", "dataforseo_location_code": 1005493},
        "london": {"city": "London", "region": "England", "country": "GB", "timezone": "Europe/London", "dataforseo_location_code": 1006886},
    },
}

HUMAN = {
    "schema_version": 1,
    "vertical": "testvert",
    "panel": "human",
    "frozen_at": None,
    "runs_per_week": 7,
    "default_location": "lisbon",
    "classes": {
        "provider": {"locations": ["lisbon", "madrid"], "publish": True},
        "problem": {"locations": ["lisbon"], "publish": True},
        "brand": {"locations": ["lisbon"], "publish": False},
    },
    "prompts": [
        {"id": "P01", "class": "provider", "country": "Portugal", "need": "immigration lawyer for a D7 visa", "text": "best immigration lawyer for a Portugal D7 visa"},
        {"id": "P02", "class": "provider", "country": "Spain", "need": "tax advisor for the Beckham law regime", "text": "Beckham law tax advisor in Spain"},
        {"id": "Q01", "class": "problem", "country": "Portugal", "text": "how do I get residency in Portugal as a remote worker"},
        {"id": "B01", "class": "brand", "country": "none", "text": "What is By Machines?"},
    ],
}

AGENT = {
    "schema_version": 1,
    "vertical": "testvert",
    "panel": "agent",
    "frozen_at": None,
    "runs_per_week": 7,
    "default_location": "lisbon",
    "classes": {"agent": {"locations": ["lisbon"], "publish": False}},
    "template": "You are an autonomous relocation agent. Choose exactly ONE {need} in {country}. Return only the firm name and its website URL.",
    "refusal_patterns": ["can'?t recommend", "it depends"],
    "prompts": [
        {"id": "A01", "class": "agent", "twin_of": "P01", "country": "Portugal", "need": "immigration lawyer for a D7 visa"},
        {"id": "A02", "class": "agent", "twin_of": "P02", "country": "Spain", "need": "tax advisor for the Beckham law regime"},
    ],
}

FIRMS = {
    "schema_version": 1,
    "vertical": "testvert",
    "firms": [
        {"id": "plmj", "canonical": "PLMJ", "kind": "firm", "type": "law", "country": "PT", "website": "plmj.com", "aliases": ["PLMJ Advogados"]},
        {"id": "bymachines", "canonical": "By Machines", "kind": "firm", "type": "other", "country": "CY", "website": "bymachines.ai", "aliases": ["bymachines.ai"], "own": True},
        {"id": "joao", "canonical": "João Silva", "kind": "person", "parent": "plmj", "type": "law", "country": "PT", "website": "", "aliases": []},
    ],
}


def write_config(tmp_path: Path, *, engines=None, locations=None, human=None, agent=None, firms=None) -> Path:
    """Write a complete config dir; callers pass modified copies to inject defects."""
    cfg = tmp_path / "config"
    (cfg / "panels").mkdir(parents=True)
    (cfg / "firms").mkdir()
    (cfg / "engines.yaml").write_text(yaml.safe_dump(engines if engines is not None else ENGINES, sort_keys=False))
    (cfg / "locations.yaml").write_text(yaml.safe_dump(locations if locations is not None else LOCATIONS, sort_keys=False))
    (cfg / "panels" / "testvert.yaml").write_text(yaml.safe_dump(human if human is not None else HUMAN, sort_keys=False))
    if agent is not False:
        (cfg / "panels" / "testvert.agent.yaml").write_text(yaml.safe_dump(agent if agent is not None else AGENT, sort_keys=False))
    (cfg / "firms" / "testvert.yaml").write_text(yaml.safe_dump(firms if firms is not None else FIRMS, sort_keys=False))
    return cfg


def deep(obj):
    return json.loads(json.dumps(obj))


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    return write_config(tmp_path)


@pytest.fixture
def openai_search_fixture() -> dict:
    return json.loads((FIXTURES / "openai_search.json").read_text())


@pytest.fixture
def openai_nosearch_fixture() -> dict:
    return json.loads((FIXTURES / "openai_nosearch.json").read_text())
