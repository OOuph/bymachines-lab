"""S3 — extraction: alias matching with legal-form normalisation, person/product credit, unmatched queue, refusal, re-extraction."""

from __future__ import annotations

import pytest

from conftest import FIRMS, deep, write_config
from lab.config import load_vertical
from lab.extract import (FirmIndex, candidates_from_domains, candidates_from_text, extract_answer, is_refusal, normalize_text,
                         rules_version_for)
from lab.engines.base import Citation


def _firms(extra=None):
    firms = deep(FIRMS)
    firms["firms"].append({"id": "cuatrecasas", "canonical": "Cuatrecasas", "kind": "firm", "type": "law", "country": "ES",
                           "website": "cuatrecasas.com", "aliases": ["Cuatrecasas Abogados S.L.P."]})
    firms["firms"].append({"id": "example", "canonical": "Example Firm", "kind": "firm", "type": "law", "country": "PT",
                           "website": "www.example-firm.pt", "aliases": ["Example Firm Advogados", "Example Firm, Lda."]})
    firms["firms"].append({"id": "d7pro", "canonical": "D7 Pro", "kind": "product", "parent": "example", "type": "relocation",
                           "country": "PT", "website": "", "aliases": ["D7Pro app"]})
    if extra:
        firms["firms"].extend(extra)
    return firms


@pytest.fixture
def vertical(tmp_path):
    cfg = write_config(tmp_path, firms=_firms())
    return load_vertical(cfg, "testvert")


def test_normalize_text_strips_accents_punctuation_and_case():
    assert normalize_text("PLMJ – Sociedade de Advogados, SP, RL") == "plmj sociedade de advogados sp rl"
    assert normalize_text("Cuatrecasas Abogados S.L.P.") == "cuatrecasas abogados s l p"
    assert normalize_text("  Vieira   de Almeida & Associados  ") == "vieira de almeida associados"
    assert normalize_text("São João Advogados") == "sao joao advogados"


def test_reference_matches_from_spec(vertical):
    idx = FirmIndex(vertical.firms)
    m1 = extract_answer("For a D7 visa many recommend PLMJ – Sociedade de Advogados, SP, RL in Lisbon.", [], idx).mentions
    m2 = extract_answer("plmj advogados is well known", [], idx).mentions
    m3 = extract_answer("In Spain, Cuatrecasas Abogados S.L.P. handles Beckham law cases.", [], idx).mentions
    assert [m.brand_id for m in m1] == ["plmj"] and [m.brand_id for m in m2] == ["plmj"]
    assert [m.brand_id for m in m3] == ["cuatrecasas"]


def test_word_boundary_and_order(vertical):
    idx = FirmIndex(vertical.firms)
    res = extract_answer("Example Firm, Lda. and later PLMJ; but PLMJX is something else and plmj.com is their site.", [], idx)
    assert [(m.brand_id, m.position) for m in res.mentions] == [("example", 1), ("plmj", 2)]
    assert extract_answer("The PLMJXpress service", [], idx).mentions == []


def test_person_and_product_credit_parent_firm(vertical):
    idx = FirmIndex(vertical.firms)
    res = extract_answer("Contact João Silva for the D7; his team also built the D7Pro app.", [], idx)
    assert [(m.brand_id, m.matched_alias) for m in res.mentions] == [("plmj", "João Silva"), ("example", "D7Pro app")]


def test_same_firm_twice_is_one_mention(vertical):
    idx = FirmIndex(vertical.firms)
    res = extract_answer("PLMJ is good. PLMJ Advogados again. Also plmj.", [], idx)
    assert len(res.mentions) == 1 and res.mentions[0].brand_id == "plmj"


def test_unmatched_domain_candidates(vertical):
    idx = FirmIndex(vertical.firms)
    cits = [Citation(1, "https://english.example-firm.pt/d7", "t", "english.example-firm.pt"),
            Citation(2, "https://www.reddit.com/r/x", "t", "reddit.com"),
            Citation(3, "https://another-firm.example.com/immigration", "t", "another-firm.example.com"),
            Citation(4, "https://bymachines.ai/lab", "t", "bymachines.ai")]
    cands = candidates_from_domains(cits, idx, ignore_domains={"reddit.com"})
    assert cands == ["another-firm.example.com"]          # firm sites (incl. subdomains) and ignored domains are not candidates


def test_unmatched_text_candidates(vertical):
    idx = FirmIndex(vertical.firms)
    text = ("For a Portugal D7 visa, firms such as Example Firm, Global Citizen Solutions and Lexidy Law Boutique are named. "
            "The Golden Visa route differs. In Lisbon, Vieira de Almeida is also cited.")
    cands = candidates_from_text(text, idx, ignore_terms={"Golden Visa", "Portugal", "Lisbon"})
    assert "Global Citizen Solutions" in cands and "Lexidy Law Boutique" in cands and "Vieira de Almeida" in cands
    assert not any("Example Firm" in c for c in cands) and "Golden Visa" not in cands and "Portugal" not in cands
    tail = candidates_from_text("Full-Service Commercial Law Firms If your case is complex, Abreu Advogados can help.", idx, set())
    assert "Full-Service Commercial Law Firms" in tail and "Abreu Advogados" in tail and not any(c.endswith(" If") for c in tail)


def test_refusal_patterns_from_config():
    patterns = ["can'?t recommend", "cannot recommend a specific", "it depends", "several (options|firms)"]
    assert is_refusal("I can't recommend one specific firm, it depends on your case.", patterns)
    assert is_refusal("There are several firms that could help.", patterns)
    assert not is_refusal("Example Firm Advogados — https://www.example-firm.pt", patterns)
    assert not is_refusal("", [])


def test_extract_answer_refusal_flag_only_without_mentions(vertical):
    idx = FirmIndex(vertical.firms)
    patterns = ["can'?t recommend"]
    assert extract_answer("I can't recommend a specific firm.", [], idx, refusal_patterns=patterns).refusal is True
    assert extract_answer("I can't recommend others, but PLMJ is solid.", [], idx, refusal_patterns=patterns).refusal is False


def test_rules_version_changes_with_aliases(tmp_path):
    v1 = load_vertical(write_config(tmp_path / "a", firms=_firms()), "testvert")
    firms2 = _firms()
    firms2["firms"][0]["aliases"].append("PLMJ Law")
    v2 = load_vertical(write_config(tmp_path / "b", firms=firms2), "testvert")
    r1, r2 = rules_version_for(v1.firms), rules_version_for(v2.firms)
    assert r1 != r2 and r1.startswith("ext-") and len(r1.split(":")[1]) == 16
    assert rules_version_for(v1.firms) == r1                                   # deterministic
