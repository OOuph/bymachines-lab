"""Extraction (SPEC S-B, S-K): firm mentions by alias matching, the unmatched queue, refusal check, re-extraction.

Rules are data: the firm list (aliases, kinds, parents) and the extraction settings live in config/firms/<vertical>.yaml;
`rules_version` hashes them together with EXTRACT_RULES so every mention row says which rules produced it and a changed
alias re-extracts from the stored raw answers (runs are never touched).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable

from lab.config import Firm, Vertical
from lab.engines.base import Citation, domain_of
from lab.store import Store

EXTRACT_RULES = "ext-2"       # bump when the matching rules in this file change (S3 review 2026-09-29: ext-1 → ext-2)

# Legal-form suffixes stripped from the END of an alias to derive a shorter matching variant.
# STRONG tokens are legal forms on their own ("Advogados", "Lda", "S.L.P." → "slp"); WEAK tokens are only stripped as part
# of a trailing legal chain that already contains a stripped token ("Sociedade de Advogados" → all three go; "Miranda & Sá"
# → "sa" stays, it is a surname). Single letters ("S.L.P." → "s l p") are stripped when at least two of them trail together.
LEGAL_STRONG = {"lda", "ltd", "llc", "llp", "sl", "slp", "sp", "rl", "plc", "inc", "srl", "gmbh", "ag", "bv", "unipessoal",
                "advogados", "abogados", "associados", "asociados"}
LEGAL_WEAK = {"sociedade", "de", "sa"}
LEGAL_FORMS = LEGAL_STRONG | LEGAL_WEAK | {"s", "l", "p", "r", "u", "a"}
# leading tokens that start a sentence but never a firm name (articles, pronouns, imperatives typical of an assistant's answer)
LEADING_STOPWORDS = {"the", "in", "for", "a", "an", "if", "when", "also", "however", "these", "this", "there", "it", "at", "on",
                     "with", "as", "but", "and", "or", "some", "many", "most", "while", "although", "here", "since", "consider",
                     "note", "another", "other", "both", "each", "best", "top", "several", "their", "its", "your", "our", "we",
                     "you", "they", "i", "he", "she", "one", "two", "three", "first", "second", "yes", "no",
                     "contact", "ask", "call", "email", "visit", "compare", "shortlist", "choose", "avoid", "hire", "look", "book",
                     "start", "try", "use", "check", "see", "read", "search", "find", "get", "go", "make", "take", "expect",
                     "remember", "then", "next", "finally", "alternatively", "otherwise", "instead", "again", "still", "so",
                     "because", "before", "after", "once", "unless", "until", "even", "just", "only", "option", "options",
                     "example", "examples", "step", "steps", "tip", "tips", "recommended", "recommendation", "recommendations",
                     "popular", "notable", "leading", "reputable", "large", "small", "local", "international", "specialised",
                     "specialized", "why", "what", "how", "which", "where", "who", "my", "his", "her", "not", "none", "all"}
# connectors inside a name ("Vieira de Almeida", "Law Office of X"); "and"/"e"/"y" are left out — in answers they join lists of firms
CONNECTORS = {"de", "da", "do", "dos", "das", "&", "of", "the", "del", "la", "le", "van", "von"}
# a dash joins a name and its legal descriptor ("BAS – Sociedade de Advogados"); it never joins two names
DASHES = {"-", "–", "—"}
LEGAL_WORDS = {"sociedade", "advogados", "abogados", "associados", "asociados", "law", "lawyers", "legal", "tax", "consulting",
               "group", "partners", "advisors", "advisers", "immigration", "relocation", "accountants", "solicitors", "abogado",
               "advogado", "lda", "ltd", "llp", "sl", "slp"}
DEFAULT_IGNORE_DOMAINS = ["reddit.com", "facebook.com", "linkedin.com", "youtube.com", "google.com", "wikipedia.org", "x.com",
                          "twitter.com", "instagram.com", "quora.com", "tiktok.com", "medium.com", "trustpilot.com", "apple.com"]
DEFAULT_SOURCE_TYPES: dict[str, list[str]] = {
    "social": ["facebook.com", "linkedin.com", "youtube.com", "x.com", "twitter.com", "instagram.com", "tiktok.com"],
    "forum": ["reddit.com", "quora.com", "expatforum.com", "internations.org", "facebook.com/groups"],
    "directory": ["lawzana.com", "legal500.com", "chambers.com", "clutch.co", "sortlist.com", "justia.com", "hg.org", "martindale.com",
                  "findlaw.com", "avvo.com", "lexology.com", "leadersleague.com", "expatriates.com", "yelp.com", "trustpilot.com"],
    "official": ["gov.pt", "gov.es", "gov.cy", "europa.eu", "aima.gov.pt", "sef.pt", "portaldascomunidades.mne.gov.pt", "exteriores.gob.es",
                 "moi.gov.cy", "mof.gov.cy", "portaldasfinancas.gov.pt", "eportugal.gov.pt", "agenciatributaria.es"],
}

SENT_SEP = "¦"                                  # sentence separator token inside a normalised answer ("¦"), never in an alias
_WORD = re.compile(r"[^a-z0-9¦]+")
_SENT_END = re.compile(r"[.!?;:](?=\s|$)|[\n\r]+")     # punctuation followed by whitespace/end (not "S.L.P." inside a token)
_APOS = re.compile(r"[’‘´`]")     # typographic apostrophes → ASCII
_CAP = re.compile(r"^[A-ZÀ-ÖØ-Þ][\w'&.-]*$")
_CAMEL = re.compile(r"^[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ]+[A-ZÀ-ÖØ-Þ]")   # "OnCorporate", "DefesaLegal"
_ALLCAPS = re.compile(r"^[A-Z][A-Z0-9]{2,}$")                  # "LACA", "PLMJ"
_HEADING = re.compile(r"^\s*(#{1,6}\s|\*\*[^*]+\*\*\s*:?\s*$)")  # markdown heading or a bold-only line
_LIST_LEAD = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+\**([A-ZÀ-ÖØ-Þ][\w'&.-]*)\**\s*(?:[:–—-]|$)")


@dataclass(frozen=True)
class Mention:
    brand_id: str
    position: int
    matched_alias: str


@dataclass
class ExtractResult:
    mentions: list[Mention] = field(default_factory=list)
    unmatched_domains: list[str] = field(default_factory=list)
    unmatched_terms: list[str] = field(default_factory=list)
    refusal: bool = False


def normalize_text(s: str) -> str:
    """Lowercase, accents stripped, punctuation → space, whitespace collapsed. Applied to aliases and answers alike."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    return " ".join(_WORD.sub(" ", s.replace(SENT_SEP, " ")).split())


