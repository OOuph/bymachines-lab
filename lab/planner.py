"""Day planner (SPEC S-A, S-G): which (prompt, engine, location, run_idx) cells to run today, minus the cells already
done; the catch-up window for a missed day; the budget guard with the fixed cut order.

One weekday = one run_idx. Explicit run_idx lists (smoke tests) disable the catch-up window. Start dates and twist
weeks (SPEC §13) decide which panels are due; the freeze check applies only to the panels due today.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any

from lab.config import Location, Panel, Prompt, Vertical
from lab.freeze import FreezeState, check, latest
from lab.store import RunKey, Store

NO_LOCATION = "n/a"
CUT_TWIST, CUT_PROVIDER_LOCATIONS, CUT_AGENT_CORE = "twist", "provider_locations_3", "agent_core"
log = logging.getLogger("lab.planner")


class NotFrozenError(RuntimeError):
    pass


class PlanError(ValueError):
    """A filter names something that does not exist — a typo must not turn into a silent empty day."""


class BudgetExceeded(RuntimeError):
    def __init__(self, cap: float, projected: float, spent: float, cuts: list[str]):
        super().__init__(f"projected weekly cost ${projected:.2f} exceeds the cap ${cap:.2f} after cuts {cuts} (spent so far ${spent:.2f})")
        self.cap, self.projected, self.spent, self.cuts = cap, projected, spent, cuts


@dataclass
class PlannedRun:
    key: RunKey
    prompt: Prompt
    panel_name: str
    panel_sha: str
    panel_kind: str
    engine_id: str
    location: Location | None
    options: dict[str, Any]

    def __eq__(self, other: object) -> bool:  # plans compare by key (tests, dedupe)
        return isinstance(other, PlannedRun) and self.key == other.key


def iso_week_of(day: dt.date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def previous_iso_week(iso_week: str) -> str:
    year, week = int(iso_week[:4]), int(iso_week[-2:])
    monday = dt.date.fromisocalendar(year, week, 1) - dt.timedelta(days=7)
    return iso_week_of(monday)


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def assert_frozen(vertical: Vertical, panels: list[Panel]) -> None:
    problems = []
    for panel in panels:
        state, _ = check(panel.path, vertical.config_dir)
        if state is not FreezeState.OK:
            problems.append(f"{panel.name}: {state.value}")
    if problems:
        raise NotFrozenError("panels not frozen — run `lab freeze --panel <file>` or use --allow-unfrozen with a non-default --db: "
                             + "; ".join(problems))


def panels_due(vertical: Vertical, day: dt.date, panel_kinds: list[str] | None = None, *, ignore_schedule: bool = False) -> list[Panel]:
    """Panels that are scheduled on `day`: kind filter, start_date reached, twist bound to this ISO week.

    `ignore_schedule` is the draft/smoke mode (`--allow-unfrozen`): every panel of the requested kinds is due.
    """
    iso_week = iso_week_of(day)
    due: list[Panel] = []
    for panel in vertical.panels:
        if panel_kinds is not None and panel.kind not in panel_kinds:
            continue
        if ignore_schedule:
            due.append(panel)
            continue
        if panel.start_date is not None and day < panel.start_date:
            log.info("skip %s: starts %s, today is %s", panel.name, panel.start_date, day)
            continue
        if panel.kind == "twist" and panel.iso_week and panel.iso_week != iso_week:
            log.info("skip twist %s: bound to %s, today is %s", panel.name, panel.iso_week, iso_week)
            continue
        due.append(panel)
    return due


def _check_filters(vertical: Vertical, engines: list[str] | None, locations: list[str] | None, panel_kinds: list[str] | None) -> None:
    unknown = []
    for eid in engines or []:
        if eid not in vertical.engines.engines:
            unknown.append(f"engine '{eid}'")
    for loc in locations or []:
        if loc not in vertical.locations:
            unknown.append(f"location '{loc}'")
    kinds = {p.kind for p in vertical.panels}
    for k in panel_kinds or []:
        if k not in kinds:
            unknown.append(f"panel kind '{k}'")
    if unknown:
        raise PlanError("unknown filter value(s): " + ", ".join(unknown))


def run_indices_for(day: dt.date, catch_up_days: int) -> list[int]:
    """Today's weekday plus up to `catch_up_days` earlier weekdays of the same ISO week (never the previous week)."""
    today = day.isoweekday()
    first = max(1, today - max(0, int(catch_up_days)))
    return list(range(first, today + 1))


