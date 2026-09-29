"""Weekly export (SPEC S-C, §6 schema): frequencies with Wilson intervals, source shares, stability, our citation, costs,
agent_single — CSV files plus one JSON with `schema_version`. Also the raw `runs.csv` dump from S1."""

from __future__ import annotations

import csv
import datetime as dt
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from lab.config import Vertical
from lab.extract import DEFAULT_SOURCE_TYPES, firm_index_for, rules_version_for
from lab.metrics import agent_single_cell, jaccard, source_shares, source_type, wilson
from lab.planner import previous_iso_week
from lab.store import Store

SCHEMA_VERSION = 2            # 2 (2026-09-29): `class` in the frequency cell key; parent-credited entities have no rows
OUR_DOMAINS = {"bymachines.ai"}
AGENT_KINDS = {"agent", "twist"}

COLUMNS = {
    "brand_frequency": ["brand", "class", "engine", "location", "n_runs", "n_mentioned", "freq", "wilson_low", "wilson_high"],
    "own_frequency": ["brand", "class", "engine", "location", "n_runs", "n_mentioned", "freq", "wilson_low", "wilson_high"],
    "sources": ["engine", "domain", "source_type", "share"],
    "stability": ["engine", "jaccard_vs_prev_week"],
    "our_citation": ["prompt_id", "engine", "n_runs", "n_cited"],
    "costs": ["engine", "calls", "cost_usd"],
    "agent_single": ["iso_week", "prompt_id", "engine", "n_runs", "n_single", "n_refusal", "n_unmatched", "slot_firm", "slot_share"],
}
RUNS_COLUMNS = ["iso_week", "prompt_id", "engine_id", "location", "run_idx", "ts_utc", "status", "model", "search", "searched",
                "catch_up", "cost_usd", "n_citations", "latency_ms", "answer_chars", "error"]


def _fmt(v: Any) -> Any:
    return f"{v:.6f}" if isinstance(v, float) else v


def _write_csv(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns)
        w.writeheader()
        for r in rows:
            w.writerow({c: _fmt(r.get(c, "")) for c in columns})


