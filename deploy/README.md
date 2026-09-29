# Deploy — VPS, systemd timer, sync

The panel runs on a small VPS under a systemd timer (daily 06:00 UTC). The operator's machine only develops, tests and reads exports. Nothing listens on 80/443 in this build.

## 1. Server

`deploy/provision_do.py` creates the DigitalOcean droplet idempotently (ssh key, droplet `bymachines-lab` in `ams3`, `s-1vcpu-1gb`, Ubuntu 24.04, firewall inbound 22 only) and writes the IP to `data/host.txt` (git-ignored). Any Ubuntu 24.04 box with root ssh works the same way; put its IP in `data/host.txt`.

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

## 3. Sync to the operator's machine (SPEC S-L)

```bash
# exports and the operational journal → local data/ (patterns anchored with a leading slash; never pulls .env)
rsync -az -e "ssh -i $KEY" --exclude '/.env' --exclude '/lab.sqlite*' --exclude '/logs' root@$HOST:/opt/bymachines-lab/data/ data/server/
# weekly SQLite backup (WAL-safe copy made on the server first)
ssh -i $KEY root@$HOST 'sudo -u lab sqlite3 /opt/bymachines-lab/data/lab.sqlite ".backup /opt/bymachines-lab/data/lab-backup.sqlite"'
rsync -az -e "ssh -i $KEY" root@$HOST:/opt/bymachines-lab/data/lab-backup.sqlite data/backups/lab-$(date -u +%F).sqlite
# panels (draft or frozen) to the server
rsync -az -e "ssh -i $KEY" config/panels/ root@$HOST:/opt/bymachines-lab/config/panels/
```
