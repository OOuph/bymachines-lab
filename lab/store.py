"""SQLite store (SPEC §6). The database is the checkpoint: every stored run is durable, re-runs are idempotent.

Rule for `runs`: one row per (iso_week, prompt_id, engine_id, location, run_idx). An `ok` row is never
overwritten; an `error` row is replaced by a later success. Raw JSON is kept forever (S-K re-extraction).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, NamedTuple

from lab import CITATION_RULES_VERSION


class RunKey(NamedTuple):
    iso_week: str
    prompt_id: str
    engine_id: str
    location: str
    run_idx: int


@dataclass
class RunRecord:
    key: RunKey
    ts_utc: str
    status: str                      # ok | error
    raw_json: str | None
    answer_text: str | None
    cost_usd: float
    error: str | None
    panel_sha: str
    search: int                      # requested with search tool (1) or not (0)
    searched: int                    # a search actually ran (engine metadata)
    catch_up: int
    latency_ms: int
    model: str
    citations: list[tuple[int, str, str, str]] = field(default_factory=list)   # (position, url, domain, title)


SCHEMA = """
CREATE TABLE IF NOT EXISTS panels (
    name TEXT PRIMARY KEY, sha256 TEXT NOT NULL, frozen_at TEXT, kind TEXT, vertical TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS prompts (
    id TEXT PRIMARY KEY, panel_name TEXT NOT NULL, class TEXT NOT NULL, text TEXT NOT NULL,
    need TEXT, country TEXT, twin_of TEXT, locations_json TEXT
);
CREATE TABLE IF NOT EXISTS engines (
    id TEXT PRIMARY KEY, name TEXT, api TEXT, model TEXT, supports_location INTEGER, notes TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL, iso_week TEXT NOT NULL, prompt_id TEXT NOT NULL, engine_id TEXT NOT NULL,
    location TEXT NOT NULL, run_idx INTEGER NOT NULL, status TEXT NOT NULL,
    raw_json TEXT, answer_text TEXT, cost_usd REAL NOT NULL DEFAULT 0, error TEXT,
    panel_sha TEXT, search INTEGER NOT NULL DEFAULT 1, searched INTEGER NOT NULL DEFAULT 0,
    catch_up INTEGER NOT NULL DEFAULT 0, latency_ms INTEGER, model TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_runs_key ON runs (iso_week, prompt_id, engine_id, location, run_idx);
CREATE TABLE IF NOT EXISTS citations (
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    position INTEGER NOT NULL, url TEXT NOT NULL, domain TEXT NOT NULL, title TEXT,
    source_type TEXT, rules_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_citations_run ON citations (run_id);
"""

RUN_COLUMNS = ("ts_utc", "iso_week", "prompt_id", "engine_id", "location", "run_idx", "status", "raw_json",
               "answer_text", "cost_usd", "error", "panel_sha", "search", "searched", "catch_up", "latency_ms", "model")


class Store:
    def __init__(self, path: str | Path = "data/lab.sqlite"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 5000")   # a second reader/writer waits instead of failing at once
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self._lock = threading.Lock()
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---- reference rows ----------------------------------------------------------------------------------------

    def upsert_panel(self, name: str, sha256: str, frozen_at: str | None, kind: str, vertical: str, now: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO panels (name, sha256, frozen_at, kind, vertical, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET sha256=excluded.sha256, frozen_at=excluded.frozen_at, kind=excluded.kind, "
                "vertical=excluded.vertical, updated_at=excluded.updated_at",
                (name, sha256, frozen_at, kind, vertical, now))
            self.conn.commit()

    def upsert_prompt(self, pid: str, panel_name: str, cls: str, text: str, need: str | None, country: str | None,
                      twin_of: str | None, locations: list[str]) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO prompts (id, panel_name, class, text, need, country, twin_of, locations_json) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET panel_name=excluded.panel_name, class=excluded.class, text=excluded.text, "
                "need=excluded.need, country=excluded.country, twin_of=excluded.twin_of, locations_json=excluded.locations_json",
                (pid, panel_name, cls, text, need, country, twin_of, json.dumps(locations)))
            self.conn.commit()

    def upsert_engine(self, eid: str, name: str, api: str, model: str, supports_location: bool, notes: str = "") -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO engines (id, name, api, model, supports_location, notes) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, api=excluded.api, model=excluded.model, "
                "supports_location=excluded.supports_location, notes=excluded.notes",
                (eid, name, api, model, int(supports_location), notes))
            self.conn.commit()

    # ---- runs ----------------------------------------------------------------------------------------------------

    def save_run(self, rec: RunRecord) -> str:
        """Insert a new row, update an existing error row, or skip when an ok row exists. Returns the action."""
        k = rec.key
        values = (rec.ts_utc, k.iso_week, k.prompt_id, k.engine_id, k.location, k.run_idx, rec.status, rec.raw_json,
                  rec.answer_text, float(rec.cost_usd), rec.error, rec.panel_sha, int(rec.search), int(rec.searched),
                  int(rec.catch_up), rec.latency_ms, rec.model)
        with self._lock:
            row = self.conn.execute(
                "SELECT id, status FROM runs WHERE iso_week=? AND prompt_id=? AND engine_id=? AND location=? AND run_idx=?",
                (k.iso_week, k.prompt_id, k.engine_id, k.location, k.run_idx)).fetchone()
            if row is None:
                cur = self.conn.execute(
                    f"INSERT INTO runs ({', '.join(RUN_COLUMNS)}) VALUES ({', '.join('?' * len(RUN_COLUMNS))})", values)
                run_id = int(cur.lastrowid)
                self._insert_citations(run_id, rec.citations)
                self.conn.commit()
                return "inserted"
            if row["status"] == "ok":
                return "skipped"
            run_id = int(row["id"])
            sets = ", ".join(f"{c}=?" for c in RUN_COLUMNS)
            self.conn.execute(f"UPDATE runs SET {sets} WHERE id=?", (*values, run_id))
            self.conn.execute("DELETE FROM citations WHERE run_id=?", (run_id,))
            self._insert_citations(run_id, rec.citations)
            self.conn.commit()
            return "updated"

    def _insert_citations(self, run_id: int, citations: Iterable[tuple[int, str, str, str]]) -> None:
        self.conn.executemany(
            "INSERT INTO citations (run_id, position, url, domain, title, source_type, rules_version) VALUES (?,?,?,?,?,NULL,?)",
            [(run_id, pos, url, domain, title, CITATION_RULES_VERSION) for pos, url, domain, title in citations])

    def ok_keys(self, iso_week: str) -> set[RunKey]:
        rows = self.conn.execute(
            "SELECT iso_week, prompt_id, engine_id, location, run_idx FROM runs WHERE iso_week=? AND status='ok'", (iso_week,))
        return {RunKey(r["iso_week"], r["prompt_id"], r["engine_id"], r["location"], int(r["run_idx"])) for r in rows}

    def iter_runs(self, iso_week: str | None = None) -> Iterator[sqlite3.Row]:
        if iso_week is None:
            yield from self.conn.execute("SELECT * FROM runs ORDER BY iso_week, prompt_id, engine_id, location, run_idx")
        else:
            yield from self.conn.execute(
                "SELECT * FROM runs WHERE iso_week=? ORDER BY prompt_id, engine_id, location, run_idx", (iso_week,))

    def citations_for(self, key: RunKey) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT c.* FROM citations c JOIN runs r ON r.id = c.run_id WHERE r.iso_week=? AND r.prompt_id=? AND r.engine_id=? "
            "AND r.location=? AND r.run_idx=? ORDER BY c.position", key))

    def summary(self, iso_week: str | None = None) -> list[dict[str, Any]]:
        sql = ("SELECT iso_week, engine_id, status, COUNT(*) AS n, ROUND(SUM(cost_usd), 6) AS cost_usd, "
               "SUM(CASE WHEN searched THEN 1 ELSE 0 END) AS n_searched FROM runs ")
        args: tuple = ()
        if iso_week is not None:
            sql += "WHERE iso_week=? "
            args = (iso_week,)
        sql += "GROUP BY iso_week, engine_id, status ORDER BY iso_week, engine_id, status"
        return [dict(r) for r in self.conn.execute(sql, args)]

    def weeks(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT DISTINCT iso_week FROM runs ORDER BY iso_week")]

    def n_citations_by_run(self, iso_week: str) -> dict[int, int]:
        rows = self.conn.execute(
            "SELECT r.id, COUNT(c.rowid) AS n FROM runs r LEFT JOIN citations c ON c.run_id = r.id WHERE r.iso_week=? GROUP BY r.id",
            (iso_week,))
        return {int(r["id"]): int(r["n"]) for r in rows}
