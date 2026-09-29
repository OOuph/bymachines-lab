"""Daily backup of the lab database from the VPS to the operator's Mac (SPEC §5; deploy/README.md §3–§4).

One run, from the operator's Mac (launchd starts it at SCHEDULE; a later start the same day exits at once when the day's
copy is complete):
 1. server: a one-step SQLite backup of data/lab.sqlite as user `lab` — one read transaction, so a consistent snapshot
    that writers cannot restart — into data/lab-backup.sqlite, switched to rollback-journal mode, `PRAGMA quick_check`;
    sha256, size, row counts and the panel service state are printed back. Refused when the disk lacks room for the copy;
 2. rsync into data/backups/incoming/ (an APFS clone of the last verified copy is the delta basis, so only changed blocks
    travel). sha256 must equal the server's, `PRAGMA integrity_check` must say ok, row counts must equal the server's, and
    `runs` / `runs_ok` must not be lower than in the last verified copy (the store never deletes a run) — only then does
    the copy replace data/backups/lab-latest.sqlite;
 3. a dated compressed copy data/backups/daily/lab-YYYY-MM-DD.sqlite.gz (UTC date), read back before it is published;
    older copies are pruned: every day for KEEP_DAILY days, the last copy of each of the last KEEP_WEEKLY ISO weeks and
    of each of the last KEEP_MONTHLY months (the current week and month included);
 4. the rest of the server's data dir (run logs, journal, exports, ALERT) is mirrored into data/backups/files/ — nothing
    is ever deleted there — and the server copy is removed;
 5. watchdogs that never fail the backup: no ok panel run for the expected day; DigitalOcean backups off, or the newest
    image older than DO_STALE_HOURS (read-only API calls, DIGITALOCEAN_TOKEN from .env).
The day counts as done only when the copy holds that day's panel run and the run was not in progress.
Failures and warnings raise a macOS notification; the launchd wrapper also notifies when the job cannot start at all.
Log: data/backups/backup.log (UTC timestamps). Exit codes: 0 ok · 3 failed (logged and notified) · 2 bad arguments.

Usage (repo root; standard library only, any Python >= 3.9):
    .venv/bin/python deploy/backup_to_mac.py                      # back up now (exits at once when the day is done)
    .venv/bin/python deploy/backup_to_mac.py --force              # back up again today
    .venv/bin/python deploy/backup_to_mac.py --accept-fewer-rows  # after an intentional restore of an older copy
    .venv/bin/python deploy/backup_to_mac.py --status             # last copy, last error, copies on disk, launchd agent
    .venv/bin/python deploy/backup_to_mac.py --install | --uninstall
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import gzip
import hashlib
import json
import logging
import os
import plistlib
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from lab.env import load_dotenv  # noqa: E402

# ---- parameters ----
HOST_FILE = REPO / "data" / "host.txt"             # droplet IP, written by deploy/provision_do.py
SSH_KEY = Path.home() / ".ssh" / "bymachines_lab_ed25519"
SSH_USER = "root"
APP_USER = "lab"                                    # owns the live database; it is never opened as root
REMOTE_DATA = "/opt/bymachines-lab/data"
REMOTE_DB = "lab.sqlite"
REMOTE_COPY = "lab-backup.sqlite"                   # exists on the server only while a backup runs
DISK_HEADROOM_PCT = 120                             # the server needs this % of the database size free for the copy
BACKUP_DIR = REPO / "data" / "backups"              # git-ignored with the rest of data/
LATEST = BACKUP_DIR / "lab-latest.sqlite"           # the last verified copy; open it read-only (deploy/README.md §4)
STAGING = BACKUP_DIR / "incoming" / LATEST.name     # rsync target; replaces LATEST only after every check
DAILY_DIR = BACKUP_DIR / "daily"
FILES_DIR = BACKUP_DIR / "files"
STATE_FILE = BACKUP_DIR / "state.json"
LOG_FILE = BACKUP_DIR / "backup.log"
LOCK_FILE = BACKUP_DIR / ".lock"
KEEP_DAILY, KEEP_WEEKLY, KEEP_MONTHLY = 7, 4, 12    # ~20 files in the steady state; the database grows ~9 MB a day
PANEL_DONE_HOUR_UTC = 8                             # the panel starts 06:00 UTC, normally done by 07:00; from 08:00 UTC the
                                                    # day's run must be in the copy, before that the previous day's
DO_STALE_HOURS = 30                                 # DigitalOcean daily plan: a new image about every 24 h
DO_FIRST_IMAGE_GRACE_HOURS = 36                     # after backups are switched on, the first image may take a day
STATUS_STALE_HOURS = 36                             # --status exits 1 when the last verified copy is older
DROPLET = "bymachines-lab"                          # droplet name and tag (deploy/provision_do.py)
DO_API = "https://api.digitalocean.com/v2"
LABEL = "ai.bymachines.lab-backup"
SCHEDULE = ((11, 30), (18, 30))                     # Mac local time, after the 06:00 UTC run; the second start is the retry
LAUNCHD_LOG = Path.home() / "Library" / "Logs" / f"{LABEL}.log"   # what escapes the logger: a job that cannot start
NOTIFY_TITLE = "By Machines lab backup"
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=15"]
TIMEOUT_S = 900                                     # per ssh / rsync call
EXIT_OK, EXIT_FAILED = 0, 3                         # 3 = failure already logged and notified
COUNT_QUERIES = {                                   # computed on the server's copy and on the local copy; must match
    "runs": "SELECT COUNT(*) FROM runs",
    "runs_ok": "SELECT COUNT(*) FROM runs WHERE status = 'ok'",
    "citations": "SELECT COUNT(*) FROM citations",
    "mentions": "SELECT COUNT(*) FROM mentions",
    "newest_ok_utc": "SELECT IFNULL(MAX(ts_utc), '') FROM runs WHERE status = 'ok'",
}
MONOTONIC = ("runs", "runs_ok")                     # lab/store.py never deletes a run and never overwrites an ok run
DATED = re.compile(r"^lab-(\d{4}-\d{2}-\d{2})\.sqlite\.gz$")
# launchd runs the job through this: exit 0 / 3 (handled) / 143 (stopped at shutdown) stay quiet, anything else — the
# interpreter is gone, an import fails, a crash before logging — becomes a notification
WRAPPER = ('"$0" "$1"; rc=$?; case $rc in 0|3|143) ;; *) "${LAB_BACKUP_OSASCRIPT:-/usr/bin/osascript}" -e '
           f'"display notification \\"the backup job did not run (exit $rc), see {LAUNCHD_LOG}\\" '
           f'with title \\"{NOTIFY_TITLE}\\"" ;; esac; exit $rc')

log = logging.getLogger("lab.backup")


class BackupError(RuntimeError):
    pass


# ---- pure helpers (tested in tests/test_backup_to_mac.py) --------------------------------------------------------

def keep_set(days: list[dt.date], keep_daily: int = KEEP_DAILY, keep_weekly: int = KEEP_WEEKLY,
             keep_monthly: int = KEEP_MONTHLY) -> set[dt.date]:
    """Days whose copies stay: the newest `keep_daily`, plus the newest copy of each of the newest `keep_weekly` ISO
    weeks and of each of the newest `keep_monthly` months. Gaps (the Mac asleep) only shift which copy represents a period."""
    newest_first = sorted(set(days), reverse=True)
    keep = set(newest_first[:keep_daily])
    for period, n in ((lambda d: tuple(d.isocalendar())[:2], keep_weekly), (lambda d: (d.year, d.month), keep_monthly)):
        chosen: dict = {}
        for d in newest_first:
            k = period(d)
            if k not in chosen:
                if len(chosen) == n:
                    break
                chosen[k] = d
        keep.update(chosen.values())
    return keep


def parse_remote(stdout: str) -> dict[str, str]:
    """`key=value` lines printed by the server script → dict; the fields the checks need must all be there."""
    out = {}
    for line in stdout.splitlines():
        k, sep, v = line.strip().partition("=")
        if sep:
            out[k] = v
    missing = [k for k in ("sha256", "size", *COUNT_QUERIES) if k not in out]
    if missing:
        raise BackupError(f"server output lacks {missing}: {stdout.strip()[-400:]!r}")
    if not re.fullmatch(r"[0-9a-f]{64}", out["sha256"]):
        raise BackupError(f"bad sha256 from the server: {out['sha256']!r}")
    return out


def run_in_progress(remote: dict[str, str]) -> bool:
    """The panel run was still going when the copy was made: the copy is partial."""
    return remote.get("service") in ("active", "activating", "deactivating", "reloading")


def done_date(remote: dict[str, str], counts: dict[str, str], today: str) -> str | None:
    """`today` when this copy completes the day — it holds the day's panel run and the run was not in progress; else None,
    so the next start repeats the copy."""
    if run_in_progress(remote) or not counts.get("newest_ok_utc", "").startswith(today):
        return None
    return today


def rows_dropped(previous: dict[str, str], counts: dict[str, str]) -> dict[str, tuple[int, int]]:
    """Monotonic counters that went down against the last verified copy: the server's database lost rows."""
    return {k: (int(previous[k]), int(counts[k])) for k in MONOTONIC
            if k in previous and k in counts and int(counts[k]) < int(previous[k])}


