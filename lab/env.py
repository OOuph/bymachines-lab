"""Environment and logging: `.env` loading without extra dependencies, timestamped logs to stdout and a daily file."""

from __future__ import annotations

import datetime as dt
import logging
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _parse_value(raw: str) -> str:
    v = raw.strip()
    if not v:
        return ""
    if v[0] in "\"'":                           # quoted: take the quoted part, ignore anything after the closing quote
        q = v[0]
        end = v.find(q, 1)
        return v[1:end] if end > 0 else v[1:]
    if " #" in v:                               # unquoted: strip an inline comment (a '#' inside a token is kept)
        v = v.split(" #", 1)[0]
    return v.strip()


def load_dotenv(path: Path | None = None) -> Path | None:
    """Load KEY=VALUE lines from .env (cwd first, then the repo root) into os.environ.

    Supports `export KEY=VALUE`, quotes and inline comments. A value already set in the environment wins,
    except an empty exported value, which must not shadow the file.
    """
    candidates = [path] if path else [Path.cwd() / ".env", REPO_ROOT / ".env"]
    for p in candidates:
        if p and p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].lstrip()
                k, v = line.split("=", 1)
                k, v = k.strip(), _parse_value(v)
                if k and not os.environ.get(k):
                    os.environ[k] = v
            return p
    return None


def data_dir() -> Path:
    return Path(os.environ.get("LAB_DATA_DIR", "data"))


class _UTCFormatter(logging.Formatter):
    converter = staticmethod(lambda ts: dt.datetime.fromtimestamp(ts, dt.timezone.utc).timetuple())


def setup_logging(base: Path | None = None, level: int = logging.INFO) -> Path:
    base = base or data_dir()
    logs = base / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    logfile = logs / f"{dt.datetime.now(dt.timezone.utc):%Y-%m-%d}.log"
    fmt = _UTCFormatter("%(asctime)sZ %(levelname)s %(name)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    for handler in (logging.StreamHandler(sys.stdout), logging.FileHandler(logfile, encoding="utf-8")):
        handler.setFormatter(fmt)
        root.addHandler(handler)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return logfile
