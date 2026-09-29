"""Operational journal (`data/journal.md`, append-only) and the budget guard's ALERT file (SPEC S-G, §8)."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

JOURNAL = "journal.md"
ALERT = "ALERT"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append(data_dir: Path | str, text: str) -> Path:
    p = Path(data_dir) / JOURNAL
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(f"{_now()} {' '.join(text.split())}\n")
    return p


def alert_path(data_dir: Path | str) -> Path:
    return Path(data_dir) / ALERT


def write_alert(data_dir: Path | str, message: str, details: dict[str, Any] | None = None) -> Path:
    p = alert_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    body = f"{_now()} {message}\n"
    if details:
        body += json.dumps(details, ensure_ascii=False, indent=2) + "\n"
    body += "Delete this file after reading to let the daily run resume.\n"
    p.write_text(body, encoding="utf-8")
    append(data_dir, f"ALERT written: {message}")
    return p


def clear_alert(data_dir: Path | str) -> bool:
    p = alert_path(data_dir)
    if p.exists():
        p.unlink()
        append(data_dir, "ALERT cleared")
        return True
    return False