def lab_warning(newest_ok_utc: str, now: dt.datetime) -> str | None:
    """No ok panel run for the day that should already be in: today from PANEL_DONE_HOUR_UTC on, else yesterday."""
    if not newest_ok_utc:
        return "no ok panel run in the database"
    expected = now.date() if now.hour >= PANEL_DONE_HOUR_UTC else now.date() - dt.timedelta(days=1)
    if newest_ok_utc[:10] < expected.isoformat():
        return f"no ok panel run on {expected} (newest {newest_ok_utc}): the daily run was missed?"
    return None


def hours_since(ts_utc: str, now: dt.datetime) -> float:
    t = dt.datetime.fromisoformat(ts_utc.replace("Z", "+00:00"))
    return (now - t).total_seconds() / 3600


def already_done(state: dict, today: str) -> bool:
    return state.get("done_date") == today and (DAILY_DIR / f"lab-{today}.sqlite.gz").exists()


def plist_dict(python: str) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": ["/bin/sh", "-c", WRAPPER, python, str(Path(__file__).resolve())],
        "WorkingDirectory": str(REPO),
        "StartCalendarInterval": [{"Hour": h, "Minute": m} for h, m in SCHEDULE],
        "RunAtLoad": False,
        "ProcessType": "Background",
        "StandardOutPath": str(LAUNCHD_LOG),
        "StandardErrorPath": str(LAUNCHD_LOG),
        "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(Path.home()),
                                 "LAB_BACKUP_LAUNCHD": "1"},
    }


