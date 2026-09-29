"""Provision the lab server on DigitalOcean — idempotent: ssh key, droplet, firewall, droplet backups. Prints the public IPv4.

Usage (from the repo root, DIGITALOCEAN_TOKEN in .env):
    uv run python deploy/provision_do.py            # create what is missing, print the IP
    uv run python deploy/provision_do.py --status   # only show what exists (droplet, backup policy, backup images)

Parameters live at the top of this file. The private key never leaves the operator's machine; only the public key is
uploaded. The droplet's IP is written to data/host.txt (git-ignored) for deploy/install.sh and the rsync commands.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lab.env import load_dotenv  # noqa: E402

# ---- parameters ----
NAME = "bymachines-lab"                 # droplet, firewall and tag name
REGION = "ams3"                         # Amsterdam (EU); alternatives: fra1, lon1
SIZE = "s-1vcpu-1gb"                    # $6/month on 2026-09-28 (1 vCPU, 1 GiB, 25 GiB SSD)
IMAGE = "ubuntu-24-04-x64"              # Ubuntu 24.04 LTS ships Python 3.12
# DigitalOcean backups (owner decision 2026-09-29): daily images, kept 7 days, window 12:00–16:00 UTC — after the 06:00 UTC
# run. 30 % of the droplet price = $1.80/month on 2026-09-29 (API /droplets/backups/supported_policies; weekly = 20 %).
# The database is also copied to the operator's Mac every day: deploy/backup_to_mac.py.
BACKUP_POLICY = {"plan": "daily", "hour": 12}
SSH_KEY_NAME = "bymachines-lab-operator"
SSH_KEY_PATH = Path.home() / ".ssh" / "bymachines_lab_ed25519"
API = "https://api.digitalocean.com/v2"
HOST_FILE = Path("data") / "host.txt"


def api(token: str):
    return httpx.Client(base_url=API, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, timeout=60)


def ensure_local_key() -> str:
    if not SSH_KEY_PATH.exists():
        SSH_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-C", SSH_KEY_NAME, "-f", str(SSH_KEY_PATH)], check=True,
                       stdout=subprocess.DEVNULL)
        print(f"generated {SSH_KEY_PATH}")
    return SSH_KEY_PATH.with_suffix(".pub").read_text().strip()


def ensure_ssh_key(c: httpx.Client, public_key: str) -> int:
    keys = c.get("/account/keys", params={"per_page": 200}).json()["ssh_keys"]
    for k in keys:
        if k["public_key"].split()[:2] == public_key.split()[:2]:
            return int(k["id"])
    r = c.post("/account/keys", json={"name": SSH_KEY_NAME, "public_key": public_key})
    r.raise_for_status()
    print("uploaded ssh key", SSH_KEY_NAME)
    return int(r.json()["ssh_key"]["id"])


def find_droplet(c: httpx.Client) -> dict | None:
    for d in c.get("/droplets", params={"tag_name": NAME, "per_page": 200}).json().get("droplets", []):
        if d["name"] == NAME:
            return d
    return None


def public_ipv4(d: dict) -> str | None:
    for n in d.get("networks", {}).get("v4", []):
        if n.get("type") == "public":
            return n["ip_address"]
    return None


def ensure_droplet(c: httpx.Client, key_id: int) -> dict:
    d = find_droplet(c)
    if d is None:
        r = c.post("/droplets", json={"name": NAME, "region": REGION, "size": SIZE, "image": IMAGE, "ssh_keys": [key_id],
                                      "backups": True, "backup_policy": BACKUP_POLICY, "ipv6": True, "monitoring": True,
                                      "tags": [NAME]})
        r.raise_for_status()
        d = r.json()["droplet"]
        print(f"created droplet {NAME} ({SIZE}, {REGION}, {IMAGE}) id={d['id']}")
    for _ in range(60):
        d = c.get(f"/droplets/{d['id']}").json()["droplet"]
        if d["status"] == "active" and public_ipv4(d):
            return d
        time.sleep(5)
    raise SystemExit("droplet did not become active in 5 minutes")


WEB_PORTS = ("80", "443")            # opened only with --web, i.e. at the site's go-live (SPEC §0, owner decision 2026-09-29)


def ensure_firewall(c: httpx.Client, droplet_id: int, web: bool = False) -> None:
    inbound = [{"protocol": "tcp", "ports": "22", "sources": {"addresses": ["0.0.0.0/0", "::/0"]}}]
    if web:
        inbound += [{"protocol": "tcp", "ports": p, "sources": {"addresses": ["0.0.0.0/0", "::/0"]}} for p in WEB_PORTS]
    for f in c.get("/firewalls", params={"per_page": 200}).json().get("firewalls", []):
        if f["name"] == NAME:
            if droplet_id not in f.get("droplet_ids", []):
                c.post(f"/firewalls/{f['id']}/droplets", json={"droplet_ids": [droplet_id]}).raise_for_status()
            have = {r.get("ports") for r in f.get("inbound_rules", []) if r.get("protocol") == "tcp"}
            want = {r["ports"] for r in inbound}
            if want - have:
                c.put(f"/firewalls/{f['id']}", json={"name": NAME, "inbound_rules": inbound, "outbound_rules": f["outbound_rules"],
                                                     "droplet_ids": f.get("droplet_ids", []), "tags": f.get("tags", [])}).raise_for_status()
                print(f"firewall {NAME}: inbound tcp {sorted(want)} (was {sorted(have)})")
            return
    body = {
        "name": NAME,
        "inbound_rules": inbound,
        "outbound_rules": [
            {"protocol": "tcp", "ports": "all", "destinations": {"addresses": ["0.0.0.0/0", "::/0"]}},
            {"protocol": "udp", "ports": "all", "destinations": {"addresses": ["0.0.0.0/0", "::/0"]}},
            {"protocol": "icmp", "destinations": {"addresses": ["0.0.0.0/0", "::/0"]}},
        ],
        "droplet_ids": [droplet_id],
        "tags": [NAME],
    }
    c.post("/firewalls", json=body).raise_for_status()
    print(f"created firewall {NAME}: inbound 22 only")


def backup_policy(c: httpx.Client, droplet_id: int) -> dict:
    r = c.get(f"/droplets/{droplet_id}/backups/policy")
    r.raise_for_status()
    return r.json().get("policy", {})


def wait_action(c: httpx.Client, action: dict, what: str) -> None:
    for _ in range(60):
        status = action.get("status")
        if status == "completed":
            return
        if status == "errored":
            raise SystemExit(f"{what}: DigitalOcean action {action.get('id')} errored")
        time.sleep(5)
        action = c.get(f"/actions/{action['id']}").json()["action"]
    raise SystemExit(f"{what}: DigitalOcean action {action.get('id')} not completed in 5 minutes")


def ensure_backups(c: httpx.Client, droplet_id: int) -> dict:
    """Droplet backups on with BACKUP_POLICY: enable when off, change the plan or window when it differs."""
    p = backup_policy(c, droplet_id)
    have = p.get("backup_policy") or {}
    if not p.get("backup_enabled"):
        kind = "enable_backups"
    elif any(have.get(k) != v for k, v in BACKUP_POLICY.items()):
        kind = "change_backup_policy"
    else:
        return p
    r = c.post(f"/droplets/{droplet_id}/actions", json={"type": kind, "backup_policy": BACKUP_POLICY})
    r.raise_for_status()
    wait_action(c, r.json()["action"], kind)
    p = backup_policy(c, droplet_id)
    print(f"droplet backups: {kind} → {p.get('backup_policy')}, next window {p.get('next_backup_window')}")
    return p


def backups_summary(c: httpx.Client, droplet_id: int) -> dict:
    p = backup_policy(c, droplet_id)
    r = c.get(f"/droplets/{droplet_id}/backups", params={"per_page": 200})
    r.raise_for_status()
    images = r.json().get("backups", [])
    newest = max((i.get("created_at", "") for i in images), default=None)
    return {"enabled": p.get("backup_enabled"), "policy": p.get("backup_policy"), "next_window": p.get("next_backup_window"),
            "images": len(images), "newest_image": newest}


def main() -> int:
    load_dotenv()
    token = os.environ.get("DIGITALOCEAN_TOKEN")
    if not token:
        print("DIGITALOCEAN_TOKEN missing in .env", file=sys.stderr)
        return 2
    with api(token) as c:
        if "--status" in sys.argv:
            d = find_droplet(c)
            print("droplet:", {k: d.get(k) for k in ("id", "status", "size_slug")} | {"region": d["region"]["slug"], "ip": public_ipv4(d)}
                  if d else None)
            if d:
                print("backups:", backups_summary(c, int(d["id"])))
            return 0
        key_id = ensure_ssh_key(c, ensure_local_key())
        d = ensure_droplet(c, key_id)
        ensure_firewall(c, int(d["id"]), web="--web" in sys.argv)
        ensure_backups(c, int(d["id"]))
        ip = public_ipv4(d)
        HOST_FILE.parent.mkdir(parents=True, exist_ok=True)
        HOST_FILE.write_text(f"{ip}\n")
        print(f"droplet {NAME} active: ip={ip} region={d['region']['slug']} size={d['size_slug']} → {HOST_FILE}")
        print(f"ssh -i {SSH_KEY_PATH} root@{ip}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
