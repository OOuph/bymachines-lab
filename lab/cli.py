"""`lab` command line: validate, freeze, run, status, export (S1 subset of SPEC §6)."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import fcntl
import logging
import os
import signal
import sys
import threading
from collections import Counter
from pathlib import Path

from lab import __version__
from lab.config import ConfigError, load_vertical
from lab.engines import build_engine
from lab.engines.base import EngineError
from lab.env import data_dir, load_dotenv, setup_logging
from lab.freeze import FreezeJournalError, check, freeze, latest
from lab.journal import alert_path, append as journal, write_alert
from lab.planner import (BudgetExceeded, CUT_AGENT_CORE, NotFrozenError, PlanError, apply_budget_guard, estimate_cost,
                         iso_week_of, panels_due, plan_day, previous_iso_week)
from lab.runner import run_plan
from lab.store import Store

log = logging.getLogger("lab.cli")

EXIT_OK, EXIT_ERRORS, EXIT_USAGE, EXIT_REFUSED, EXIT_LOCKED, EXIT_BUDGET = 0, 1, 2, 3, 4, 5


def default_db() -> Path:
    return data_dir() / "lab.sqlite"


def _is_default_db(db: str | None) -> bool:
    return db is None or Path(db).resolve() == default_db().resolve()


def _vertical(args):
    try:
        return load_vertical(Path(args.config_dir), args.vertical)
    except (ConfigError, FreezeJournalError) as exc:
        print(f"CONFIG ERROR\n{exc}", file=sys.stderr)
        sys.exit(EXIT_USAGE)


def cmd_validate(args) -> int:
    load_dotenv()
    v = _vertical(args)
    print(f"vertical {v.name}: config OK ({args.config_dir})")
    for panel in v.panels:
        state, _ = check(panel.path, v.config_dir)
        counts = Counter(p.cls for p in panel.prompts)
        window = f" start={panel.start_date}" if panel.start_date else ""
        window += f" week={panel.iso_week}" if panel.iso_week else ""
        print(f"  {panel.name}: kind={panel.kind} prompts={len(panel.prompts)} {dict(counts)} freeze={state.value}{window} sha={panel.sha256[:12]}")
    print(f"  firms: {len(v.firms)} ({sum(1 for f in v.firms if f.own)} own) · locations: {', '.join(v.locations)}")
    enabled = [e for e in v.engines.engines.values() if e.enabled]
    keys = {e.id: all(os.environ.get(k) for k in e.env) for e in enabled}
    print("  engines enabled: " + ", ".join(
        f"{e.id}[{e.model}{'' if e.supports_location else ', no location'}{'' if keys[e.id] else ', NO KEY'}]" for e in enabled))
    per_engine: dict[str, int] = {}
    for panel in v.panels:
        if panel.kind == "twist":
            continue
        for e in enabled:
            if panel.engines is not None and e.id not in panel.engines:
                continue
            n = sum(len(panel.classes[p.cls].locations) if e.supports_location else 1 for p in panel.prompts)
            per_engine[e.id] = per_engine.get(e.id, 0) + n * panel.runs_per_week
    print(f"  calls/week (human+agent): {per_engine} total={sum(per_engine.values())}")
    return EXIT_OK


def cmd_freeze(args) -> int:
    target = Path(args.panel or args.file)
    config_dir = Path(args.config_dir)
    if not target.exists():
        print(f"no such file: {target}", file=sys.stderr)
        return EXIT_USAGE
    if args.panel and target.resolve().parent.name == "panels" and not target.resolve().is_relative_to(config_dir.resolve()):
        print(f"WARNING: {target} is not under --config-dir {config_dir}: the journal entry would use the bare file name "
              f"and the runner would not recognise it. Pass the matching --config-dir.", file=sys.stderr)
        return EXIT_USAGE
    try:
        previous = latest(config_dir, target.name if not target.resolve().is_relative_to(config_dir.resolve())
                          else target.resolve().relative_to(config_dir.resolve()).as_posix())
        sha, appended = freeze(target, config_dir)
    except FreezeJournalError as exc:
        print(f"JOURNAL ERROR: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if appended and previous is not None:
        print(f"WARNING: re-freezing {target} — previous entry {previous.date} {previous.sha[:12]} stays in the journal; "
              f"a frozen human/agent panel must not change after its freeze date (SPEC S-J)", file=sys.stderr)
    print(f"{'frozen' if appended else 'already frozen'} {target} sha256={sha} → {config_dir / 'panel-hashes.txt'}")
    return EXIT_OK


def _acquire_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "w")  # noqa: SIM115 — held for the process lifetime
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    fh.write(f"{os.getpid()} {dt.datetime.now(dt.timezone.utc):%Y-%m-%dT%H:%M:%SZ}\n")
    fh.flush()
    return fh


def cmd_run(args) -> int:
    load_dotenv()
    logfile = setup_logging(data_dir())
    v = _vertical(args)
    db_path = args.db or str(default_db())
    production = _is_default_db(args.db)
    if production:
        for flag, used in (("--allow-unfrozen", args.allow_unfrozen), ("--runs", args.runs is not None), ("--search", args.search != "auto")):
            if used:
                print(f"{flag} requires a non-default --db (the production database {default_db()} takes only frozen, "
                      f"scheduled, weekday-indexed runs)", file=sys.stderr)
                return EXIT_USAGE
    today = dt.datetime.now(dt.timezone.utc).date()
    day = dt.date.fromisoformat(args.date) if args.date else today
    if production and not args.dry_run and (day > today or iso_week_of(day) != iso_week_of(today)):
        print(f"--date {day} is outside the current ISO week ({iso_week_of(today)}) or in the future: the production database "
              f"never back-fills other weeks (SPEC §4); use a non-default --db for experiments", file=sys.stderr)
        return EXIT_USAGE
    run_idx_list = list(range(1, args.runs + 1)) if args.runs else None
    search_override = None if args.search == "auto" else (args.search == "on")

    lock = None
    if not args.dry_run:
        if alert_path(data_dir()).exists() and production:
            print(f"REFUSED: {alert_path(data_dir())} exists (budget stop) — read it, then delete it to resume (SPEC S-G)", file=sys.stderr)
            return EXIT_BUDGET
        lock = _acquire_lock(Path(db_path).with_suffix(".lock"))
        if lock is None:
            print(f"REFUSED: another `lab run` holds {Path(db_path).with_suffix('.lock')}", file=sys.stderr)
            return EXIT_LOCKED

    unknown = [e for e in (args.engine or []) if e not in v.engines.engines]
    if unknown:
        print(f"USAGE ERROR: unknown engine id(s) {', '.join(unknown)}; known: {', '.join(v.engines.engines)}", file=sys.stderr)
        return EXIT_USAGE

    # engines that can actually be called today: enabled, adapter built, keys present (SPEC §4 "skipped with a warning")
    available: list[str] = []
    engines = {}
    for eid, spec in v.engines.engines.items():
        if not spec.enabled or (args.engine and eid not in args.engine):
            continue
        if args.dry_run:
            available.append(eid)
            continue
        try:
            engine = build_engine(spec, v.engines.tunables)
        except EngineError as exc:
            log.warning("%s — engine skipped today", exc)
            continue
        if engine is None:
            log.warning("engine %s: adapter not available in this build — skipped today", eid)
            continue
        engines[eid] = engine
        available.append(eid)
    if not available:
        print("no engine can run: check the API keys in .env (see .env.example)", file=sys.stderr)
        return EXIT_USAGE

    # a dry run must leave no trace: plan against memory when the database does not exist yet
    store = Store(":memory:") if args.dry_run and not Path(db_path).exists() else Store(db_path)
    if args.allow_unfrozen:
        log.warning("draft mode (--allow-unfrozen): freeze journal and start dates ignored, writing to %s", db_path)
    try:
        plan = plan_day(v, store, day, run_idx_list=run_idx_list, limit=args.limit, engines=available,
                        locations=args.location or None, panel_kinds=args.panel or None, search_override=search_override,
                        require_frozen=not args.allow_unfrozen, persist=not args.dry_run,
                        ignore_schedule=args.allow_unfrozen, log=log)
    except PlanError as exc:
        print(f"USAGE ERROR: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except NotFrozenError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    iso_week = iso_week_of(day)
    month = today.strftime("%Y-%m")
    planned_before = len(plan)
    # ALERT and the journal belong to the production run only; a smoke run with --db must never block the timer
    record = production and not args.dry_run
    try:
        plan, cuts = apply_budget_guard(plan, v, store, iso_week, month)
    except PlanError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except BudgetExceeded as exc:
        msg = f"budget guard: {exc}"
        log.error(msg)
        if record:
            write_alert(data_dir(), "weekly budget cap exceeded — run stopped before any call",
                        details={"iso_week": iso_week, "cap": exc.cap, "spent": round(exc.spent, 4), "projected": round(exc.projected, 4),
                                 "cuts_tried": exc.cuts, "cells_planned": planned_before})
        print(f"REFUSED: {msg}", file=sys.stderr)
        return EXIT_BUDGET
    spent = store.week_cost(iso_week)
    est = estimate_cost(plan, v, store, month)
    for cut in cuts:
        line = f"budget cut {iso_week}: {cut} dropped (spent ${spent:.2f} + projected ${est:.2f} vs cap ${float(v.engines.tunables.get('weekly_budget_usd', 50)):.2f})"
        log.warning(line)
        if record:
            journal(data_dir(), line)
    if CUT_AGENT_CORE in cuts and record:
        agent_panels = [panel for panel in v.panels if panel.kind == "agent"]
        agent_ids = [p.id for panel in agent_panels for p in panel.prompts]
        prev = previous_iso_week(iso_week)
        prev_sunday = dt.date.fromisocalendar(int(prev[:4]), int(prev[-2:]), 7)
        agent_due_prev = any(panel.start_date is None or panel.start_date <= prev_sunday for panel in agent_panels)
        if agent_due_prev and store.count_ok(prev, agent_ids) == 0 and store.count_ok(iso_week, agent_ids) == 0:
            journal(data_dir(), f"agent experiment paused: no agent-core runs in {prev} and {iso_week} (budget)")
    due = [p.name for p in panels_due(v, day, args.panel or None, ignore_schedule=args.allow_unfrozen)]
    log.info("plan: day=%s iso_week=%s run_idx=%s panels=%s engines=%s cells=%d cuts=%s spent=$%.4f estimated=$%.4f db=%s log=%s",
             day, iso_week, sorted({p.key.run_idx for p in plan}) or [day.isoweekday()], due, available, len(plan), cuts, spent, est,
             db_path, logfile)
    if args.dry_run:
        for pr in plan:
            print(f"  {pr.key.iso_week} {pr.key.prompt_id:8} {pr.key.engine_id:10} {pr.key.location:9} run{pr.key.run_idx} "
                  f"model={pr.options['model']} search={pr.options['search']}{' catch_up' if pr.options.get('catch_up') else ''}")
        print(f"dry run: {len(plan)} cells, cuts={cuts or 'none'}, spent this week ${spent:.4f}, estimated ${est:.4f} (nothing written)")
        return EXIT_OK
    if not plan:
        print(f"nothing to do for {day}: panels due {due or 'none'}, every planned cell already has an ok row")
        return EXIT_OK
    stop = threading.Event()

    def _sig(signum, _frame):
        log.warning("signal %s received: finishing in-flight calls, starting nothing new", signum)
        stop.set()

    for s in (signal.SIGINT, signal.SIGTERM):
        signal.signal(s, _sig)
    stats = run_plan(plan, engines, store, concurrency=args.concurrency or int(v.engines.tunables.get("concurrency", 4)), stop_event=stop,
                     specs=v.engines.engines)
    print(f"inserted={stats.inserted} updated={stats.updated} skipped={stats.skipped} errors={stats.errors} "
          f"store_failures={stats.store_failures} cost=${stats.cost_usd:.4f}{' (stopped early)' if stats.stopped_early else ''}")
    return EXIT_ERRORS if (stats.errors or stats.store_failures) and not (stats.inserted or stats.updated) else EXIT_OK


def cmd_status(args) -> int:
    load_dotenv()
    db = args.db or str(default_db())
    store = Store(db)
    weeks = store.weeks()
    if not weeks:
        print(f"{db}: no runs yet")
        return EXIT_OK
    rows = store.summary(args.week)
    print(f"{'week':10} {'engine':11} {'status':7} {'n':>5} {'searched':>9} {'cost_usd':>10}")
    for r in rows:
        print(f"{r['iso_week']:10} {r['engine_id']:11} {r['status']:7} {r['n']:5d} {r['n_searched']:9d} {r['cost_usd']:10.4f}")
    total = sum(r["cost_usd"] or 0 for r in rows)
    print(f"weeks: {', '.join(weeks)} · total cost ${total:.4f} · db {db}")
    return EXIT_OK


def cmd_export(args) -> int:
    load_dotenv()
    store = Store(args.db or str(default_db()))
    week = args.week
    out_dir = Path(args.out or (data_dir() / "export" / week))
    out_dir.mkdir(parents=True, exist_ok=True)
    n_cit = store.n_citations_by_run(week)
    path = out_dir / "runs.csv"
    cols = ["iso_week", "prompt_id", "engine_id", "location", "run_idx", "ts_utc", "status", "model", "search", "searched",
            "catch_up", "cost_usd", "n_citations", "latency_ms", "answer_chars", "error"]
    n = 0
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in store.iter_runs(week):
            w.writerow([r["iso_week"], r["prompt_id"], r["engine_id"], r["location"], r["run_idx"], r["ts_utc"], r["status"],
                        r["model"], r["search"], r["searched"], r["catch_up"], f"{r['cost_usd']:.6f}", n_cit.get(int(r["id"]), 0),
                        r["latency_ms"], len(r["answer_text"] or ""), r["error"] or ""])
            n += 1
    print(f"wrote {path} ({n} rows)")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="lab", description=f"By Machines Lab {__version__}")
    ap.add_argument("--config-dir", default="config")
    ap.add_argument("--vertical", default="relocation-europe")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("validate", help="load and validate the vertical config").set_defaults(fn=cmd_validate)

    p = sub.add_parser("freeze", help="record a panel (or any file) sha256 in config/panel-hashes.txt")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--panel")
    g.add_argument("--file")
    p.set_defaults(fn=cmd_freeze)

    p = sub.add_parser("run", help="run today's cells (or an explicit smoke plan)")
    p.add_argument("--date", help="YYYY-MM-DD (UTC), default today")
    p.add_argument("--runs", type=int, help="explicit run_idx 1..N instead of today's weekday (smoke tests; needs --db)")
    p.add_argument("--limit", type=int, help="first N prompts only")
    p.add_argument("--engine", action="append", help="engine id (repeatable)")
    p.add_argument("--location", action="append", help="location key (repeatable)")
    p.add_argument("--panel", action="append", choices=["human", "agent", "twist"], help="panel kind (repeatable)")
    p.add_argument("--search", choices=["auto", "on", "off"], default="auto", help="override the search tool for every cell (needs --db)")
    p.add_argument("--db", help="SQLite path (default $LAB_DATA_DIR/lab.sqlite)")
    p.add_argument("--allow-unfrozen", action="store_true", help="run draft panels (requires a non-default --db)")
    p.add_argument("--dry-run", action="store_true", help="print the plan and the cost estimate; writes nothing")
    p.add_argument("--concurrency", type=int)
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("status", help="rows and cost per week/engine/status")
    p.add_argument("--db")
    p.add_argument("--week")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("export", help="S1: raw runs.csv for a week (S3 adds the metric exports)")
    p.add_argument("--week", required=True)
    p.add_argument("--db")
    p.add_argument("--out")
    p.add_argument("--raw", action="store_true", default=True)
    p.set_defaults(fn=cmd_export)

    args = ap.parse_args(argv)
    return int(args.fn(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
