# Deploy — VPS, systemd timer, sync

The panel runs on a small VPS under a systemd timer (daily 06:00 UTC). The operator's machine develops, tests, reads exports and keeps the daily verified backup (§3). Ports 80/443 stay closed until the site goes live (`--web`, owner command); the site itself lives in the separate private repository `bymachines-site` (nginx config and install script there).

## 1. Server

`deploy/provision_do.py` creates the DigitalOcean droplet idempotently (ssh key, droplet `bymachines-lab` in `ams3`, `s-1vcpu-1gb`, Ubuntu 24.04, firewall inbound 22 only; `--web` adds 80/443 for the site; daily droplet backups, §3) and writes the IP to `data/host.txt` (git-ignored). Any Ubuntu 24.04 box with root ssh works the same way; put its IP in `data/host.txt`.

```bash
uv run python deploy/provision_do.py            # needs DIGITALOCEAN_TOKEN in .env
HOST=$(cat data/host.txt); KEY=~/.ssh/bymachines_lab_ed25519
ssh -i $KEY root@$HOST 'bash -s' -- bootstrap < deploy/install.sh   # packages, user `lab`, /opt/bymachines-lab, uv, Python 3.12
scp -i $KEY .env root@$HOST:/opt/bymachines-lab/.env                # API keys — never in git
ssh -i $KEY root@$HOST 'bash -s' -- deploy    < deploy/install.sh   # clone/pull, uv sync, install and enable lab.timer
```

Re-run `deploy` after every push to `main`. The service runs as the unprivileged user `lab` from `/opt/bymachines-lab` with `LAB_DATA_DIR=/opt/bymachines-lab/data`.

## 2. Operate

```bash
ssh -i $KEY root@$HOST systemctl list-timers lab.timer            # next run
ssh -i $KEY root@$HOST systemctl start lab.service                # run today now (idempotent: only missing cells)
ssh -i $KEY root@$HOST journalctl -u lab.service -n 50            # last run's log
ssh -i $KEY root@$HOST 'sudo -u lab -H bash -c "cd /opt/bymachines-lab && LAB_DATA_DIR=/opt/bymachines-lab/data .venv/bin/lab status"'
ssh -i $KEY root@$HOST 'cat /opt/bymachines-lab/data/ALERT'       # exists only after a budget stop; delete it after reading
```

Freezing panels (SPEC S-J): panel files are not in git until their freeze date. Copy them to the server with `rsync` (below), run `lab freeze --panel config/panels/<file>` locally, commit `config/panel-hashes.txt`, and `deploy` again; the server refuses a panel whose hash is not in the journal.

## 3. Sync and backups (SPEC S-L, §5)

Two independent backup layers (owner decision 2026-09-29):

- **DigitalOcean droplet backups** — a daily image of the whole server, kept 7 days, window 12:00–16:00 UTC (after the 06:00 run); `BACKUP_POLICY` in `provision_do.py`, $1.80/month for the $6 droplet on 2026-09-29. Restores everything at once: system, nginx and certificates, code, `.env`, the database, nginx logs.
- **Daily verified copy on the Mac** — `deploy/backup_to_mac.py` under launchd, 11:30 and 18:30 local time (a Mac that slept through a slot runs it on wake):
  - the server makes a one-step SQLite backup as user `lab` — a consistent snapshot even while the panel writes; refused when the disk lacks room — and the Mac pulls it with `rsync` (only changed blocks travel);
  - the copy is kept only if its sha256 equals the server's, `PRAGMA integrity_check` says ok, the row counts equal the server's and `runs` did not go down against the last verified copy; then it becomes `data/backups/lab-latest.sqlite` and a dated `data/backups/daily/lab-YYYY-MM-DD.sqlite.gz`, read back before it is kept;
  - retention: every day for a week, the last copy of each of the last 4 ISO weeks and of each of the last 12 months (the current ones included) — about 20 files;
  - the rest of the server's `data/` (run logs, journal, exports) is mirrored into `data/backups/files/`, never deleting there; the server copy is removed after the transfer;
  - a day is done only when its copy holds that day's panel run; until then every slot repeats it;
  - macOS notifications: a failed run; fewer rows than the last verified copy (nothing is replaced); no ok panel run for the day that should be in (today from 08:00 UTC, before that yesterday); DigitalOcean backups off or the newest image older than 30 h; the job could not start at all (the launchd wrapper; log `~/Library/Logs/ai.bymachines.lab-backup.log`);
  - the nginx logs are not copied to the Mac: the site footer promises a twelve-week access log.

