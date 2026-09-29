"""deploy/backup_to_mac.py — retention, server output, local verification, the day logic, the row-count guard, the launchd
wrapper, notifications and the DigitalOcean watchdog.

Offline: no ssh, no network, no real notifications (a fake osascript records its arguments).
"""

from __future__ import annotations

import datetime as dt
import gzip
import http.client
import importlib.util
import json
import os
import plistlib
import shutil
import signal
import sqlite3
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location("backup_to_mac", Path(__file__).resolve().parents[1] / "deploy" / "backup_to_mac.py")
bm = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bm)

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 2, 11, 0, tzinfo=UTC)


def span(start: str, n: int, step: int = 1) -> list[dt.date]:
    d0 = dt.date.fromisoformat(start)
    return [d0 + dt.timedelta(days=i * step) for i in range(n)]


def newest_per(days: list[dt.date], key, n: int) -> set[dt.date]:
    groups = defaultdict(list)
    for d in days:
        groups[key(d)].append(d)
    return {max(groups[k]) for k in sorted(groups, reverse=True)[:n]}


WEEK = lambda d: tuple(d.isocalendar())[:2]   # noqa: E731
MONTH = lambda d: (d.year, d.month)           # noqa: E731


# ---- retention -----------------------------------------------------------------------------------------------------

def test_keep_set_a_year_of_daily_copies():
    days = span("2026-09-29", 365)
    keep = bm.keep_set(days)
    expected = set(sorted(days)[-7:]) | newest_per(days, WEEK, 4) | newest_per(days, MONTH, 12)
    assert keep == expected
    assert len(keep) <= bm.KEEP_DAILY + bm.KEEP_WEEKLY + bm.KEEP_MONTHLY
    assert min(days) not in keep


def test_keep_set_with_gaps_keeps_the_newest_copy_of_each_period():
    days = span("2026-10-01", 20, step=3) + span("2026-12-20", 4)     # the Mac asleep for weeks at a time
    keep = bm.keep_set(days)
    assert set(sorted(days)[-7:]) <= keep                               # 7 newest copies, even across a long gap
    assert newest_per(days, WEEK, 4) <= keep
    assert newest_per(days, MONTH, 12) <= keep


def test_keep_set_few_copies_keeps_all():
    days = span("2026-09-29", 3)
    assert bm.keep_set(days) == set(days)


def test_prune_touches_only_dated_copies(tmp_path):
    days = span("2026-06-01", 120)
    for d in days:
        (tmp_path / f"lab-{d}.sqlite.gz").write_bytes(b"x")
    foreign = [tmp_path / "notes.txt", tmp_path / "lab-latest.sqlite", tmp_path / "lab-2026-10-01.sqlite"]
    for p in foreign:
        p.write_bytes(b"y")
    removed = bm.prune(tmp_path)
    left = {dt.date.fromisoformat(p.name[4:14]) for p in tmp_path.glob("lab-*.sqlite.gz")}
    assert left == bm.keep_set(days)
    assert len(removed) == len(days) - len(left)
    assert all(p.exists() for p in foreign)


# ---- server output and the day logic -------------------------------------------------------------------------------

def good_output(**over) -> str:
    fields = {"sha256": "a" * 64, "size": "9289728", "runs": "392", "runs_ok": "392", "citations": "4100", "mentions": "16",
              "newest_ok_utc": "2026-09-29T09:02:54Z", "service": "inactive"} | over
    return "\n".join(f"{k}={v}" for k, v in fields.items()) + "\n"


def test_parse_remote_ok():
    out = bm.parse_remote(good_output())
    assert out["runs"] == "392" and out["newest_ok_utc"] == "2026-09-29T09:02:54Z"


def test_parse_remote_empty_newest_is_allowed():
    assert bm.parse_remote(good_output(newest_ok_utc=""))["newest_ok_utc"] == ""


@pytest.mark.parametrize("text", ["quick_check=*** in database main ***\n", good_output(sha256="xyz")])
def test_parse_remote_rejects_incomplete_or_bad_output(text):
    with pytest.raises(bm.BackupError):
        bm.parse_remote(text)


@pytest.mark.parametrize("service,partial", [("inactive", False), ("failed", False), ("activating", True), ("active", True)])
def test_run_in_progress_marks_the_copy_partial(service, partial):
    assert bm.run_in_progress(bm.parse_remote(good_output(service=service))) is partial


