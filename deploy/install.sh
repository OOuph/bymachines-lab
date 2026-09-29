#!/usr/bin/env bash
# By Machines Lab — server install, idempotent. Run over ssh as root on Ubuntu 24.04:
#   ssh -i ~/.ssh/bymachines_lab_ed25519 root@$(cat data/host.txt) 'bash -s' -- bootstrap < deploy/install.sh
#   ssh -i ~/.ssh/bymachines_lab_ed25519 root@$(cat data/host.txt) 'bash -s' -- deploy   < deploy/install.sh
# bootstrap: system packages, the `lab` user, /opt/bymachines-lab, uv, unattended security upgrades, UTC clock.
# deploy:    git pull (or clone) the public repo, uv sync, install the systemd units, enable the timer.
# The .env with the API keys is copied separately (never in git):  scp -i <key> .env root@<ip>:/opt/bymachines-lab/.env
set -euo pipefail
cd /   # uv discovers uv.toml in the current directory and its parents; never run it from /root as another user

REPO_URL="https://github.com/OOuph/bymachines-lab.git"
APP_DIR="/opt/bymachines-lab"
APP_USER="lab"
PYTHON_VERSION="3.12"
MODE="${1:-deploy}"

bootstrap() {
  export DEBIAN_FRONTEND=noninteractive
  timedatectl set-timezone UTC || true
  apt-get update -q
  apt-get install -y -q git curl ca-certificates unattended-upgrades python3 python3-venv sqlite3 rsync
  id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --home-dir "/home/$APP_USER" --shell /usr/sbin/nologin "$APP_USER"
  mkdir -p "$APP_DIR"
  chown "$APP_USER:$APP_USER" "$APP_DIR"
  if [ ! -x "/home/$APP_USER/.local/bin/uv" ]; then
    sudo -u "$APP_USER" -H bash -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
  fi
  sudo -u "$APP_USER" -H "/home/$APP_USER/.local/bin/uv" python install "$PYTHON_VERSION"
  echo "bootstrap done: $(python3 --version), uv $(sudo -u "$APP_USER" -H "/home/$APP_USER/.local/bin/uv" --version)"
}

deploy() {
  if [ -d "$APP_DIR/.git" ]; then
    sudo -u "$APP_USER" -H git -C "$APP_DIR" pull -q --ff-only
  elif [ -f "$APP_DIR/pyproject.toml" ]; then
    echo "using the rsynced tree in $APP_DIR (no .git) — switch to git once the code is on main"
  else
    sudo -u "$APP_USER" -H git clone -q "$REPO_URL" "$APP_DIR"
  fi
  chown -R "$APP_USER:$APP_USER" "$APP_DIR"
  sudo -u "$APP_USER" -H bash -c "cd '$APP_DIR' && /home/$APP_USER/.local/bin/uv sync -q --python $PYTHON_VERSION --no-dev"
  mkdir -p "$APP_DIR/data/logs" "$APP_DIR/data/export"
  chown -R "$APP_USER:$APP_USER" "$APP_DIR/data"
  if [ -f "$APP_DIR/.env" ]; then chown "$APP_USER:$APP_USER" "$APP_DIR/.env"; chmod 600 "$APP_DIR/.env"; else echo "WARNING: $APP_DIR/.env missing — copy it with scp before the first run"; fi
  install -m 644 "$APP_DIR/deploy/lab.service" /etc/systemd/system/lab.service
  install -m 644 "$APP_DIR/deploy/lab.timer" /etc/systemd/system/lab.timer
  systemctl daemon-reload
  systemctl enable --now lab.timer
  echo "deploy done: $(git -C "$APP_DIR" log --oneline -1 2>/dev/null || echo 'rsynced tree')"
  systemctl list-timers lab.timer --no-pager | head -3
}

case "$MODE" in
  bootstrap) bootstrap ;;
  deploy) deploy ;;
  *) echo "usage: install.sh bootstrap|deploy" >&2; exit 2 ;;
esac