# ---- server side ---------------------------------------------------------------------------------------------------

def remote_script() -> str:
    counts_sql = " ".join(f"SELECT '{k}=' || ({q});" for k, q in COUNT_QUERIES.items())
    tmp = f"{REMOTE_COPY}.tmp"
    one_step_backup = "\n".join([
        "import sqlite3",
        f"src = sqlite3.connect({REMOTE_DB!r}, timeout=60)",
        f"dst = sqlite3.connect({tmp!r})",
        "src.backup(dst)                              # all pages in one step: writers cannot restart it",
        "dst.execute('PRAGMA journal_mode=DELETE')    # one self-contained file, opens anywhere without -wal/-shm",
        "dst.close()",
        "src.close()",
    ])
    return "\n".join([
        "set -euo pipefail",
        f"cd {shlex.quote(REMOTE_DATA)}",
        f"rm -f {REMOTE_COPY} {tmp} {tmp}-journal {tmp}-wal {tmp}-shm",
        f"need=$(( $(stat -c %s {REMOTE_DB}) * {DISK_HEADROOM_PCT} / 100 ))",
        "free=$(df --output=avail -B1 . | tail -n 1 | tr -d ' ')",
        'if [ "$free" -lt "$need" ]; then echo "disk: $free bytes free, $need needed for the copy" >&2; exit 4; fi',
        f"sudo -u {APP_USER} python3 - <<'PY'",
        one_step_backup,
        "PY",
        f"qc=$(sqlite3 -readonly 'file:{tmp}?immutable=1' 'PRAGMA quick_check;')",
        f'if [ "$qc" != ok ]; then echo "quick_check: $qc" >&2; rm -f {tmp}; exit 3; fi',
        f"mv -f {tmp} {REMOTE_COPY}",
        f'echo "sha256=$(sha256sum {REMOTE_COPY} | cut -d" " -f1)"',
        f'echo "size=$(stat -c %s {REMOTE_COPY})"',
        f"sqlite3 -readonly 'file:{REMOTE_COPY}?immutable=1' {shlex.quote(counts_sql)}",
        'echo "service=$(systemctl is-active lab.service || true)"',
    ]) + "\n"


