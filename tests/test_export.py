"""S3 — extract_week over a stored week + the weekly export (SPEC §6 schema, §10 values, own/publish exclusions, agent_single)."""

from __future__ import annotations

import csv
import json

import pytest
import yaml

from conftest import AGENT, FIRMS, deep, write_config
from lab.config import load_vertical
from lab.export import export_week
from lab.extract import extract_week, rules_version_for
from lab.freeze import freeze
from lab.store import RunKey, RunRecord, Store


def _firms():
    firms = deep(FIRMS)
    firms["firms"].append({"id": "example", "canonical": "Example Firm", "kind": "firm", "type": "law", "country": "PT",
                           "website": "www.example-firm.pt", "aliases": ["Example Firm Advogados"]})
    firms["firms"].append({"id": "another", "canonical": "Another Firm", "kind": "firm", "type": "law", "country": "PT",
                           "website": "another-firm.example.com", "aliases": []})
    return firms


def _run(store, week, pid, engine, loc, idx, text, cites=(), status="ok", cost=0.01, ts=None):
    rec = RunRecord(key=RunKey(week, pid, engine, loc, idx), ts_utc=ts or f"2026-10-0{min(idx, 7)}T06:00:00Z", status=status, raw_json="{}",
                    answer_text=text if status == "ok" else None, cost_usd=cost, error=None if status == "ok" else "boom",
                    panel_sha="p" * 64, search=1, searched=1, catch_up=0, latency_ms=1, model="m",
                    citations=[(i + 1, u, d, "t") for i, (u, d) in enumerate(cites)])
    store.save_run(rec)


@pytest.fixture
def world(tmp_path):
    agent = deep(AGENT)
    agent["start_date"] = "2026-10-08"
    cfg = write_config(tmp_path, firms=_firms(), agent=agent)
    freeze(cfg / "panels" / "testvert.yaml", cfg)
    freeze(cfg / "panels" / "testvert.agent.yaml", cfg)
    v = load_vertical(cfg, "testvert")
    store = Store(":memory:")
    W = "2026-W41"
    ex = ("https://www.example-firm.pt/d7", "example-firm.pt")
    an = ("https://another-firm.example.com/x", "another-firm.example.com")
    rd = ("https://www.reddit.com/r/x", "reddit.com")
    bm = ("https://bymachines.ai/lab", "bymachines.ai")
    # P01 × openai × lisbon: 7 runs, Example Firm named in 3, Another in 1, one run cites bymachines.ai
    for i in range(1, 8):
        text = "Example Firm Advogados is recommended." if i <= 3 else ("Another Firm is an option." if i == 4 else "No specific names here.")
        cites = [ex, rd] if i <= 3 else ([an] if i == 4 else [bm] if i == 5 else [rd])
        _run(store, W, "P01", "openai", "lisbon", i, text, cites)
    # P01 × openai × madrid: 4 runs (partial), none mention firms
    for i in range(1, 5):
        _run(store, W, "P01", "openai", "madrid", i, "Generic advice.", [rd])
    # B01 (brand class, publish: false): By Machines named → must not enter brand_frequency; goes to own_frequency
    for i in range(1, 8):
        _run(store, W, "B01", "openai", "lisbon", i, "By Machines runs a lab at bymachines.ai.", [bm])
    # Q01 with an error row (cost counts, no mentions)
    _run(store, W, "Q01", "openai", "lisbon", 1, "", [], status="error", cost=0.02)
    # agent form A01 × openai × lisbon: 5 single Example, 1 double, 1 refusal
    for i in range(1, 8):
        text = "Example Firm — https://www.example-firm.pt" if i <= 5 else ("Example Firm or Another Firm" if i == 6 else "I can't recommend one.")
        _run(store, W, "A01", "openai", "lisbon", i, text, [ex])
    # previous week for stability: Example + Another mentioned by openai
    _run(store, "2026-W40", "P01", "openai", "lisbon", 4, "Example Firm and Another Firm.", [ex, an])
    return v, store, W, cfg


def test_extract_week_populates_mentions_unmatched_and_is_repeatable(world):
    v, store, W, cfg = world
    stats = extract_week(store, v, W)
    rules = rules_version_for(v.firms)
    assert stats["runs"] == 7 + 4 + 7 + 7 and stats["mentions"] == 3 + 1 + 7 + 5 + 2 and stats["rules_version"] == rules
    rows = store.conn.execute("SELECT brand_id, COUNT(*) FROM mentions WHERE rules_version=? GROUP BY brand_id ORDER BY brand_id", (rules,)).fetchall()
    assert [tuple(r) for r in rows] == [("another", 2), ("bymachines", 7), ("example", 9)]
    unmatched = {r[0] for r in store.conn.execute("SELECT candidate FROM unmatched")}
    assert "reddit.com" not in unmatched                                           # DEFAULT_IGNORE_DOMAINS keeps social sites out of the queue
    assert "another-firm.example.com" not in unmatched                             # a firm's own domain is never a candidate
    n_runs_before = store.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    stats2 = extract_week(store, v, W)                                              # re-extraction: same numbers, runs untouched
    assert stats2["mentions"] == stats["mentions"]
    assert store.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == n_runs_before
    assert store.conn.execute("SELECT COUNT(*) FROM mentions WHERE rules_version=?", (rules,)).fetchone()[0] == stats["mentions"]