def plan_day(vertical: Vertical, store: Store, day: dt.date, *, run_idx_list: list[int] | None = None,
             limit: int | None = None, engines: list[str] | None = None, locations: list[str] | None = None,
             panel_kinds: list[str] | None = None, search_override: bool | None = None,
             require_frozen: bool = False, persist: bool = True, ignore_schedule: bool = False,
             log: Any = None) -> list[PlannedRun]:
    _check_filters(vertical, engines, locations, panel_kinds)
    iso_week = iso_week_of(day)
    tunables = vertical.engines.tunables
    today_idx = day.isoweekday()
    if run_idx_list:
        run_idxs = list(run_idx_list)
        catch_up_idxs: set[int] = set()
    else:
        run_idxs = run_indices_for(day, int(tunables.get("catch_up_days", 0) or 0))
        catch_up_idxs = {i for i in run_idxs if i != today_idx}
    panels = panels_due(vertical, day, panel_kinds, ignore_schedule=ignore_schedule)
    if require_frozen:
        assert_frozen(vertical, panels)

    enabled = {eid: spec for eid, spec in vertical.engines.engines.items() if spec.enabled and (engines is None or eid in engines)}
    now = _now_iso()
    if persist:
        for eid, spec in enabled.items():
            store.upsert_engine(eid, spec.name, spec.api, spec.model, spec.supports_location)

    done = store.ok_keys(iso_week)
    monday = day - dt.timedelta(days=today_idx - 1)
    plan: list[PlannedRun] = []
    n_prompts = 0
    for panel in panels:
        if persist:
            entry = latest(vertical.config_dir, panel.name)
            store.upsert_panel(panel.name, panel.sha256, entry.date if entry else None, panel.kind, panel.vertical, now)
        panel_engines = {eid: s for eid, s in enabled.items() if panel.engines is None or eid in panel.engines}
        # the catch-up window never reaches before the panel's start date (first partial week: Thu start → run_idx 4..7)
        panel_run_idxs = [i for i in run_idxs if ignore_schedule or panel.start_date is None
                          or monday + dt.timedelta(days=i - 1) >= panel.start_date]
        for prompt in panel.prompts:
            if limit is not None and n_prompts >= limit:
                break
            n_prompts += 1
            cls = panel.classes[prompt.cls]
            if persist:
                store.upsert_prompt(prompt.id, panel.name, prompt.cls, prompt.text, prompt.need, prompt.country, prompt.twin_of, cls.locations)
            for eid, spec in panel_engines.items():
                eng_opts = dict(panel.engine_options.get(eid) or {})
                search = search_override if search_override is not None else bool(eng_opts.get("search", True))
                base_options: dict[str, Any] = {
                    **eng_opts, "search": search,
                    "model": eng_opts.get("model") or spec.model_for_class(prompt.cls),
                    "search_context_size": eng_opts.get("search_context_size") or spec.options.get("search_context_size")
                    or tunables.get("default_search_context_size", "medium"),
                }
                if spec.supports_location:
                    loc_keys = [k for k in cls.locations if locations is None or k in locations]
                else:
                    loc_keys = [NO_LOCATION]
                for loc_key in loc_keys:
                    location = vertical.locations.get(loc_key) if loc_key != NO_LOCATION else None
                    for run_idx in panel_run_idxs:
                        key = RunKey(iso_week, prompt.id, eid, loc_key, int(run_idx))
                        if key in done:
                            continue
                        options = {**base_options, "catch_up": 1 if run_idx in catch_up_idxs else 0}
                        plan.append(PlannedRun(key=key, prompt=prompt, panel_name=panel.name, panel_sha=panel.sha256,
                                               panel_kind=panel.kind, engine_id=eid, location=location, options=options))
    return plan