def normalize_answer(s: str) -> str:
    """normalize_text for an answer: sentence ends and line breaks become the SENT_SEP token so a multi-word alias never
    matches across "… in Lisbon. Lawyers there …"."""
    s = _SENT_END.sub(f" {SENT_SEP} ", s or "")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    return " ".join(_WORD.sub(" ", s).split())


def derived_variant(alias_norm: str, blocked: Iterable[str] = ()) -> str | None:
    """'cuatrecasas abogados s l p' → 'cuatrecasas'; 'plmj sociedade de advogados sp rl' → 'plmj'; 'miranda sa' → None (surname);
    'porto advogados' → None when 'porto' is a blocked term (place names from unmatched_ignore_terms)."""
    tokens = alias_norm.split()
    popped = 0
    while tokens:
        t = tokens[-1]
        single_run = 0
        for x in reversed(tokens):
            if len(x) == 1:
                single_run += 1
            else:
                break
        if t in LEGAL_STRONG or (len(t) == 1 and single_run >= 2) or (t in LEGAL_WEAK and popped) or (len(t) == 1 and popped):
            tokens.pop()
            popped += 1
        else:
            break
    variant = " ".join(tokens)
    if not popped or variant == alias_norm or len(variant) < 4 or variant in LEGAL_FORMS or variant in set(blocked):
        return None
    return variant


