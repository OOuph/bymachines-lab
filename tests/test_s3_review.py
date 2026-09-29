"""S3 independent review (2026-09-29) — the counter-examples that failed before the fixes. Each test asserts the behaviour SPEC
§6/§8 promises; a failing test here = a regression of a confirmed defect (4 MAJOR + 10 MINOR)."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest
import yaml

from conftest import AGENT, FIRMS, deep, write_config
from lab.config import ConfigError, load_vertical
from lab.export import export_week
from lab.extract import FirmIndex, candidates_from_text, derived_variant, extract_answer, extract_week, is_refusal
from lab.freeze import freeze
from lab.store import RunKey, RunRecord, Store

REPO = Path(__file__).resolve().parents[1]
REAL_AGENT = yaml.safe_load((REPO / "config/panels/relocation-europe.agent.yaml").read_text())
REAL_FIRMS = yaml.safe_load((REPO / "config/firms/relocation-europe.yaml").read_text())
EX = ("https://www.example-firm.pt/d7", "example-firm.pt")


def _firms(extra=None):
    firms = deep(FIRMS)
    firms["firms"].append({"id": "example", "canonical": "Example Firm", "kind": "firm", "type": "law", "country": "PT",
                           "website": "www.example-firm.pt", "aliases": ["Example Firm Advogados"]})
    firms["firms"].append({"id": "another", "canonical": "Another Firm", "kind": "firm", "type": "law", "country": "PT",
                           "website": "another-firm.example.com", "aliases": []})
    if extra:
        firms["firms"].extend(extra)
    return firms


def _run(store, week, pid, engine, loc, idx, text, cites=(), status="ok", cost=0.01):
    rec = RunRecord(key=RunKey(week, pid, engine, loc, idx), ts_utc=f"2026-10-0{min(idx, 7)}T06:00:00Z", status=status, raw_json="{}",
                    answer_text=text if status == "ok" else None, cost_usd=cost, error=None if status == "ok" else "boom",
                    panel_sha="p" * 64, search=1, searched=1, catch_up=0, latency_ms=1, model="m",
                    citations=[(i + 1, u, d, "t") for i, (u, d) in enumerate(cites)])
    store.save_run(rec)


def _world(tmp_path, firms=None, twist=None):
    agent = deep(AGENT)
    agent["start_date"] = "2026-10-08"
    cfg = write_config(tmp_path, firms=firms or _firms(), agent=agent)
    if twist:
        (cfg / "panels" / "testvert.twist.yaml").write_text(yaml.safe_dump(twist, sort_keys=False))
    freeze(cfg / "panels" / "testvert.yaml", cfg)
    freeze(cfg / "panels" / "testvert.agent.yaml", cfg)
    v = load_vertical(cfg, "testvert")
    return v, Store(":memory:"), "2026-W41", cfg


def _index(tmp_path, extra, ignore_terms=None):
    firms = _firms(extra)
    if ignore_terms:
        firms["unmatched_ignore_terms"] = list(ignore_terms)
    v = load_vertical(write_config(tmp_path / str(abs(hash(str(extra)))), firms=firms), "testvert")
    return FirmIndex(v.firms, blocked_variants=v.extraction.get("unmatched_ignore_terms") or [])


# ---------------------------------------------------------------- MAJOR 1: export completeness

def test_export_refuses_when_runs_arrived_after_the_last_extract(tmp_path):
    v, store, W, cfg = _world(tmp_path)
    for i in range(1, 4):
        _run(store, W, "P01", "openai", "lisbon", i, "Example Firm Advogados is recommended.", [EX])
        _run(store, W, "A01", "openai", "lisbon", i, "Example Firm — https://www.example-firm.pt", [EX])
    extract_week(store, v, W)
    for i in range(4, 8):                                            # Thursday..Sunday runs arrive after Wednesday's extract
        _run(store, W, "P01", "openai", "lisbon", i, "Example Firm Advogados is recommended.", [EX])
        _run(store, W, "A01", "openai", "lisbon", i, "Example Firm — https://www.example-firm.pt", [EX])
    with pytest.raises(RuntimeError, match="incomplete"):
        export_week(store, v, W, tmp_path / "e")
    extract_week(store, v, W)
    out = tmp_path / "e2"
    export_week(store, v, W, out)
    freq = {(r["brand"], r["location"]): r for r in csv.DictReader((out / "brand_frequency.csv").open())}
    agent = {r["prompt_id"]: r for r in csv.DictReader((out / "agent_single.csv").open())}
    assert (freq[("example", "lisbon")]["n_runs"], freq[("example", "lisbon")]["n_mentioned"]) == ("7", "7")
    assert (agent["A01"]["n_single"], agent["A01"]["n_unmatched"], agent["A01"]["slot_share"]) == ("7", "0", "1.000000")


# ---------------------------------------------------------------- MAJOR 2: the queue follows the current rules

def test_unmatched_queue_filters_by_current_rules_version(tmp_path):
    from lab.extract import rules_version_for

    v, store, W, cfg = _world(tmp_path)
    _run(store, W, "P01", "openai", "lisbon", 1, "Contact Abreu Advogados in Lisbon.", [("https://abreuadvogados.com/x", "abreuadvogados.com")])
    extract_week(store, v, W)
    before = {(r["candidate"], r["source"]): r["n"] for r in store.unmatched_summary(W, rules_version=rules_version_for(v.firms))}
    assert before[("Abreu Advogados", "text")] == 1 and before[("abreuadvogados.com", "domain")] == 1
    firms = yaml.safe_load((cfg / "firms" / "testvert.yaml").read_text())
    firms["firms"].append({"id": "abreu", "canonical": "Abreu Advogados", "kind": "firm", "type": "law", "country": "PT",
                           "website": "abreuadvogados.com", "aliases": []})
    (cfg / "firms" / "testvert.yaml").write_text(yaml.safe_dump(firms, sort_keys=False))
    v2 = load_vertical(cfg, "testvert")
    stats = extract_week(store, v2, W)                               # the firm is now in the list → it leaves the queue
    assert stats["purged_unmatched"] == 2
    after = {(r["candidate"], r["source"]) for r in store.unmatched_summary(W, rules_version=rules_version_for(v2.firms))}
    assert ("Abreu Advogados", "text") not in after and ("abreuadvogados.com", "domain") not in after
    assert store.conn.execute("SELECT COUNT(*) FROM unmatched").fetchone()[0] == 0   # nothing of the old rules survives


# ---------------------------------------------------------------- MAJOR 3: ignore terms drop only whole matches

def test_ignore_terms_do_not_swallow_firm_names_that_contain_a_place_name(tmp_path):
    cfg = write_config(tmp_path / "real", firms=deep(REAL_FIRMS))
    v = load_vertical(cfg, "testvert")
    idx = FirmIndex(v.firms)
    ignore = set(v.extraction["unmatched_ignore_terms"])
    text = ("Options: Harvey Law Group Portugal, PwC Portugal, CMS Portugal, Portugal Residency Advisors, Lisbon Lawyers, "
            "Madrid Abogados, Cyprus Company Formation Services and Global Citizen Solutions. The Portugal Golden Visa is separate.")
    cands = candidates_from_text(text, idx, ignore)
    for name in ("Harvey Law Group Portugal", "PwC Portugal", "CMS Portugal", "Portugal Residency Advisors", "Lisbon Lawyers",
                 "Madrid Abogados", "Cyprus Company Formation Services", "Global Citizen Solutions"):
        assert name in cands, (name, cands)
    assert "Portugal Golden Visa" not in cands and "Portugal" not in cands          # made only of ignored terms → dropped


# ---------------------------------------------------------------- MAJOR 4: typographic apostrophes in refusals

def test_refusal_patterns_catch_typographic_apostrophe_with_the_frozen_list():
    pats = REAL_AGENT["refusal_patterns"]
    assert is_refusal("I can't recommend one specific firm.", pats)
    assert is_refusal("I can’t recommend one specific firm.", pats)
    assert is_refusal("I don’t have enough information to choose.", pats)
    assert not is_refusal("Example Firm — https://www.example-firm.pt", pats)


# ---------------------------------------------------------------- MINOR 5: twist cells in agent_single

def test_twist_cells_get_agent_single_rows(tmp_path):
    twist = {"schema_version": 1, "vertical": "testvert", "panel": "twist", "iso_week": "2026-W41", "start_date": "2026-10-08",
             "runs_per_week": 7, "default_location": "lisbon", "classes": {"twist": {"locations": ["lisbon"], "publish": False}},
             "engines": ["openai"], "engine_options": {"openai": {"search": False}},
             "template": "Choose exactly ONE {need} in {country}.", "refusal_patterns": ["can'?t recommend"],
             "prompts": [{"id": "T41-01", "class": "twist", "twin_of": "A01", "country": "Portugal", "need": "immigration lawyer for a D7 visa"}]}
    v, store, W, cfg = _world(tmp_path, twist=twist)
    for i in range(1, 8):
        _run(store, W, "A01", "openai", "lisbon", i, "Example Firm — https://www.example-firm.pt", [EX])
        _run(store, W, "T41-01", "openai", "lisbon", i, "Example Firm — https://www.example-firm.pt", [])
    extract_week(store, v, W)
    out = tmp_path / "e"
    export_week(store, v, W, out)
    rows = {r["prompt_id"]: r for r in csv.DictReader((out / "agent_single.csv").open())}
    assert "A01" in rows and "T41-01" in rows
    assert rows["T41-01"]["slot_firm"] == "example" and rows["T41-01"]["n_single"] == "7"


# ---------------------------------------------------------------- MINOR 6 / 13: over-stripping and collisions

def test_derived_variant_keeps_surnames_and_place_names():
    assert derived_variant("miranda sa") is None                                      # surname, not S.A.
    assert derived_variant("cuatrecasas abogados s l p") == "cuatrecasas"
    assert derived_variant("plmj sociedade de advogados sp rl") == "plmj"
    assert derived_variant("example firm lda") == "example firm"
    assert derived_variant("porto advogados", blocked={"porto"}) is None              # place names never become bare variants


def test_place_name_firm_does_not_match_the_place(tmp_path):
    idx = _index(tmp_path, [{"id": "porto", "canonical": "Porto Advogados", "kind": "firm", "type": "law", "country": "PT", "website": "", "aliases": []}],
                 ignore_terms=["Porto", "Lisbon"])                      # the real config lists the place names of the vertical
    res = extract_answer("Offices in Lisbon and Porto. Ask which lawyer will handle the file.", [], idx)
    assert res.mentions == []
    assert [m.brand_id for m in extract_answer("Call Porto Advogados first.", [], idx).mentions] == ["porto"]


def test_colliding_matching_keys_are_rejected_by_validation(tmp_path):
    extra = [{"id": "miranda_assoc", "canonical": "Miranda & Associados", "kind": "firm", "type": "law", "country": "PT", "website": "", "aliases": []},
             {"id": "miranda2", "canonical": "Miranda Lda", "kind": "firm", "type": "law", "country": "PT", "website": "", "aliases": []}]
    with pytest.raises(ConfigError, match="collides"):
        load_vertical(write_config(tmp_path, firms=_firms(extra)), "testvert")


def test_two_letter_acronym_matches_and_lowercase_short_alias_is_rejected(tmp_path):
    idx = _index(tmp_path, [{"id": "ey", "canonical": "EY", "kind": "firm", "type": "tax", "country": "PT", "website": "ey.com", "aliases": []}])
    assert [m.brand_id for m in extract_answer("For IFICI, EY and PwC Portugal both advertise the service.", [], idx).mentions] == ["ey"]
    assert extract_answer("They convey the message.", [], idx).mentions == []
    with pytest.raises(ConfigError, match="too short"):
        load_vertical(write_config(tmp_path / "short", firms=_firms([{"id": "sa", "canonical": "Sá", "kind": "firm", "type": "law",
                                                                        "country": "PT", "website": "", "aliases": []}])), "testvert")


# ---------------------------------------------------------------- MINOR 7 / 8: sentence boundaries

def test_multiword_alias_does_not_match_across_sentence_boundary(tmp_path):
    idx = _index(tmp_path, [{"id": "ll", "canonical": "Lisbon Lawyers", "kind": "firm", "type": "law", "country": "PT", "website": "", "aliases": []}])
    assert extract_answer("Most firms are in Lisbon. Lawyers there charge €1,500.", [], idx).mentions == []
    assert extract_answer("Most firms are in Lisbon\nLawyers there charge €1,500.", [], idx).mentions == []
    assert [m.brand_id for m in extract_answer("Try Lisbon Lawyers, they are fast.", [], idx).mentions] == ["ll"]


def test_candidate_does_not_run_over_a_full_stop_or_keep_a_leading_verb(tmp_path):
    idx = _index(tmp_path, [])
    cands = candidates_from_text("I’d start with Abreu Advogados. Ask for a written quote from Reis & Pellicano. Compare two quotes.\n"
                                 "Contact Abreu Advogados in Lisbon. Shortlist LVP Advogados next.", idx, set())
    assert "Abreu Advogados" in cands and "Reis & Pellicano" in cands and "LVP Advogados" in cands
    assert not any("." in c or c.startswith(("Contact ", "Shortlist ", "Ask ")) for c in cands), cands


# ---------------------------------------------------------------- MINOR 9 / 10: export rows

def test_brand_frequency_has_no_rows_for_entities_credited_to_a_parent(tmp_path):
    v, store, W, cfg = _world(tmp_path)
    for i in range(1, 8):
        _run(store, W, "P01", "openai", "lisbon", i, "Contact João Silva at PLMJ.", [])
    extract_week(store, v, W)
    out = tmp_path / "e"
    export_week(store, v, W, out)
    rows = {(r["brand"], r["location"]): r for r in csv.DictReader((out / "brand_frequency.csv").open())}
    assert rows[("plmj", "lisbon")]["n_mentioned"] == "7"
    assert ("joao", "lisbon") not in rows


def test_stability_blank_for_engine_absent_last_week(tmp_path):
    v, store, W, cfg = _world(tmp_path)
    _run(store, "2026-W40", "P01", "openai", "lisbon", 4, "Example Firm Advogados.", [EX])
    for i in range(1, 8):
        _run(store, W, "P01", "openai", "lisbon", i, "Example Firm Advogados.", [EX])
        _run(store, W, "P01", "gemini", "n/a", i, "Example Firm Advogados.", [EX])
    extract_week(store, v, "2026-W40")
    extract_week(store, v, W)
    out = tmp_path / "e"
    export_week(store, v, W, out)
    stab = {r["engine"]: r["jaccard_vs_prev_week"] for r in csv.DictReader((out / "stability.csv").open())}
    assert float(stab["openai"]) == 1.0 and stab["gemini"] == ""


# ---------------------------------------------------------------- MINOR 12 / 14: single tokens and headings

def test_single_token_firm_names_reach_the_queue_with_evidence(tmp_path):
    idx = _index(tmp_path, [])
    text = ("## Fee Expectations\n**Power of Attorney**\n"
            "- **Garrigues** — large Spanish firm\n1. Lexidy: boutique\n"
            "LACA is recommended in Portugal; OnCorporate and TaxAccountant.pt handle company formation. Cuatrecasas is bigger.")
    cands = candidates_from_text(text, idx, set())
    for name in ("Garrigues", "Lexidy", "LACA", "OnCorporate", "TaxAccountant.pt"):
        assert name in cands, (name, cands)
    assert "Fee Expectations" not in cands and "Power of Attorney" not in cands      # headings are not firms
    assert "Cuatrecasas" not in cands                                                  # a bare mid-sentence word has no evidence


def test_dash_joins_a_name_and_its_legal_descriptor(tmp_path):
    idx = _index(tmp_path, [])
    cands = candidates_from_text("BAS – Sociedade de Advogados is one; Lisbon – Porto is a route.", idx, set())
    assert "BAS – Sociedade de Advogados" in cands and "Lisbon – Porto" not in cands