def rel(p: Path) -> str:
    try:
        return str(p.relative_to(REPO))
    except ValueError:
        return str(p)


def host() -> str:
    if not HOST_FILE.exists():
        raise BackupError(f"{HOST_FILE} missing — run deploy/provision_do.py first")
    return HOST_FILE.read_text().strip()


def ssh_cmd() -> list[str]:
    return ["ssh", "-i", str(SSH_KEY), *SSH_OPTS]


def run(cmd: list[str], what: str, stdin: str | None = None) -> str:
    log.debug("%s: %s", what, shlex.join(cmd))
    try:
        p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired as e:
        raise BackupError(f"{what}: timed out after {TIMEOUT_S} s") from e
    if p.returncode != 0:
        raise BackupError(f"{what}: exit {p.returncode}: {(p.stderr or p.stdout).strip()[-600:]}")
    return p.stdout


def rsync(src: str, dst: Path, what: str, excludes: tuple[str, ...] = ()) -> None:
    cmd = ["rsync", "-a", "-z", f"--timeout={TIMEOUT_S}", *(f"--exclude={e}" for e in excludes),
           "-e", shlex.join(ssh_cmd()), src, str(dst)]
    run(cmd, what)


# ---- local side ----------------------------------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_local(path: Path) -> dict[str, str]:
    con = sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True)
    try:
        ic = [r[0] for r in con.execute("PRAGMA integrity_check")]
        if ic != ["ok"]:
            raise BackupError(f"integrity_check on {path.name}: {ic[:5]}")
        return {k: str(con.execute(q).fetchone()[0]) for k, q in COUNT_QUERIES.items()}
    finally:
        con.close()


def clone(src: Path, dst: Path) -> None:
    """APFS copy-on-write clone (instant, no extra space); a plain copy on other file systems."""
    if subprocess.run(["cp", "-c", str(src), str(dst)], capture_output=True).returncode != 0:
        shutil.copy2(src, dst)


def write_gz(src: Path, dst: Path, expected_sha: str) -> None:
    """Compressed copy through a temp name, published only when what went in and what reads back both hash to
    `expected_sha` (a crash never leaves an unverified file under the final name)."""
    tmp = dst.with_name(dst.name + ".tmp")
    try:
        h = hashlib.sha256()
        with open(src, "rb") as fi, open(tmp, "wb") as raw:
            with gzip.GzipFile(filename=dst.name[:-3], mode="wb", fileobj=raw, compresslevel=6) as fo:
                for chunk in iter(lambda: fi.read(1 << 20), b""):
                    h.update(chunk)
                    fo.write(chunk)
            raw.flush()
            os.fsync(raw.fileno())
        if h.hexdigest() != expected_sha:
            raise BackupError(f"{dst.name}: the compressed input differs from the verified copy")
        back = hashlib.sha256()
        with gzip.open(tmp, "rb") as g:                        # read back: gzip checks its CRC at the end
            for chunk in iter(lambda: g.read(1 << 20), b""):
                back.update(chunk)
        if back.hexdigest() != expected_sha:
            raise BackupError(f"{dst.name}: the compressed file does not read back to the verified copy")
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)


def prune(directory: Path) -> list[str]:
    copies = {}
    for p in directory.iterdir():
        m = DATED.match(p.name)
        if m:
            copies[dt.date.fromisoformat(m.group(1))] = p
    keep = keep_set(list(copies))
    removed = []
    for d, p in sorted(copies.items()):
        if d not in keep:
            p.unlink()
            removed.append(p.name)
    return removed


def read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def write_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False) + "\n")
    os.replace(tmp, STATE_FILE)