def write_runs_csv(store: Store, iso_week: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    n_cit = store.n_citations_by_run(iso_week)
    path = out_dir / "runs.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(RUNS_COLUMNS)
        for r in store.iter_runs(iso_week):
            w.writerow([r["iso_week"], r["prompt_id"], r["engine_id"], r["location"], r["run_idx"], r["ts_utc"], r["status"],
                        r["model"], r["search"], r["searched"], r["catch_up"], f"{r['cost_usd']:.6f}", n_cit.get(int(r["id"]), 0),
                        r["latency_ms"], len(r["answer_text"] or ""), r["error"] or ""])
    return path


def export_week(store: Store, vertical: Vertical, iso_week: str, out_dir: Path | str, rules_version: str | None = None) -> list[Path]:
    out_dir = Path(out_dir)
    rules = rules_version or rules_version_for(vertical.firms)
    n_ext, n_ok = store.extraction_coverage(iso_week, rules)
    if n_ok == 0:
        raise RuntimeError(f"no ok runs for {iso_week}: nothing to export")
    if n_ext < n_ok:
        raise RuntimeError(f"extraction of {iso_week} is incomplete under rules {rules}: {n_ext} of {n_ok} ok runs extracted — "
                           f"run `lab extract --week {iso_week}` first (runs stored after the last extraction have no mentions yet)")
    index = firm_index_for(vertical)
    # prompt → (class, publish, panel kind): the config first; the store's prompts table for prompts that left the config
    # (a twist file re-frozen for the next week) — those are never published
    prompt_meta: dict[str, tuple[str, bool, str]] = {pid: (cls, False, kind) for pid, (cls, kind) in store.prompt_meta().items()}
    for panel in vertical.panels:
        for p in panel.prompts:
            prompt_meta[p.id] = (p.cls, panel.classes[p.cls].publish, panel.kind)
    ids = {f.id for f in vertical.firms}
    own_ids = {f.id for f in vertical.firms if f.own}
    # entities credited to a parent (persons, products) never carry a mention under their own id → no rows for them
    published_ids = sorted(f.id for f in vertical.firms if not f.own and not (f.parent and f.parent in ids))

    runs = store.runs_for_export(iso_week)                            # all rows: id, prompt_id, engine_id, location, status, cost_usd
    ok_runs = [r for r in runs if r["status"] == "ok"]
    mentions = store.mentions_for_week(iso_week, rules)               # run_id → list of brand_id
    extractions = store.extractions_for_week(iso_week, rules)         # run_id → (n_mentions, refusal)
    citations = store.citations_for_week(iso_week)                    # run_id → list of (url, domain)

    # --- frequencies -------------------------------------------------------------------------------------------------
    # cell = class × engine × location: classes run on different location sets (provider: 5 cities, problem: 1), mixing
    # them would make "lisbon" incomparable with "madrid"
    def frequencies(brand_ids: list[str], eligible: list[dict[str, Any]]) -> list[dict[str, Any]]:
        cells: dict[tuple[str, str, str], list[int]] = defaultdict(list)
        for r in eligible:
            cls = prompt_meta.get(r["prompt_id"], ("", False, ""))[0]
            cells[(cls, r["engine_id"], r["location"])].append(int(r["id"]))
        rows = []
        for (cls, engine, location), run_ids in sorted(cells.items()):
            for brand in brand_ids:
                n_mentioned = sum(1 for rid in run_ids if brand in mentions.get(rid, []))
                low, high = wilson(n_mentioned, len(run_ids))
                rows.append({"brand": brand, "class": cls, "engine": engine, "location": location, "n_runs": len(run_ids),
                             "n_mentioned": n_mentioned, "freq": n_mentioned / len(run_ids) if run_ids else 0.0,
                             "wilson_low": low, "wilson_high": high})
        return rows

    published_runs = [r for r in ok_runs if prompt_meta.get(r["prompt_id"], ("", False, ""))[1]]
    brand_frequency = frequencies(published_ids, published_runs)
    own_frequency = frequencies(sorted(own_ids), ok_runs)

    # --- sources -----------------------------------------------------------------------------------------------------
    rules_types = {**DEFAULT_SOURCE_TYPES, **{k: list(v) for k, v in (vertical.extraction.get("source_types") or {}).items()}}
    sources: list[dict[str, Any]] = []
    for engine in sorted({r["engine_id"] for r in ok_runs}):
        counts: Counter[str] = Counter()
        sample_url: dict[str, str] = {}
        for r in ok_runs:
            if r["engine_id"] != engine:
                continue
            for url, domain in citations.get(int(r["id"]), []):
                d = (domain or "").lower().removeprefix("www.")
                if not d:
                    continue
                counts[d] += 1
                sample_url.setdefault(d, url)
        for d, share in sorted(source_shares(counts).items(), key=lambda kv: (-kv[1], kv[0])):
            sources.append({"engine": engine, "domain": d, "source_type": source_type(sample_url[d], d, index.firm_domains, rules_types), "share": share})

    # --- stability ---------------------------------------------------------------------------------------------------
    # blank when the previous week has no complete extraction under these rules, or when the engine had no ok runs then
    prev_week = previous_iso_week(iso_week)
    p_ext, p_ok = store.extraction_coverage(prev_week, rules)
    prev_complete = p_ok > 0 and p_ext == p_ok
    prev_mentions = store.mentions_for_week(prev_week, rules) if prev_complete else {}
    prev_engines = {r["engine_id"] for r in store.runs_for_export(prev_week) if r["status"] == "ok"} if prev_complete else set()
    prev_runs = {int(r["id"]): r["engine_id"] for r in store.runs_for_export(prev_week)} if prev_complete else {}
    stability: list[dict[str, Any]] = []
    for engine in sorted({r["engine_id"] for r in ok_runs}):
        this = {b for r in ok_runs if r["engine_id"] == engine for b in mentions.get(int(r["id"]), [])}
        if engine not in prev_engines:
            stability.append({"engine": engine, "jaccard_vs_prev_week": ""})
            continue
        prev = {b for rid, eng in prev_runs.items() if eng == engine for b in prev_mentions.get(rid, [])}
        stability.append({"engine": engine, "jaccard_vs_prev_week": jaccard(this, prev)})

    # --- our citation ------------------------------------------------------------------------------------------------
    our: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    for r in ok_runs:
        key = (r["prompt_id"], r["engine_id"])
        our[key][0] += 1
        if any((d or "").lower().removeprefix("www.") in OUR_DOMAINS for _, d in citations.get(int(r["id"]), [])):
            our[key][1] += 1
    our_citation = [{"prompt_id": p, "engine": e, "n_runs": n, "n_cited": c} for (p, e), (n, c) in sorted(our.items())]

    # --- costs -------------------------------------------------------------------------------------------------------
    cost_rows: dict[str, list[float]] = defaultdict(lambda: [0, 0.0])
    for r in runs:
        cost_rows[r["engine_id"]][0] += 1
        cost_rows[r["engine_id"]][1] += float(r["cost_usd"] or 0.0)
    costs = [{"engine": e, "calls": int(n), "cost_usd": round(c, 6)} for e, (n, c) in sorted(cost_rows.items())]
    for row in costs:
        store.upsert_cost(iso_week, row["engine"], row["calls"], row["cost_usd"])

    # --- agent_single ------------------------------------------------------------------------------------------------
    agent_cells: dict[tuple[str, str], list[tuple[int, bool, list[str]]]] = defaultdict(list)
    for r in ok_runs:
        meta = prompt_meta.get(r["prompt_id"])
        if meta is None or meta[2] not in AGENT_KINDS:
            continue
        n_m, refusal = extractions.get(int(r["id"]), (0, False))
        agent_cells[(r["prompt_id"], r["engine_id"])].append((n_m, bool(refusal), sorted(set(mentions.get(int(r["id"]), [])))))
    agent_single = []
    for (prompt_id, engine), cell_runs in sorted(agent_cells.items()):
        cell = agent_single_cell(cell_runs)
        agent_single.append({"iso_week": iso_week, "prompt_id": prompt_id, "engine": engine, **cell})

    # --- write -------------------------------------------------------------------------------------------------------
    out_dir.mkdir(parents=True, exist_ok=True)
    tables = {"brand_frequency": brand_frequency, "own_frequency": own_frequency, "sources": sources, "stability": stability,
              "our_citation": our_citation, "costs": costs, "agent_single": agent_single}
    paths: list[Path] = []
    for name, rows in tables.items():
        path = out_dir / f"{name}.csv"
        _write_csv(path, COLUMNS[name], rows)
        paths.append(path)
    brands = [{"id": f.id, "canonical": f.canonical, "kind": f.kind, "parent": f.parent, "country": f.country, "type": f.type, "own": f.own}
              for f in vertical.firms]
    # prompt metadata lets a consumer (the site) keep only rows of published classes without reading the panel files
    prompts = [{"id": pid, "class": cls, "kind": kind, "publish": publish} for pid, (cls, publish, kind) in sorted(prompt_meta.items())]
    payload = {"schema_version": SCHEMA_VERSION, "iso_week": iso_week, "vertical": vertical.name, "rules_version": rules,
               "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "brands": brands, "prompts": prompts,
               "tables": tables}
    jpath = out_dir / "export.json"
    jpath.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    paths.append(jpath)
    paths.append(write_runs_csv(store, iso_week, out_dir))
    return paths
