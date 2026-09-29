"""Metrics (SPEC §10 reference values): Wilson interval, Jaccard stability, source shares, agent_single cell, source types."""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any, Iterable
from urllib.parse import urlparse

Z95 = 1.96
LISTICLE_PATH = re.compile(r"(^|[/_-])(best|top-?\d*)([/_-]|$)")


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval for k successes in n trials. 3/7 → [0.16, 0.75]; 0/7 → [0, 0.35]; 7/7 → [0.65, 1]."""
    if n <= 0:
        return 0.0, 0.0
    p = k / n
    z2 = z * z
    denom = 1 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    low = max(0.0, round(center - half, 10))
    high = min(1.0, round(center + half, 10))
    return low, high


def jaccard(a: Iterable[Any], b: Iterable[Any]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / len(sa | sb)


def source_shares(counts: dict[str, int]) -> dict[str, float]:
    total = sum(counts.values())
    if total <= 0:
        return {}
    return {domain: n / total for domain, n in counts.items()}


def agent_single_cell(runs: list[tuple[int, bool, list[str]]]) -> dict[str, Any]:
    """One agent-form cell (prompt × engine × week): runs as (n_firms, refusal, brand_ids) → M1–M3 counts and the slot holder."""
    n_runs = len(runs)
    singles: Counter[str] = Counter()
    n_single = n_refusal = n_unmatched = 0
    for n_firms, refusal, brand_ids in runs:
        if n_firms == 1:
            n_single += 1
            singles[brand_ids[0]] += 1
        elif n_firms == 0:
            if refusal:
                n_refusal += 1
            else:
                n_unmatched += 1
    if singles:
        top = max(singles.values())
        slot_firm = sorted(b for b, c in singles.items() if c == top)[0]      # ties → lowest id, deterministic
        slot_share = singles[slot_firm] / n_runs
    else:
        slot_firm, slot_share = "", 0.0
    return {"n_runs": n_runs, "n_single": n_single, "n_refusal": n_refusal, "n_unmatched": n_unmatched,
            "slot_firm": slot_firm, "slot_share": slot_share}


def _matches(domain: str, candidates: Iterable[str]) -> bool:
    return any(domain == c or domain.endswith("." + c) for c in candidates if c)


def source_type(url: str, domain: str, firm_domains: Iterable[str], rules: dict[str, list[str]]) -> str:
    """firm | social | forum | directory | official | listicle | other — by domain rules, then a listicle path heuristic."""
    domain = (domain or "").lower().removeprefix("www.")
    if _matches(domain, firm_domains):
        return "firm"
    for kind in ("social", "forum", "directory", "official"):
        if _matches(domain, rules.get(kind) or []):
            return kind
    path = (urlparse(url).path or "").lower()
    if LISTICLE_PATH.search(path):
        return "listicle"
    return "other"