@pytest.mark.parametrize("newest,service,done", [
    ("2026-09-30T06:52:10Z", "inactive", "2026-09-30"),   # the day's run is in the copy
    ("2026-09-29T09:02:54Z", "inactive", None),           # woke before 06:00 UTC: the copy lacks today's run → repeat
    ("2026-09-30T06:20:00Z", "activating", None),         # run in progress → partial → repeat
    ("", "inactive", None),
])
def test_done_date(newest, service, done):
    remote = bm.parse_remote(good_output(service=service, newest_ok_utc=newest))
    assert bm.done_date(remote, {"newest_ok_utc": newest}, "2026-09-30") == done


def test_rows_dropped():
    prev = {"runs": "5000", "runs_ok": "4990", "mentions": "900"}
    assert bm.rows_dropped(prev, {"runs": "12", "runs_ok": "12", "mentions": "40"}) == {"runs": (5000, 12), "runs_ok": (4990, 12)}
    assert bm.rows_dropped(prev, {"runs": "5000", "runs_ok": "4995", "mentions": "10"}) == {}   # mentions may shrink
    assert bm.rows_dropped({}, {"runs": "1", "runs_ok": "1"}) == {}


@pytest.mark.parametrize("now,newest,warns", [
    ("2026-09-30T10:30:00Z", "2026-09-29T09:02:54Z", True),    # no run today by 10:30 UTC (a 25 h age would pass a 26 h rule)
    ("2026-09-30T05:45:00Z", "2026-09-29T07:00:00Z", False),   # before 08:00 UTC yesterday's run is enough
    ("2026-09-30T05:45:00Z", "2026-09-28T07:00:00Z", True),
    ("2026-09-30T10:30:00Z", "2026-09-30T06:40:00Z", False),
    ("2026-09-30T10:30:00Z", "", True),
])
def test_lab_warning(now, newest, warns):
    t = dt.datetime.fromisoformat(now.replace("Z", "+00:00"))
    assert (bm.lab_warning(newest, t) is not None) is warns


def test_remote_script_never_opens_the_live_database_as_root():
    script = bm.remote_script()
    lines = script.splitlines()
    touching_live = [ln for ln in lines if bm.REMOTE_DB in ln.replace(bm.REMOTE_COPY, "")]
    assert all(ln.startswith(("rm -f", "need=", "src = ")) for ln in touching_live), touching_live
    assert f"sudo -u {bm.APP_USER} python3 - <<'PY'" in lines                   # the only reader of the live file
    assert lines[0] == "set -euo pipefail"
    assert all(f"{key}=" in script for key in ("sha256", "size", "service", *bm.COUNT_QUERIES))


def test_remote_script_is_valid_bash():
    assert subprocess.run(["bash", "-n"], input=bm.remote_script(), text=True).returncode == 0


# ---- local verification --------------------------------------------------------------------------------------------