def record_error(message: str) -> None:
    """Keep the last verified copy's fields (the row-count guard needs them) and add the failure for --status."""
    try:
        state = read_state()
        state["last_error"] = {"utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "error": message[:500]}
        write_state(state)
    except OSError as e:
        log.warning("could not record the error in %s: %s", STATE_FILE.name, e)


def notify(message: str) -> None:
    if sys.platform != "darwin" or os.environ.get("LAB_BACKUP_NOTIFY", "1") == "0":
        return
    text = "".join(ch if ch.isprintable() else " " for ch in message)[:220]
    # the text travels as an argument, never inside the AppleScript source: quotes and control characters cannot break it
    cmd = [os.environ.get("LAB_BACKUP_OSASCRIPT", "/usr/bin/osascript"), "-e", "on run argv",
           "-e", f'display notification (item 1 of argv) with title "{NOTIFY_TITLE}"', "-e", "end run", text]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("notification not shown: %s", e)
        return
    if p.returncode != 0:
        log.warning("notification not shown: osascript exit %s %s", p.returncode, p.stderr.strip()[:200])


# ---- watchdogs -----------------------------------------------------------------------------------------------------

def do_get(token: str, path: str) -> dict:
    req = urllib.request.Request(DO_API + path, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def do_check(state: dict, now: dt.datetime) -> tuple[dict, str | None]:
    """DigitalOcean backups of the droplet: on, with an image younger than DO_STALE_HOURS. Read-only; never raises."""
    first_seen = (state.get("do") or {}).get("enabled_first_seen_utc")
    try:
        load_dotenv(REPO / ".env")
        token = os.environ.get("DIGITALOCEAN_TOKEN")
        if not token:
            return {"checked": False, "enabled_first_seen_utc": first_seen}, \
                "DigitalOcean check skipped: no DIGITALOCEAN_TOKEN in .env"
        droplets = [d for d in do_get(token, f"/droplets?tag_name={DROPLET}&per_page=200").get("droplets") or []
                    if d.get("name") == DROPLET]
        if not droplets:
            return {"checked": True, "enabled_first_seen_utc": first_seen}, f"droplet {DROPLET} not found on DigitalOcean"
        did = droplets[0]["id"]
        pol = do_get(token, f"/droplets/{did}/backups/policy").get("policy") or {}
        images = do_get(token, f"/droplets/{did}/backups?per_page=200").get("backups") or []
        newest = max((i.get("created_at") or "" for i in images), default="") or None
        info = {"checked": True, "enabled": bool(pol.get("backup_enabled")),
                "plan": (pol.get("backup_policy") or {}).get("plan"), "images": len(images), "newest_image_utc": newest,
                "enabled_first_seen_utc": first_seen}
        if not info["enabled"]:
            return info, "DigitalOcean backups are OFF for the droplet"
        info["enabled_first_seen_utc"] = first_seen or now.strftime("%Y-%m-%dT%H:%M:%SZ")
        if newest:
            age = hours_since(newest, now)
            if age > DO_STALE_HOURS:
                return info, f"newest DigitalOcean backup image is {age:.0f} h old"
        elif hours_since(info["enabled_first_seen_utc"], now) > DO_FIRST_IMAGE_GRACE_HOURS:
            return info, "no DigitalOcean backup image yet"
        return info, None
    except Exception as e:                  # a watchdog never turns a verified copy into a failed backup
        return {"checked": False, "error": f"{type(e).__name__}: {e}"[:200], "enabled_first_seen_utc": first_seen}, \
            f"DigitalOcean check failed: {type(e).__name__}: {e}"


# ---- the run -------------------------------------------------------------------------------------------------------

def backup(force: bool = False, accept_fewer_rows: bool = False) -> int:
    now = dt.datetime.now(dt.timezone.utc)
    today = now.date().isoformat()
    state = read_state()
    if not force and already_done(state, today):
        log.info("today's copy is complete (%s) — nothing to do", state.get("last_ok_utc"))
        return EXIT_OK
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    for stale in DAILY_DIR.glob("lab-*.sqlite.gz.tmp"):           # left by a killed run
        stale.unlink()
    if STAGING.parent.exists():                                   # our staging dir only: rsync temp files of a killed run
        shutil.rmtree(STAGING.parent)
    STAGING.parent.mkdir(parents=True)
    target = f"{SSH_USER}@{host()}"
    t0 = time.monotonic()
    try:
        # 1. consistent copy on the server
        remote = parse_remote(run([*ssh_cmd(), target, "bash", "-s"], "server copy", stdin=remote_script()))
        log.info("server copy: %s bytes sha256=%s… runs=%s newest_ok=%s service=%s", remote["size"],
                 remote["sha256"][:12], remote["runs"], remote["newest_ok_utc"], remote.get("service"))

        # 2. pull into staging, verify, then publish as the latest copy
        if LATEST.exists():
            clone(LATEST, STAGING)
        rsync(f"{target}:{REMOTE_DATA}/{REMOTE_COPY}", STAGING, "rsync database")
        local_sha = sha256_file(STAGING)
        if local_sha != remote["sha256"]:
            raise BackupError(f"sha256 mismatch after rsync: local {local_sha[:12]}… server {remote['sha256'][:12]}…")
        counts = check_local(STAGING)
        diff = {k: (counts[k], remote[k]) for k in COUNT_QUERIES if counts[k] != remote[k]}
        if diff:
            raise BackupError(f"row counts differ from the server's copy: {diff}")
        dropped = rows_dropped(state.get("counts") or {}, counts)
        if dropped and not accept_fewer_rows:
            raise BackupError(f"the server's database has fewer rows than the last verified copy {dropped}: nothing "
                              "replaced, the old copies are kept; after an intentional restore run --accept-fewer-rows")
        os.replace(STAGING, LATEST)
        log.info("copy verified: sha256 equal, integrity_check ok, counts %s%s",
                 {k: v for k, v in counts.items() if k != "newest_ok_utc"},
                 f" (fewer rows accepted: {dropped})" if dropped else "")

        # 3. dated compressed copy + retention
        dated = DAILY_DIR / f"lab-{today}.sqlite.gz"
        write_gz(LATEST, dated, local_sha)
        removed = prune(DAILY_DIR)
        kept = sorted(p.name for p in DAILY_DIR.iterdir() if DATED.match(p.name))
        log.info("dated copy %s (%d bytes, read back ok); %d copies kept%s", dated.name, dated.stat().st_size, len(kept),
                 f", pruned {removed}" if removed else "")

        # 4. the rest of the server's data dir (never pulls .env; never deletes locally); the server copy goes
        rsync(f"{target}:{REMOTE_DATA}/", FILES_DIR, "rsync data dir",
              excludes=("/.env", "/lab.sqlite*", f"/{REMOTE_COPY}*", "/lab.lock", "/*.tmp"))
        log.info("data dir mirrored into %s", rel(FILES_DIR))
        try:
            run([*ssh_cmd(), target, f"rm -f {REMOTE_DATA}/{REMOTE_COPY}"], "remove the server copy")
        except BackupError as e:
            log.warning("%s (the next run removes it first)", e)
    finally:
        STAGING.unlink(missing_ok=True)

    # 5. watchdogs
    warnings = [w for w in (lab_warning(counts["newest_ok_utc"], now),) if w]
    do_info, do_warning = do_check(state, now)
    if do_warning:
        warnings.append(do_warning)
    if do_info.get("checked"):
        log.info("DigitalOcean backups: enabled=%s plan=%s images=%s newest=%s", do_info.get("enabled"),
                 do_info.get("plan"), do_info.get("images"), do_info.get("newest_image_utc"))
    done = done_date(remote, counts, today)
    if run_in_progress(remote):
        log.info("the panel run is in progress (lab.service %s): this copy is partial, the next start repeats it",
                 remote["service"])
    elif done is None:
        log.info("the copy does not hold today's panel run yet: the next start repeats it")

    write_state({"done_date": done, "last_ok_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "sha256": local_sha,
                 "size": int(remote["size"]), "counts": counts, "service": remote.get("service"),
                 "daily_file": rel(dated), "daily_size": dated.stat().st_size, "copies": kept,
                 "do": do_info, "warnings": warnings, "seconds": round(time.monotonic() - t0, 1)})
    for w in warnings:
        log.warning(w)
    if warnings:
        notify("Backup OK, but: " + "; ".join(warnings))
    log.info("backup OK in %.1f s", time.monotonic() - t0)
    return EXIT_OK


# ---- launchd and status --------------------------------------------------------------------------------------------

def agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def install() -> int:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    LAUNCHD_LOG.parent.mkdir(parents=True, exist_ok=True)
    path = agent_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(plist_dict(sys.executable)))
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)    # not loaded yet → ignored
    subprocess.run(["launchctl", "bootstrap", domain, str(path)], check=True)
    times = ", ".join(f"{h:02d}:{m:02d}" for h, m in SCHEDULE)
    print(f"installed {path} ({sys.executable}); runs daily at {times} local time")
    print(f"run it now:  launchctl kickstart {domain}/{LABEL}   ·   log: {LOG_FILE}")
    return EXIT_OK


