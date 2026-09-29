"""S3 — metrics: Wilson interval, Jaccard, source shares, agent_single cell, source types (SPEC §10 reference values)."""

from __future__ import annotations

import pytest

from lab.metrics import agent_single_cell, jaccard, source_shares, source_type, wilson


@pytest.mark.parametrize("k,n,low,high", [(3, 7, 0.16, 0.75), (0, 7, 0.00, 0.35), (7, 7, 0.65, 1.00)])
def test_wilson_reference_values(k, n, low, high):
    lo, hi = wilson(k, n)
    assert round(lo, 2) == low and round(hi, 2) == high


def test_wilson_edge_cases():
    assert wilson(0, 0) == (0.0, 0.0)
    lo, hi = wilson(1, 1)
    assert 0.0 < lo < 1.0 and hi == 1.0


def test_jaccard_reference():
    assert jaccard({"A", "B", "C"}, {"B", "C", "D"}) == 0.5
    assert jaccard(set(), set()) == 1.0
    assert jaccard({"A"}, set()) == 0.0


def test_source_share_sums_to_one():
    shares = source_shares({"a.com": 3, "b.com": 1, "c.com": 4})
    assert abs(sum(shares.values()) - 1.0) < 1e-12 and shares["c.com"] == 0.5
    assert source_shares({}) == {}


def test_agent_single_reference_cell():
    # 7 runs: 5 name exactly X, 1 names X and Y, 1 is a refusal (SPEC §10)
    runs = [(1, False, ["x"])] * 5 + [(2, False, ["x", "y"])] + [(0, True, [])]
    cell = agent_single_cell(runs)
    assert cell == {"n_runs": 7, "n_single": 5, "n_refusal": 1, "n_unmatched": 0, "slot_firm": "x", "slot_share": pytest.approx(5 / 7)}
    assert cell["slot_share"] >= 0.70


def test_agent_single_partial_week_and_no_slot():
    runs = [(1, False, ["x"]), (1, False, ["y"]), (0, False, []), (0, True, [])]
    cell = agent_single_cell(runs)
    assert cell["n_runs"] == 4 and cell["n_single"] == 2 and cell["n_unmatched"] == 1 and cell["n_refusal"] == 1
    assert cell["slot_firm"] in ("x", "y") and cell["slot_share"] == pytest.approx(0.25)   # ties → deterministic lowest id
    assert cell["slot_firm"] == "x"
    assert agent_single_cell([]) == {"n_runs": 0, "n_single": 0, "n_refusal": 0, "n_unmatched": 0, "slot_firm": "", "slot_share": 0.0}


def test_source_type_rules():
    firm_domains = {"plmj.com", "example-firm.pt"}
    rules = {"social": ["facebook.com", "linkedin.com"], "forum": ["reddit.com"], "directory": ["lawzana.com"], "official": ["gov.pt", "europa.eu"]}
    assert source_type("https://www.plmj.com/en/immigration", "plmj.com", firm_domains, rules) == "firm"
    assert source_type("https://english.example-firm.pt/d7", "english.example-firm.pt", firm_domains, rules) == "firm"   # subdomain of a firm site
    assert source_type("https://www.reddit.com/r/PortugalExpats/x", "reddit.com", firm_domains, rules) == "forum"
    assert source_type("https://lawzana.com/portugal/lawyers", "lawzana.com", firm_domains, rules) == "directory"
    assert source_type("https://aima.gov.pt/en/", "aima.gov.pt", firm_domains, rules) == "official"
    assert source_type("https://movingto.com/best-immigration-lawyers-portugal", "movingto.com", firm_domains, rules) == "listicle"
    assert source_type("https://movingto.com/pt/top-10-law-firms", "movingto.com", firm_domains, rules) == "listicle"
    assert source_type("https://blog.example.org/my-story", "blog.example.org", firm_domains, rules) == "other"