def _pattern(alias_norm: str) -> re.Pattern[str]:
    return re.compile(r"(?<![a-z0-9])" + r"\s+".join(re.escape(t) for t in alias_norm.split()) + r"(?![a-z0-9])")


def alias_keys(alias: str, blocked: Iterable[str] = ()) -> list[str]:
    """The normalised forms under which an alias matches (its own and the derived variant). Used by config validation too."""
    norm = normalize_text(alias)
    if len(norm) < 2:
        return []
    keys = [norm]
    v = derived_variant(norm, blocked)
    if v:
        keys.append(v)
    return keys


def rules_version_for(firms: Iterable[Firm]) -> str:
    """EXTRACT_RULES + a 16-hex digest of the firm list (ids, canonicals, kinds, parents, aliases, websites, own flags)."""
    payload = sorted(
        [{"id": f.id, "canonical": f.canonical, "kind": f.kind, "parent": f.parent, "aliases": sorted(f.aliases), "website": f.website, "own": f.own}
         for f in firms],
        key=lambda d: d["id"],
    )
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
    return f"{EXTRACT_RULES}:{digest}"


def website_domain(website: str) -> str:
    w = (website or "").strip().lower()
    if not w:
        return ""
    if "://" not in w:
        w = "https://" + w
    return domain_of(w)


class FirmIndex:
    """Compiled alias patterns → credited brand id, plus the set of firm domains."""

    def __init__(self, firms: Iterable[Firm], blocked_variants: Iterable[str] = ()):
        self.firms = {f.id: f for f in firms}
        self.patterns: list[tuple[str, str, re.Pattern[str]]] = []      # (brand_id, alias as written, pattern)
        self.firm_domains: dict[str, str] = {}                           # domain → brand id
        blocked = {normalize_text(b) for b in blocked_variants}
        seen: set[tuple[str, str]] = set()
        variants: list[str] = []
        for f in self.firms.values():
            credit = f.parent if f.parent and f.parent in self.firms else f.id
            for alias in [f.canonical, *f.aliases]:
                for variant in alias_keys(alias, blocked):
                    if (credit, variant) in seen:
                        continue
                    seen.add((credit, variant))
                    self.patterns.append((credit, alias, _pattern(variant)))
                    variants.append(variant)
            d = website_domain(f.website)
            if d:
                self.firm_domains[d] = credit
        # one alternation for the cheap "does this text mention any firm" question (unmatched queue)
        alts = sorted({r"\s+".join(re.escape(t) for t in v.split()) for v in variants}, key=len, reverse=True)
        self._any = re.compile(r"(?<![a-z0-9])(?:" + "|".join(alts) + r")(?![a-z0-9])") if alts else None

    def is_firm_domain(self, domain: str) -> bool:
        domain = (domain or "").lower().removeprefix("www.")
        return any(domain == d or domain.endswith("." + d) for d in self.firm_domains)

    def contains_alias(self, text_norm: str) -> bool:
        return bool(self._any and self._any.search(text_norm))


def is_refusal(text: str, patterns: Iterable[str]) -> bool:
    """Frozen regexes are written with the ASCII apostrophe; engines answer with ’ (U+2019) — normalised here, not in the list."""
    t = _APOS.sub("'", text or "").replace(" ", " ")
    return any(re.search(p, t, re.IGNORECASE) for p in patterns if p)


