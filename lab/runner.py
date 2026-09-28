"""Runner (SPEC S-A): execute a plan with bounded concurrency, store every result immediately, stop gracefully.

Engine calls run in worker threads; all SQLite writes happen in the calling thread as results arrive, so a crash
after any row leaves a consistent database and the next `lab run` plans only the missing cells.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, Mapping

from lab.engines.base import Answer, EngineError, EngineLike
from lab.planner import PlannedRun
from lab.store import RunRecord, Store

log = logging.getLogger("lab.runner")


@dataclass
class RunStats:
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    errors: int = 0
    store_failures: int = 0
    cost_usd: float = 0.0
    stopped_early: bool = False


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _record(pr: PlannedRun, answer: Answer | None, error: str | None, latency_ms: int,
            raw: dict[str, Any] | None = None, cost_usd: float = 0.0) -> RunRecord:
    if answer is None:
        # a paid-but-unusable response (incomplete, unparsable) keeps its raw JSON and cost — never lost (S-K)
        return RunRecord(key=pr.key, ts_utc=_now_iso(), status="error",
                         raw_json=json.dumps(raw, ensure_ascii=False) if raw is not None else None, answer_text=None,
                         cost_usd=float(cost_usd), error=error, panel_sha=pr.panel_sha,
                         search=int(bool(pr.options.get("search", True))), searched=0,
                         catch_up=int(pr.options.get("catch_up", 0)), latency_ms=latency_ms, model=str(pr.options.get("model", "")))
    return RunRecord(
        key=pr.key, ts_utc=_now_iso(), status="ok", raw_json=json.dumps(answer.raw, ensure_ascii=False),
        answer_text=answer.text, cost_usd=float(answer.cost_usd), error=None, panel_sha=pr.panel_sha,
        search=int(bool(pr.options.get("search", True))), searched=int(answer.searched),
        catch_up=int(pr.options.get("catch_up", 0)), latency_ms=int(answer.latency_ms or latency_ms), model=answer.model,
        citations=[(c.position, c.url, c.domain, c.title) for c in answer.citations],
    )


def _call(engine: EngineLike, pr: PlannedRun) -> tuple[PlannedRun, Answer | None, str | None, dict[str, Any] | None, float]:
    try:
        return pr, engine.ask(pr.prompt.text, pr.location, dict(pr.options)), None, None, 0.0
    except EngineError as exc:
        return pr, None, f"{type(exc).__name__}: {exc}", exc.raw, float(exc.cost_usd or 0.0)
    except Exception as exc:  # noqa: BLE001 — any adapter bug becomes an error row, never a crash of the day
        return pr, None, f"{type(exc).__name__}: {exc}", None, 0.0


def run_plan(plan: list[PlannedRun], engines: Mapping[str, EngineLike], store: Store, *, concurrency: int = 1,
             stop_event: threading.Event | None = None) -> RunStats:
    stats = RunStats()
    stop_event = stop_event or threading.Event()
    queue = list(plan)
    in_flight: set[Future] = set()
    started = dt.datetime.now(dt.timezone.utc)

    def store_result(fut: Future) -> None:
        pr, answer, error, raw, paid = fut.result()
        rec = _record(pr, answer, error, 0, raw=raw, cost_usd=paid)
        try:
            action = store.save_run(rec)
        except Exception as exc:  # noqa: BLE001 — one failed write must not discard the other in-flight answers
            stats.store_failures += 1
            log.error("store failure for %s/%s/%s/%s/%s: %s — the cell stays missing and is re-planned next run",
                      pr.key.iso_week, pr.key.prompt_id, pr.key.engine_id, pr.key.location, pr.key.run_idx, exc)
            return
        if rec.status != "ok":
            stats.errors += 1          # error rows are stored (for re-planning) but never counted as progress
        elif action == "inserted":
            stats.inserted += 1
            stats.cost_usd = round(stats.cost_usd + rec.cost_usd, 6)
        elif action == "updated":
            stats.updated += 1
            stats.cost_usd = round(stats.cost_usd + rec.cost_usd, 6)
        else:
            stats.skipped += 1
        log.info("run %s/%s/%s/%s/%s status=%s cost=%.4f citations=%d searched=%s latency_ms=%s%s",
                 pr.key.iso_week, pr.key.prompt_id, pr.key.engine_id, pr.key.location, pr.key.run_idx, rec.status,
                 rec.cost_usd, len(rec.citations), rec.searched, rec.latency_ms, f" error={error}" if error else "")

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        while queue or in_flight:
            while queue and len(in_flight) < max(1, concurrency) and not stop_event.is_set():
                pr = queue.pop(0)
                engine = engines.get(pr.engine_id)
                if engine is None:
                    store_result(_done(pr, None, f"engine {pr.engine_id} not configured (missing key or adapter)"))
                    continue
                in_flight.add(pool.submit(_call, engine, pr))
            if stop_event.is_set() and queue:
                log.warning("stop requested: %d planned runs left unstarted, %d in flight will finish", len(queue), len(in_flight))
                queue.clear()
                stats.stopped_early = True
            if not in_flight:
                continue
            finished, _ = wait(in_flight, timeout=1.0, return_when=FIRST_COMPLETED)
            for fut in finished:
                in_flight.discard(fut)
                store_result(fut)
    elapsed = (dt.datetime.now(dt.timezone.utc) - started).total_seconds()
    log.info("done: inserted=%d updated=%d skipped=%d errors=%d store_failures=%d cost=$%.4f elapsed=%.1fs",
             stats.inserted, stats.updated, stats.skipped, stats.errors, stats.store_failures, stats.cost_usd, elapsed)
    return stats


def _done(pr: PlannedRun, answer: Answer | None, error: str | None) -> Future:
    fut: Future = Future()
    fut.set_result((pr, answer, error, None, 0.0))
    return fut
