"""Day planner (SPEC S-A): which (prompt, engine, location, run_idx) cells to run today, minus the cells already done.

S1 scope: one weekday = one run_idx, explicit run_idx lists for smoke tests, start dates and twist weeks
(SPEC §13), frozen-panel check only for the panels actually planned today, ok-row exclusion.
S2 adds the catch-up window and the budget guard with the cut order.
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
log = logging.getLogger("lab.planner")


class NotFrozenError(RuntimeError):
    pass


class PlanError(ValueError):
    """A filter names something that does not exist — a typo must not turn into a silent empty day."""


@dataclass
class PlannedRun:
    key: RunKey
    prompt: Prompt
    panel_name: str
    panel_sha: str
    engine_id: str
    location: Location | None
    options: dict[str, Any]

    def __eq__(self, other: object) -> bool:  # plans compare by key (tests, dedupe)
        return isinstance(other, PlannedRun) and self.key == other.key


def iso_week_of(day: dt.date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


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


def plan_day(vertical: Vertical, store: Store, day: dt.date, *, run_idx_list: list[int] | None = None,
             limit: int | None = None, engines: list[str] | None = None, locations: list[str] | None = None,
             panel_kinds: list[str] | None = None, search_override: bool | None = None,
             require_frozen: bool = False, persist: bool = True, ignore_schedule: bool = False,
             log: Any = None) -> list[PlannedRun]:
    _check_filters(vertical, engines, locations, panel_kinds)
    iso_week = iso_week_of(day)
    run_idxs = list(run_idx_list) if run_idx_list else [day.isoweekday()]
    panels = panels_due(vertical, day, panel_kinds, ignore_schedule=ignore_schedule)
    if require_frozen:
        assert_frozen(vertical, panels)

    enabled = {eid: spec for eid, spec in vertical.engines.engines.items() if spec.enabled and (engines is None or eid in engines)}
    now = _now_iso()
    if persist:
        for eid, spec in enabled.items():
            store.upsert_engine(eid, spec.name, spec.api, spec.model, spec.supports_location)

    done = store.ok_keys(iso_week)
    tunables = vertical.engines.tunables
    plan: list[PlannedRun] = []
    n_prompts = 0
    for panel in panels:
        if persist:
            entry = latest(vertical.config_dir, panel.name)
            store.upsert_panel(panel.name, panel.sha256, entry.date if entry else None, panel.kind, panel.vertical, now)
        panel_engines = {eid: s for eid, s in enabled.items() if panel.engines is None or eid in panel.engines}
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
                options: dict[str, Any] = {
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
                    for run_idx in run_idxs:
                        key = RunKey(iso_week, prompt.id, eid, loc_key, int(run_idx))
                        if key in done:
                            continue
                        plan.append(PlannedRun(key=key, prompt=prompt, panel_name=panel.name, panel_sha=panel.sha256,
                                               engine_id=eid, location=location, options=options))
    return plan


def estimate_cost(plan: list[PlannedRun], vertical: Vertical) -> float:
    """Rough projection from engines.yaml prices and the per-call token estimate (used by dry runs and, in S2, the guard)."""
    est = dict(vertical.engines.tunables.get("estimate_tokens") or {})
    est_in, est_out = int(est.get("input", 10000)), int(est.get("output", 1000))
    est_in_nosearch = int(est.get("input_no_search", 200))
    est_searches = float(vertical.engines.tunables.get("estimate_search_calls", 1.0))
    total = 0.0
    for pr in plan:
        spec = vertical.engines.engines[pr.engine_id]
        price = spec.price
        model = pr.options.get("model", spec.model)
        p_in = float((price.get("per_1m_input") or {}).get(model, 0.0))
        p_out = float((price.get("per_1m_output") or {}).get(model, 0.0))
        if pr.options.get("search", True):
            total += (est_in * p_in + est_out * p_out) / 1e6 + est_searches * float(price.get("per_search_call", 0.0))
        else:
            total += (est_in_nosearch * p_in + est_out * p_out) / 1e6
        per_request = price.get("per_request")
        if isinstance(per_request, dict):
            total += float(per_request.get(spec.options.get("queue", "standard"), 0.0))
    return round(total, 4)