def extract_answer(text: str, citations: Iterable[Citation], index: FirmIndex, refusal_patterns: Iterable[str] | None = None,
                   ignore_domains: Iterable[str] | None = None, ignore_terms: Iterable[str] | None = None) -> ExtractResult:
    norm = normalize_answer(text or "")
    first: dict[str, tuple[int, str]] = {}
    for brand_id, alias, pattern in index.patterns:
        m = pattern.search(norm)
        if m and (brand_id not in first or m.start() < first[brand_id][0]):
            first[brand_id] = (m.start(), alias)
    ordered = sorted(first.items(), key=lambda kv: kv[1][0])
    mentions = [Mention(brand_id=b, position=i + 1, matched_alias=alias) for i, (b, (_, alias)) in enumerate(ordered)]
    citations = list(citations)
    res = ExtractResult(mentions=mentions)
    res.unmatched_domains = candidates_from_domains(citations, index, set(ignore_domains or DEFAULT_IGNORE_DOMAINS))
    res.unmatched_terms = candidates_from_text(text or "", index, set(ignore_terms or []))
    res.refusal = not mentions and is_refusal(text or "", refusal_patterns or [])
    return res


def candidates_from_domains(citations: Iterable[Citation], index: FirmIndex, ignore_domains: Iterable[str]) -> list[str]:
    ignore = {d.lower().removeprefix("www.") for d in ignore_domains}
    out: list[str] = []
    for c in citations:
        d = (c.domain or "").lower().removeprefix("www.")
        if not d or d in out or index.is_firm_domain(d):
            continue
        if any(d == i or d.endswith("." + i) for i in ignore):
            continue
        out.append(d)
    return out


def _covered_by_terms(norm: str, ignore_norms: list[str]) -> bool:
    """True when the candidate is made only of ignored terms ("Portugal", "Golden Visa", "Portugal Golden Visa"); a firm name that
    merely CONTAINS a place name ("PwC Portugal") is not covered and reaches the queue."""
    if not norm:
        return True
    tokens = [t for t in norm.split() if t != "s"]         # "Portugal's IFICI" → possessive "s" is not a name token
    i = 0
    while i < len(tokens):
        step = 0
        for term in ignore_norms:
            tt = term.split()
            if tt and tokens[i:i + len(tt)] == tt and len(tt) > step:
                step = len(tt)
        if step == 0:
            return False
        i += step
    return True


def _single_token_ok(tok: str, line: str) -> bool:
    """A single capitalised token is a candidate only with extra evidence: acronym (LACA), CamelCase (OnCorporate),
    a domain-like token (TaxAccountant.pt), a bold span, or the lead of a markdown list item."""
    bare = tok.strip("*")
    if _ALLCAPS.match(bare) or _CAMEL.match(bare) or re.search(r"\.[a-z]{2,4}$", bare):
        return True
    if f"**{bare}**" in line or f"*{bare}*" in line:
        return True
    m = _LIST_LEAD.match(line)
    return bool(m and m.group(1) == bare)


def candidates_from_text(text: str, index: FirmIndex, ignore_terms: Iterable[str]) -> list[str]:
    """Capitalised 1–5-word names (connectors allowed, sentence boundaries respected) that match no firm alias and are not made
    of ignored terms. Markdown headings and bold-only lines are skipped (they are section titles, not firms)."""
    ignore_norms = sorted({normalize_text(t) for t in ignore_terms if t and normalize_text(t)}, key=len, reverse=True)
    out: list[str] = []
    for line in (text or "").splitlines():
        if _HEADING.match(line):
            continue
        for cand in _line_candidates(line):
            if cand in out:
                continue
            norm = normalize_text(cand)
            if not norm or index.contains_alias(norm) or _covered_by_terms(norm, ignore_norms):
                continue
            out.append(cand)
    return out