def estimate_cost(plan: list[PlannedRun], vertical: Vertical, store: Store | None = None, month: str | None = None) -> float:
    """Rough projection from engines.yaml prices and the per-call estimates (dry runs and the budget guard).

    With `store` and `month` ("YYYY-MM") the projection also includes per-query search fees beyond an engine's monthly
    free quota (Gemini), from the month's counter in the database.
    """
    global_est = dict(vertical.engines.tunables.get("estimate_tokens") or {})
    global_searches = float(vertical.engines.tunables.get("estimate_search_calls", 1.0))
    total = 0.0
    projected_queries: dict[str, float] = {}
    for pr in plan:
        spec = vertical.engines.engines[pr.engine_id]
        price = spec.price
        # per-engine estimates (engines.yaml → <engine>.estimate_tokens / estimate_search_calls) override the global ones
        est = {**global_est, **(spec.options.get("estimate_tokens") or {})}
        est_in, est_out = int(est.get("input", 10000)), int(est.get("output", 1000))
        est_in_nosearch = int(est.get("input_no_search", 200))
        est_searches = float(spec.options.get("estimate_search_calls", global_searches))
        model = pr.options.get("model", spec.model)
        p_in = float((price.get("per_1m_input") or {}).get(model, 0.0))
        p_out = float((price.get("per_1m_output") or {}).get(model, 0.0))
        if pr.options.get("search", True):
            total += (est_in * p_in + est_out * p_out) / 1e6 + est_searches * float(price.get("per_search_call", 0.0))
            if price.get("per_search_query") and price.get("free_search_queries_per_month") is not None:
                projected_queries[pr.engine_id] = projected_queries.get(pr.engine_id, 0.0) + est_searches
        else:
            total += (est_in_nosearch * p_in + est_out * p_out) / 1e6
        per_request = price.get("per_request")
        if isinstance(per_request, dict):
            total += float(per_request.get(spec.options.get("queue", "standard"), 0.0))
    if store is not None and month is not None:
        for eid, queries in projected_queries.items():
            price = vertical.engines.engines[eid].price
            used = store.month_search_queries(eid, month)
            billable = max(0.0, used + queries - float(price["free_search_queries_per_month"]))
            total += min(queries, billable) * float(price["per_search_query"])
    return round(total, 4)


def _apply_cut(plan: list[PlannedRun], cut: str, vertical: Vertical) -> list[PlannedRun]:
    if cut == CUT_TWIST:
        return [p for p in plan if p.panel_kind != "twist"]
    if cut == CUT_PROVIDER_LOCATIONS:
        reduced = set(vertical.engines.tunables.get("provider_locations_reduced") or [])
        return [p for p in plan if not (p.prompt.cls == "provider" and p.key.location != NO_LOCATION and p.key.location not in reduced)]
    if cut == CUT_AGENT_CORE:
        return [p for p in plan if p.panel_kind != "agent"]
    raise PlanError(f"unknown budget cut '{cut}' in budget_cut_order")


def apply_budget_guard(plan: list[PlannedRun], vertical: Vertical, store: Store, iso_week: str,
                       month: str | None = None) -> tuple[list[PlannedRun], list[str]]:
    """SPEC S-G: spent so far + projection ≤ cap, else cut in the fixed order; raise when nothing is left to cut.

    An empty plan never raises (nothing would be spent). Only cuts that actually removed cells are reported.
    """
    if not plan:
        return [], []
    tunables = vertical.engines.tunables
    cap = float(tunables.get("weekly_budget_usd", 50))
    spent = store.week_cost(iso_week)
    kept = list(plan)
    if spent + estimate_cost(kept, vertical, store, month) <= cap + 1e-9:
        return kept, []
    cuts: list[str] = []
    for cut in tunables.get("budget_cut_order") or [CUT_TWIST, CUT_PROVIDER_LOCATIONS, CUT_AGENT_CORE]:
        before = len(kept)
        kept = _apply_cut(kept, str(cut), vertical)
        if len(kept) < before:
            cuts.append(str(cut))
        if spent + estimate_cost(kept, vertical, store, month) <= cap + 1e-9:
            return kept, cuts
    raise BudgetExceeded(cap, spent + estimate_cost(kept, vertical, store, month), spent, cuts)
