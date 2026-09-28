"""Freeze journal (SPEC S-J): `config/panel-hashes.txt`, append-only lines `<date> <sha256> <file>`.

A panel is "frozen" when its current sha256 equals the latest journal entry for its name. The runner refuses
anything else. Re-freezing after an edit appends a new line — the history of every change stays visible.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

JOURNAL_NAME = "panel-hashes.txt"


class FreezeJournalError(ValueError):
    """The journal file is malformed — refuse to guess which panels are frozen."""


class FreezeState(Enum):
    OK = "ok"
    NOT_FROZEN = "not frozen"
    MODIFIED = "modified after freeze"


@dataclass(frozen=True)
class JournalEntry:
    date: str
    sha: str
    name: str


def sha256_file(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def journal_path(config_dir: Path | str) -> Path:
    return Path(config_dir) / JOURNAL_NAME


def entry_name(path: Path | str, config_dir: Path | str) -> str:
    path, config_dir = Path(path), Path(config_dir)
    try:
        return path.resolve().relative_to(config_dir.resolve()).as_posix()
    except ValueError:
        return path.name


def read_journal(config_dir: Path | str) -> list[JournalEntry]:
    jp = journal_path(config_dir)
    if not jp.exists():
        return []
    out: list[JournalEntry] = []
    for line in jp.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(maxsplit=2)
        if len(parts) != 3 or len(parts[1]) != 64:
            raise FreezeJournalError(f"{jp}: malformed line: {line!r} (expected '<date> <sha256> <file>')")
        out.append(JournalEntry(*parts))
    return out


def latest(config_dir: Path | str, name: str) -> JournalEntry | None:
    entries = [e for e in read_journal(config_dir) if e.name == name]
    return entries[-1] if entries else None


def freeze(path: Path | str, config_dir: Path | str, today: dt.date | None = None) -> tuple[str, bool]:
    """Record the file's sha256. Returns (sha, appended). Idempotent: an unchanged file adds no line."""
    path, config_dir = Path(path), Path(config_dir)
    sha = sha256_file(path)
    name = entry_name(path, config_dir)
    last = latest(config_dir, name)
    if last is not None and last.sha == sha:
        return sha, False
    day = (today or dt.datetime.now(dt.timezone.utc).date()).isoformat()
    jp = journal_path(config_dir)
    jp.parent.mkdir(parents=True, exist_ok=True)
    with jp.open("a", encoding="utf-8") as fh:
        fh.write(f"{day} {sha} {name}\n")
    return sha, True


def check(path: Path | str, config_dir: Path | str) -> tuple[FreezeState, str | None]:
    path, config_dir = Path(path), Path(config_dir)
    last = latest(config_dir, entry_name(path, config_dir))
    if last is None:
        return FreezeState.NOT_FROZEN, None
    if last.sha == sha256_file(path):
        return FreezeState.OK, last.sha
    return FreezeState.MODIFIED, last.sha