def _line_candidates(line: str) -> list[str]:
    clean = _APOS.sub("'", line)
    raw = re.findall(r"[\w'&.-]+|[^\w\s]", clean)
    tokens: list[str] = []
    ends: list[bool] = []                                     # True when a sentence ends after this token
    for t in raw:
        core = t.rstrip(".!?;:")
        if core and core != t and not re.search(r"\.[A-Za-z]$", core):   # "Advogados." → "Advogados" + sentence end; "S.L.P." kept
            tokens.append(core)
            ends.append(True)
        elif t in {".", "!", "?", ";", ":"} and ends:
            ends[-1] = True
        else:
            tokens.append(t)
            ends.append(False)
    out: list[str] = []
    i, n = 0, len(tokens)
    while i < n:
        if not _CAP.match(tokens[i]):
            i += 1
            continue
        seq = [tokens[i]]
        j = i + 1
        stop = ends[i]
        while j < n and not stop:
            tok = tokens[j]
            if _CAP.match(tok):
                seq.append(tok)
            elif tok.lower() in CONNECTORS and j + 1 < n and _CAP.match(tokens[j + 1]) and not ends[j]:
                seq.append(tok)
            elif tok in DASHES and j + 1 < n and _CAP.match(tokens[j + 1]) and tokens[j + 1].lower() in LEGAL_WORDS:
                seq.append(tok)
            else:
                break
            stop = ends[j]
            j += 1
        while seq and seq[0].lower().strip("*") in LEADING_STOPWORDS:
            seq.pop(0)
        while seq and (seq[-1].lower() in CONNECTORS or seq[-1] in DASHES or seq[-1].lower().strip("*") in LEADING_STOPWORDS):
            seq.pop()                                   # "… Law Firms If" → "… Law Firms"
        caps = [t for t in seq if _CAP.match(t)]
        if 2 <= len(caps) <= 5 or (len(caps) == 1 and len(seq) == 1 and _single_token_ok(seq[0], line)):
            cand = " ".join(t.strip("*") for t in seq).strip(" ,.;:*")
            if cand and cand not in out:
                out.append(cand)
        i = max(j, i + 1)
    return out


# ---------------------------------------------------------------- week-level extraction into the store

def firm_index_for(vertical: Vertical) -> FirmIndex:
    """The index the whole pipeline uses: derived variants blocked by the vertical's ignore terms (place names)."""
    return FirmIndex(vertical.firms, blocked_variants=vertical.extraction.get("unmatched_ignore_terms") or [])


def extract_week(store: Store, vertical: Vertical, iso_week: str) -> dict[str, Any]:
    rules = rules_version_for(vertical.firms)
    index = firm_index_for(vertical)
    settings = vertical.extraction
    ignore_domains = set(DEFAULT_IGNORE_DOMAINS) | set(settings.get("unmatched_ignore_domains") or [])
    ignore_terms = set(settings.get("unmatched_ignore_terms") or [])
    panel_by_name = {p.name: p for p in vertical.panels}
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for f in vertical.firms:
        store.upsert_brand(f.id, f.canonical, f.kind, f.parent, f.country, f.type, website_domain(f.website), f.own, iso_week)
    # prompt → panel: the config first (source of truth), the store's prompts table for prompts no longer in the config
    prompt_panel = store.prompt_panels()
    prompt_panel.update({p.id: panel.name for panel in vertical.panels for p in panel.prompts})
    n_runs = n_mentions = n_unmatched = 0
    for run in store.ok_runs_with_citations(iso_week):
        panel = panel_by_name.get(prompt_panel.get(run["prompt_id"], ""))
        patterns = panel.refusal_patterns if panel is not None else []
        cites = [Citation(position=p, url=u, title="", domain=d) for p, u, d in run["citations"]]
        res = extract_answer(run["answer_text"] or "", cites, index, refusal_patterns=patterns,
                             ignore_domains=ignore_domains, ignore_terms=ignore_terms)
        unmatched = [(d, "domain") for d in res.unmatched_domains] + [(t, "text") for t in res.unmatched_terms]
        store.replace_extraction(int(run["id"]), rules, [(m.brand_id, m.position, m.matched_alias) for m in res.mentions], unmatched, res.refusal, now)
        n_runs += 1
        n_mentions += len(res.mentions)
        n_unmatched += len(unmatched)
    purged = store.purge_unmatched_other_rules(iso_week, rules)      # the queue is a work list: only the current rules survive
    return {"iso_week": iso_week, "runs": n_runs, "mentions": n_mentions, "unmatched": n_unmatched, "rules_version": rules,
            "purged_unmatched": purged}