```bash
.venv/bin/python deploy/backup_to_mac.py --install            # launchd agent ai.bymachines.lab-backup for this checkout and interpreter
.venv/bin/python deploy/backup_to_mac.py --status             # last verified copy, last error, copies; exit 1 if failing or > 36 h old
.venv/bin/python deploy/backup_to_mac.py                      # back up now; --force repeats a done day
.venv/bin/python deploy/backup_to_mac.py --accept-fewer-rows  # once, after an intentional restore of an older copy (§4)
launchctl kickstart gui/$(id -u)/ai.bymachines.lab-backup     # run the agent now; log in data/backups/backup.log
.venv/bin/python deploy/backup_to_mac.py --uninstall
```

By hand:

```bash
# exports and the operational journal → local data/ (patterns anchored with a leading slash; never pulls .env)
rsync -az -e "ssh -i $KEY" --exclude '/.env' --exclude '/lab.sqlite*' --exclude '/lab-backup.sqlite*' --exclude '/logs' root@$HOST:/opt/bymachines-lab/data/ data/server/
# panels (draft or frozen) to the server
rsync -az -e "ssh -i $KEY" config/panels/ root@$HOST:/opt/bymachines-lab/config/panels/
```

A verified database copy right now: `backup_to_mac.py --force`.

## 4. Restore

- **Read an old state without restoring:** the copies are self-contained SQLite files (rollback journal, no `-wal`). `data/backups/lab-latest.sqlite` is always the last verified copy; open it read-only: `sqlite3 -readonly "file:data/backups/lab-latest.sqlite?immutable=1"`. A dated copy: `gunzip -c data/backups/daily/lab-YYYY-MM-DD.sqlite.gz > /tmp/lab-YYYY-MM-DD.sqlite` (outside `data/backups/`, so nothing stray stays there).
- **Database only, server alive** (newest copy first — runs after its date are lost except the last `catch_up_days`, which the planner re-asks):

```bash
ssh -i $KEY root@$HOST 'systemctl stop lab.timer; systemctl is-active lab.service'   # anything but "active"/"activating" → safe to swap
gunzip -c data/backups/daily/lab-YYYY-MM-DD.sqlite.gz > /tmp/lab-restore.sqlite       # or: cp data/backups/lab-latest.sqlite /tmp/lab-restore.sqlite
scp -i $KEY /tmp/lab-restore.sqlite root@$HOST:/opt/bymachines-lab/data/lab.sqlite.restore
ssh -i $KEY root@$HOST 'cd /opt/bymachines-lab/data && ts=$(date -u +%Y%m%dT%H%M%SZ) && for f in lab.sqlite lab.sqlite-wal lab.sqlite-shm; do [ -e $f ] && mv -n $f before-restore-$ts.$f; done; [ ! -e lab.sqlite ] && mv lab.sqlite.restore lab.sqlite && chown lab:lab lab.sqlite && systemctl start lab.timer'
.venv/bin/python deploy/backup_to_mac.py --accept-fewer-rows   # the restored database has fewer runs than the last copy
```

Every attempt keeps the database it replaced, with its own `-wal`/`-shm`, under `before-restore-<UTC time>.*`, so a second attempt with another copy never overwrites the original; delete those files only when the restore is confirmed.

- **Whole server:** DigitalOcean control panel → Droplets → `bymachines-lab` → Backups → *Restore Droplet* (in place: same IP, tags, firewall and host key). An image is a crash-consistent disk copy taken outside the run window; SQLite recovers it like after a power cut. Then check `lab status` (§2) and the site, and run `backup_to_mac.py --accept-fewer-rows` once if the image is older than the last Mac copy. A new droplet from an image instead needs the name and tag `bymachines-lab`, `provision_do.py --web`, the new IP in `data/host.txt` and the DNS A records.