def uninstall() -> int:
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    agent_path().unlink(missing_ok=True)
    print(f"removed {LABEL}")
    return EXIT_OK


def status() -> int:
    state = read_state()
    rc = EXIT_OK
    loaded = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], capture_output=True).returncode == 0
    print(f"launchd agent {LABEL}: {'loaded' if loaded else 'NOT loaded'} ({agent_path()})")
    if not loaded:
        rc = 1
    last_ok, err = state.get("last_ok_utc"), state.get("last_error")
    if not last_ok:
        print("no verified copy yet" + (f"; last error at {err['utc']}: {err.get('error')}" if err else ""))
        return 1
    print(f"last verified copy: {last_ok} (day done: {state.get('done_date') or 'no'})  sha256 "
          f"{str(state.get('sha256'))[:12]}…  {state.get('size')} bytes  counts {state.get('counts')}")
    print(f"warnings at that run: {state.get('warnings') or 'none'}  ·  DigitalOcean: {state.get('do')}")
    if err and err.get("utc", "") > last_ok:
        print(f"LAST RUN FAILED at {err['utc']}: {err.get('error')}")
        rc = 1
    copies = sorted(DAILY_DIR.glob("lab-*.sqlite.gz"))
    total = sum(p.stat().st_size for p in copies)
    print(f"{len(copies)} dated copies, {total / 1e6:.1f} MB: {', '.join(p.name[4:14] for p in copies)}")
    age = hours_since(last_ok, dt.datetime.now(dt.timezone.utc))
    if age > STATUS_STALE_HOURS:
        print(f"WARNING: the last verified copy is {age:.0f} h old")
        rc = 1
    return rc