def make_db(path: Path, runs: int = 3, day: str = "2026-09-29") -> None:
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE runs (id INTEGER PRIMARY KEY, ts_utc TEXT, status TEXT, raw_json TEXT);
        CREATE TABLE citations (run_id INTEGER, url TEXT);
        CREATE TABLE mentions (run_id INTEGER, brand_id TEXT);
    """)
    for i in range(runs):
        con.execute("INSERT INTO runs (ts_utc, status, raw_json) VALUES (?, ?, ?)",
                    (f"{day}T0{i % 10}:00:00Z", "ok" if i else "error", "x" * 5000))
        con.execute("INSERT INTO citations VALUES (?, ?)", (i + 1, "https://example.com"))
    con.commit()
    con.close()


def test_check_local_counts_match_the_query_list(tmp_path):
    db = tmp_path / "lab.sqlite"
    make_db(db)
    counts = bm.check_local(db)
    assert counts == {"runs": "3", "runs_ok": "2", "citations": "3", "mentions": "0", "newest_ok_utc": "2026-09-29T02:00:00Z"}
    assert not (tmp_path / "lab.sqlite-wal").exists() and not (tmp_path / "lab.sqlite-journal").exists()


def test_check_local_refuses_damage_the_row_counts_cannot_see(tmp_path):
    db = tmp_path / "lab.sqlite"
    make_db(db, runs=0)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO runs (ts_utc, status, raw_json) VALUES ('2026-09-29T06:00:00Z', 'ok', ?)", ("x" * 20000,))
    con.commit()
    con.close()
    raw = bytearray(db.read_bytes())
    last = len(raw) - 4096                                   # the last overflow page of the one big raw_json
    raw[last:last + 4] = b"\x7f\xff\xff\xff"                 # its "next page" pointer now points outside the file
    db.write_bytes(bytes(raw))
    con = sqlite3.connect(f"{db.as_uri()}?mode=ro&immutable=1", uri=True)
    assert [con.execute(q).fetchone()[0] for q in bm.COUNT_QUERIES.values()][:2] == [1, 1]   # counts do not notice
    con.close()
    with pytest.raises(bm.BackupError, match="integrity_check"):
        bm.check_local(db)


def test_write_gz_round_trip(tmp_path):
    src = tmp_path / "lab-latest.sqlite"
    make_db(src)
    dst = tmp_path / "lab-2026-09-29.sqlite.gz"
    bm.write_gz(src, dst, bm.sha256_file(src))
    assert gzip.decompress(dst.read_bytes()) == src.read_bytes()
    assert b"lab-2026-09-29.sqlite\x00" in dst.read_bytes()[:64]          # original name in the gzip header, no .tmp
    assert not list(tmp_path.glob("*.tmp"))


def test_write_gz_publishes_nothing_on_a_mismatch(tmp_path):
    src = tmp_path / "lab-latest.sqlite"
    make_db(src)
    dst = tmp_path / "lab-2026-09-29.sqlite.gz"
    with pytest.raises(bm.BackupError):
        bm.write_gz(src, dst, "0" * 64)
    assert not dst.exists() and not list(tmp_path.glob("*.tmp"))


def test_already_done(tmp_path, monkeypatch):
    monkeypatch.setattr(bm, "DAILY_DIR", tmp_path)
    assert not bm.already_done({"done_date": "2026-09-29"}, "2026-09-29")
    (tmp_path / "lab-2026-09-29.sqlite.gz").write_bytes(b"x")
    assert bm.already_done({"done_date": "2026-09-29"}, "2026-09-29")
    assert not bm.already_done({"done_date": None}, "2026-09-29")
    assert not bm.already_done({"done_date": "2026-09-28"}, "2026-09-29")


# ---- the whole run against a fake server ---------------------------------------------------------------------------

@pytest.fixture
def fake_server(tmp_path, monkeypatch):
    """backup() end to end: `run` and `rsync` act on a local "server" directory; paths point into tmp_path."""
    srv = tmp_path / "srv"
    srv.mkdir()
    b = tmp_path / "b"
    for name, p in (("BACKUP_DIR", b), ("LATEST", b / "lab-latest.sqlite"), ("STAGING", b / "incoming" / "lab-latest.sqlite"),
                    ("DAILY_DIR", b / "daily"), ("FILES_DIR", b / "files"), ("STATE_FILE", b / "state.json"),
                    ("LOG_FILE", b / "backup.log"), ("LOCK_FILE", b / ".lock"), ("HOST_FILE", tmp_path / "host.txt")):
        monkeypatch.setattr(bm, name, p)
    (tmp_path / "host.txt").write_text("203.0.113.1\n")
    b.mkdir()
    calls = []

    def fake_run(cmd, what, stdin=None):
        calls.append(what)
        if what == "server copy":
            copy = srv / "lab-backup.sqlite"
            shutil.copy2(srv / "lab.sqlite", copy)
            counts = bm.check_local(copy)
            return "\n".join([f"sha256={bm.sha256_file(copy)}", f"size={copy.stat().st_size}",
                              *(f"{k}={v}" for k, v in counts.items()), "service=inactive"]) + "\n"
        return ""

    def fake_rsync(src, dst, what, excludes=()):
        calls.append(what)
        if what == "rsync database":
            shutil.copy2(srv / "lab-backup.sqlite", dst)
        else:
            Path(dst).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(bm, "run", fake_run)
    monkeypatch.setattr(bm, "rsync", fake_rsync)
    monkeypatch.setattr(bm, "do_check", lambda state, now: ({"checked": False}, None))
    monkeypatch.setenv("LAB_BACKUP_NOTIFY", "0")
    return srv, calls


def test_backup_end_to_end_and_the_fewer_rows_guard(fake_server):
    srv, calls = fake_server
    today = dt.datetime.now(UTC).date().isoformat()
    make_db(srv / "lab.sqlite", runs=5, day=today)
    assert bm.backup(force=True) == 0
    first = bm.sha256_file(bm.LATEST)
    assert first == bm.sha256_file(srv / "lab.sqlite")
    dated = bm.DAILY_DIR / f"lab-{today}.sqlite.gz"
    assert gzip.decompress(dated.read_bytes()) == bm.LATEST.read_bytes()
    state = json.loads(bm.STATE_FILE.read_text())
    assert state["counts"]["runs"] == "5" and state["done_date"] == today and state["sha256"] == first
    assert calls == ["server copy", "rsync database", "rsync data dir", "remove the server copy"]
    assert not bm.STAGING.exists()
    assert bm.backup() == 0 and calls[-1] == "remove the server copy"               # the day is done: no second copy

    (srv / "lab.sqlite").unlink()
    make_db(srv / "lab.sqlite", runs=2, day=today)                                 # the server lost rows
    with pytest.raises(bm.BackupError, match="fewer rows"):
        bm.backup(force=True)
    assert bm.sha256_file(bm.LATEST) == first                                      # nothing replaced
    assert gzip.decompress(dated.read_bytes()) == bm.LATEST.read_bytes()
    assert json.loads(bm.STATE_FILE.read_text())["counts"]["runs"] == "5"
    assert not bm.STAGING.exists()

    assert bm.backup(force=True, accept_fewer_rows=True) == 0                       # an intentional restore
    assert json.loads(bm.STATE_FILE.read_text())["counts"]["runs"] == "2"


@pytest.mark.parametrize("error", [bm.BackupError("server copy: exit 255: ssh: connect to host port 22: Operation timed out"),
                                   RuntimeError("something unexpected")])
def test_failed_run_exits_3_logs_and_keeps_the_last_good_state(fake_server, monkeypatch, error):
    good = {"done_date": "2026-09-28", "last_ok_utc": "2026-09-28T10:30:00Z", "counts": {"runs": "7", "runs_ok": "7"}}
    bm.STATE_FILE.write_text(json.dumps(good))
    monkeypatch.setenv("LAB_BACKUP_LAUNCHD", "1")

    def boom(*a, **k):
        raise error
    monkeypatch.setattr(bm, "run", boom)
    old_term = signal.getsignal(signal.SIGTERM)
    try:
        assert bm.main(["--force"]) == bm.EXIT_FAILED == 3
    finally:
        signal.signal(signal.SIGTERM, old_term)
        for h in list(bm.log.handlers):
            bm.log.removeHandler(h)
            h.close()
    text = bm.LOG_FILE.read_text()
    assert "ERROR lab.backup backup FAILED" in text and str(error) in text
    state = json.loads(bm.STATE_FILE.read_text())
    assert {k: state[k] for k in good} == good and str(error) in state["last_error"]["error"]
    assert not list(bm.DAILY_DIR.glob("*"))


def test_help_and_typos_never_start_a_backup(monkeypatch):
    monkeypatch.setattr(bm, "backup", lambda **k: pytest.fail("a backup started"))
    for argv in (["--help"], ["--staus"]):
        with pytest.raises(SystemExit) as e:
            bm.main(argv)
        assert e.value.code in (0, 2)


# ---- launchd wrapper and notifications -----------------------------------------------------------------------------

def test_plist_runs_this_script_through_the_wrapper():
    p = bm.plist_dict("/usr/bin/python3")
    assert p["Label"] == bm.LABEL
    assert p["ProgramArguments"][:3] == ["/bin/sh", "-c", bm.WRAPPER]
    assert p["ProgramArguments"][3] == "/usr/bin/python3" and p["ProgramArguments"][4].endswith("deploy/backup_to_mac.py")
    assert [(i["Hour"], i["Minute"]) for i in p["StartCalendarInterval"]] == list(bm.SCHEDULE)
    assert p["EnvironmentVariables"]["LAB_BACKUP_LAUNCHD"] == "1" and p["RunAtLoad"] is False
    assert b".env" not in plistlib.dumps(p)


def fake_osascript(tmp_path) -> tuple[Path, Path]:
    record = tmp_path / "osascript-args.txt"
    fake = tmp_path / "osascript"
    fake.write_text(f'#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a" >> "{record}"; done\n')
    fake.chmod(0o755)
    return fake, record


def test_wrapper_notifies_only_when_the_job_did_not_run(tmp_path):
    fake, record = fake_osascript(tmp_path)
    env = {**os.environ, "LAB_BACKUP_OSASCRIPT": str(fake)}
    job = tmp_path / "job.sh"
    for code, notified in ((0, False), (3, False), (143, False), (1, True)):
        job.write_text(f"#!/bin/sh\nexit {code}\n")
        job.chmod(0o755)
        record.unlink(missing_ok=True)
        rc = subprocess.run(["/bin/sh", "-c", bm.WRAPPER, str(job), "script.py"], env=env).returncode
        assert rc == code and record.exists() is notified
    record.unlink(missing_ok=True)
    rc = subprocess.run(["/bin/sh", "-c", bm.WRAPPER, str(tmp_path / "no-such-python"), "script.py"], env=env,
                        capture_output=True).returncode
    assert rc == 127 and "did not run (exit 127)" in record.read_text()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS notifications")
def test_notify_passes_the_text_as_an_argument(tmp_path, monkeypatch):
    fake, record = fake_osascript(tmp_path)
    monkeypatch.setenv("LAB_BACKUP_OSASCRIPT", str(fake))
    monkeypatch.delenv("LAB_BACKUP_NOTIFY", raising=False)
    bm.notify('Backup FAILED: "quoted" \x1b[0m \\ end')
    args = record.read_text().splitlines()
    assert args[-1] == 'Backup FAILED: "quoted"  [0m \\ end'                 # control character → space, text last
    assert not any("Backup FAILED" in a for a in args[:-1])                 # never inside the AppleScript source


# ---- DigitalOcean watchdog -----------------------------------------------------------------------------------------

def fake_do(monkeypatch, enabled, images, policy_null=False):
    monkeypatch.setattr(bm, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", "test")

    def get(token, path):
        if path.startswith("/droplets?"):
            return {"droplets": [{"id": 7, "name": bm.DROPLET}]}
        if path.endswith("/backups/policy"):
            return {"policy": None} if policy_null else \
                {"policy": {"backup_enabled": enabled, "backup_policy": {"plan": "daily", "hour": 12}}}
        return {"backups": [{"created_at": t} for t in images]}
    monkeypatch.setattr(bm, "do_get", get)


def test_do_check_fresh_image_is_quiet(monkeypatch):
    fake_do(monkeypatch, True, ["2026-10-01T13:10:00Z", "2026-09-30T13:05:00Z"])
    info, warning = bm.do_check({}, NOW)
    assert warning is None and info["images"] == 2 and info["newest_image_utc"] == "2026-10-01T13:10:00Z"


def test_do_check_stale_image_warns(monkeypatch):
    fake_do(monkeypatch, True, ["2026-09-30T13:05:00Z"])
    assert "h old" in bm.do_check({}, NOW)[1]


def test_do_check_first_image_grace(monkeypatch):
    fake_do(monkeypatch, True, [])
    info, warning = bm.do_check({}, NOW)                                      # first time seen enabled: grace starts now
    assert warning is None and info["enabled_first_seen_utc"] == "2026-10-02T11:00:00Z"
    late = {"do": {"enabled_first_seen_utc": "2026-09-30T10:00:00Z"}}
    assert bm.do_check(late, NOW)[1] == "no DigitalOcean backup image yet"


def test_do_check_backups_off_or_null_policy_warns(monkeypatch):
    fake_do(monkeypatch, False, [])
    assert "OFF" in bm.do_check({}, NOW)[1]
    fake_do(monkeypatch, True, [], policy_null=True)
    assert "OFF" in bm.do_check({}, NOW)[1]


def test_do_check_missing_token_warns(monkeypatch):
    monkeypatch.setattr(bm, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
    info, warning = bm.do_check({"do": {"enabled_first_seen_utc": "2026-09-29T10:18:43Z"}}, NOW)
    assert "no DIGITALOCEAN_TOKEN" in warning and info["enabled_first_seen_utc"] == "2026-09-29T10:18:43Z"


@pytest.mark.parametrize("exc", [OSError("network down"), http.client.IncompleteRead(b""), AttributeError("x")])
def test_do_check_never_raises_and_keeps_the_grace_start(monkeypatch, exc):
    monkeypatch.setattr(bm, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", "test")

    def boom(token, path):
        raise exc
    monkeypatch.setattr(bm, "do_get", boom)
    info, warning = bm.do_check({"do": {"enabled_first_seen_utc": "2026-09-29T10:18:43Z"}}, NOW)
    assert warning.startswith("DigitalOcean check failed") and info["enabled_first_seen_utc"] == "2026-09-29T10:18:43Z"


def test_hours_since():
    assert bm.hours_since("2026-10-01T09:00:00Z", NOW) == pytest.approx(26.0)