def test_reextract_after_alias_change_changes_mentions_not_runs(world):
    v, store, W, cfg = world
    extract_week(store, v, W)
    firms = yaml.safe_load((cfg / "firms" / "testvert.yaml").read_text())
    firms["firms"] = [f for f in firms["firms"] if f["id"] != "another"]        # drop a firm → its mentions disappear under the new rules
    (cfg / "firms" / "testvert.yaml").write_text(yaml.safe_dump(firms, sort_keys=False))
    v2 = load_vertical(cfg, "testvert")
    stats = extract_week(store, v2, W)
    rules2 = rules_version_for(v2.firms)
    assert rules2 != rules_version_for(v.firms)
    assert store.conn.execute("SELECT COUNT(*) FROM mentions WHERE rules_version=? AND brand_id='another'", (rules2,)).fetchone()[0] == 0
    assert stats["mentions"] == 3 + 7 + 5 + 1                                       # A01 run 6 now names only Example → single


def test_export_week_files_and_values(world, tmp_path):
    v, store, W, cfg = world
    extract_week(store, v, W)
    extract_week(store, v, "2026-W40")
    out = tmp_path / "export"
    paths = export_week(store, v, W, out)
    names = {p.name for p in paths}
    assert {"brand_frequency.csv", "own_frequency.csv", "sources.csv", "stability.csv", "our_citation.csv", "costs.csv",
            "agent_single.csv", "export.json", "runs.csv"} <= names

    # cell = class × engine × location (schema 2): the provider cell in Lisbon is 7 runs, the brand class is a separate cell
    freq = {(r["brand"], r["class"], r["engine"], r["location"]): r for r in csv.DictReader((out / "brand_frequency.csv").open())}
    ex = freq[("example", "provider", "openai", "lisbon")]
    assert (int(ex["n_runs"]), int(ex["n_mentioned"])) == (7, 3) and float(ex["freq"]) == pytest.approx(3 / 7, abs=1e-4)
    assert (round(float(ex["wilson_low"]), 2), round(float(ex["wilson_high"]), 2)) == (0.16, 0.75)     # SPEC §10
    md = freq[("example", "provider", "openai", "madrid")]
    assert md["n_runs"] == "4" and md["n_mentioned"] == "0"
    assert all(r["brand"] != "bymachines" for r in freq.values())                                       # own brand never published
    assert all(r["class"] != "brand" for r in freq.values())                                            # publish: false class never published
    assert all(r["brand"] != "joao" for r in freq.values())                                             # person credited to plmj: no rows of its own
    own = {(r["brand"], r["class"], r["location"]): r for r in csv.DictReader((out / "own_frequency.csv").open())}
    assert own[("bymachines", "brand", "lisbon")]["n_mentioned"] == "7"                                 # B01 (publish: false) counted here only
    assert own[("bymachines", "provider", "lisbon")]["n_mentioned"] == "0"
    assert int(ex["n_runs"]) == 7                                                                       # B01 runs are not in the published denominator

    src = list(csv.DictReader((out / "sources.csv").open()))
    by_engine = {}
    for r in src:
        by_engine.setdefault(r["engine"], 0.0)
        by_engine[r["engine"]] += float(r["share"])
    assert all(abs(s - 1.0) < 1e-6 for s in by_engine.values())
    types = {r["domain"]: r["source_type"] for r in src}
    assert types["example-firm.pt"] == "firm" and types["reddit.com"] == "forum" and types["bymachines.ai"] in ("firm", "other")

    stab = {r["engine"]: r["jaccard_vs_prev_week"] for r in csv.DictReader((out / "stability.csv").open())}
    assert float(stab["openai"]) == pytest.approx(2 / 3)     # this week {example, another, bymachines} vs last week {example, another}

    ours = {(r["prompt_id"], r["engine"]): r for r in csv.DictReader((out / "our_citation.csv").open())}
    assert ours[("P01", "openai")]["n_cited"] == "1" and ours[("B01", "openai")]["n_cited"] == "7"

    costs = {r["engine"]: r for r in csv.DictReader((out / "costs.csv").open())}
    assert costs["openai"]["calls"] == str(7 + 4 + 7 + 1 + 7) and float(costs["openai"]["cost_usd"]) == pytest.approx(0.01 * 25 + 0.02)

    agent = {(r["prompt_id"], r["engine"]): r for r in csv.DictReader((out / "agent_single.csv").open())}
    a = agent[("A01", "openai")]
    assert (a["iso_week"], a["n_runs"], a["n_single"], a["n_refusal"], a["n_unmatched"], a["slot_firm"]) == (W, "7", "5", "1", "0", "example")
    assert float(a["slot_share"]) == pytest.approx(5 / 7, abs=1e-4)

    data = json.loads((out / "export.json").read_text())
    assert data["schema_version"] == 2 and data["iso_week"] == W and data["rules_version"].startswith("ext-")
    assert {"brand_frequency", "own_frequency", "sources", "stability", "our_citation", "costs", "agent_single"} <= set(data["tables"])
    assert len(data["tables"]["brand_frequency"]) == len(freq)
    prompts = {p["id"]: p for p in data["prompts"]}                         # consumers filter published rows by this, not by panel files
    assert prompts["P01"] == {"id": "P01", "class": "provider", "kind": "human", "publish": True}
    assert prompts["B01"]["publish"] is False and prompts["A01"]["kind"] == "agent" and prompts["A01"]["publish"] is False


def test_export_without_extraction_refuses(world, tmp_path):
    v, store, W, cfg = world
    with pytest.raises(RuntimeError, match="extract"):
        export_week(store, v, W, tmp_path / "e")