def setup_logging(debug: bool = False) -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)sZ %(levelname)s %(name)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
    fmt.converter = time.gmtime
    handlers: list[logging.Handler] = [logging.FileHandler(LOG_FILE, encoding="utf-8")]
    if os.environ.get("LAB_BACKUP_LAUNCHD") != "1":         # by hand: also to the terminal
        handlers.append(logging.StreamHandler(sys.stderr))
    for h in handlers:
        h.setFormatter(fmt)
        log.addHandler(h)
    log.setLevel(logging.DEBUG if debug else logging.INFO)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Daily verified backup of the lab database to this Mac (see the docstring).")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--status", action="store_true", help="last verified copy, last error, copies, launchd agent")
    mode.add_argument("--install", action="store_true", help="install and load the launchd agent")
    mode.add_argument("--uninstall", action="store_true", help="unload and remove the launchd agent")
    ap.add_argument("--force", action="store_true", help="back up even if today's copy is complete")
    ap.add_argument("--accept-fewer-rows", action="store_true",
                    help="accept a database with fewer runs than the last verified copy (after an intentional restore)")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args(argv)
    if args.install:
        return install()
    if args.uninstall:
        return uninstall()
    if args.status:
        return status()
    setup_logging(args.debug)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))     # graceful stop: finally-blocks remove temp files
    with open(LOCK_FILE, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.info("another backup run holds %s — exit", LOCK_FILE.name)
            return EXIT_OK
        try:
            return backup(force=args.force, accept_fewer_rows=args.accept_fewer_rows)
        except BackupError as e:
            log.error("backup FAILED: %s", e)
            record_error(str(e))
            notify(f"Backup FAILED: {e}")
            return EXIT_FAILED
        except Exception as e:                                    # unexpected: keep the traceback in the log
            log.exception("backup FAILED (unexpected): %s", e)
            record_error(f"{type(e).__name__}: {e}")
            notify(f"Backup FAILED: {type(e).__name__}: {e}")
            return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
