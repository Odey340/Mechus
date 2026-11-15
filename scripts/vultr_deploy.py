#!/usr/bin/env python3
"""
Provision a Vultr Cloud Compute server for Koov and wire it to `./run.sh deploy`.

Develop locally, deploy globally:
  1. Sign up (mlh.link/vultr-signup), then create an API key: Vultr console -> Account -> API.
  2. Put it in .env:          VULTR_API_KEY=...
  3. Create the server:       python scripts/vultr_deploy.py create --ssh-pubkey ~/.ssh/id_ed25519.pub
  4. Ship the code:           ./run.sh deploy        (uses the .deploy.env this script writes)

Other commands:
  python scripts/vultr_deploy.py regions            list regions (default: dfw = Dallas)
  python scripts/vultr_deploy.py plans [--gpu]      list plans (cloud compute, or Cloud GPU with --gpu)
  python scripts/vultr_deploy.py list               list your instances
  python scripts/vultr_deploy.py status <id>        show one instance
  python scripts/vultr_deploy.py destroy <id>       delete an instance (stops billing)

Standard library only, so it also runs on Windows without extra installs.
API reference: https://www.vultr.com/api/
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.vultr.com/v2"
REPO_ROOT = Path(__file__).resolve().parents[1]
CLOUD_INIT = REPO_ROOT / "deploy" / "vultr" / "cloud-init.yaml"
UBUNTU_2404_OS_ID = 2284


def _load_dotenv() -> None:
    for name in (".env.local", ".env"):  # .env.local (gitignored secrets) wins
        env = REPO_ROOT / name
        if not env.exists():
            continue
        for raw in env.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _api(method: str, path: str, body: dict | None = None) -> dict:
    key = os.environ.get("VULTR_API_KEY", "").strip()
    if not key:
        sys.exit("VULTR_API_KEY is not set (add it to .env or export it). Create one at https://my.vultr.com/settings/#settingsapi")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{API}{path}",
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        sys.exit(f"Vultr API {method} {path} failed: HTTP {exc.code}: {detail}")


def _ensure_ssh_key(pubkey_path: str | None) -> list[str]:
    if not pubkey_path:
        return []
    pub = Path(os.path.expanduser(pubkey_path)).read_text(encoding="utf-8").strip()
    for k in _api("GET", "/ssh-keys?per_page=500").get("ssh_keys", []):
        if k.get("ssh_key", "").strip() == pub:
            return [k["id"]]
    created = _api("POST", "/ssh-keys", {"name": "koov-deploy", "ssh_key": pub})
    return [created["ssh_key"]["id"]]


def cmd_create(args: argparse.Namespace) -> None:
    user_data = base64.b64encode(CLOUD_INIT.read_bytes()).decode()
    body = {
        "region": args.region,
        "plan": args.plan,
        "os_id": args.os_id,
        "label": args.label,
        "hostname": args.label,
        "user_data": user_data,
        "enable_ipv6": True,
        "tags": ["koov", "hackuta"],
    }
    ssh_ids = _ensure_ssh_key(args.ssh_pubkey)
    if ssh_ids:
        body["sshkey_id"] = ssh_ids
    inst = _api("POST", "/instances", body)["instance"]
    iid = inst["id"]
    print(f"Created instance {iid} ({args.plan} in {args.region}). Waiting for it to boot...")
    if inst.get("default_password") and not ssh_ids:
        print(f"Root password (shown once, store it safely): {inst['default_password']}")

    ip = "0.0.0.0"
    for _ in range(90):
        time.sleep(10)
        cur = _api("GET", f"/instances/{iid}")["instance"]
        ip = cur.get("main_ip") or ip
        print(f"  status={cur.get('status')} power={cur.get('power_status')} ip={ip}")
        if cur.get("status") == "active" and cur.get("power_status") == "running" and ip != "0.0.0.0":
            break
    else:
        sys.exit("Timed out waiting for the instance; check https://my.vultr.com/")

    lines = [f"DEPLOY_HOST=root@{ip}", f"VULTR_INSTANCE_ID={iid}"]
    if args.ssh_pubkey:
        priv = os.path.expanduser(args.ssh_pubkey)
        if priv.endswith(".pub"):
            priv = priv[: -len(".pub")]
        lines.insert(1, f"DEPLOY_SSH_KEY={priv}")
    deploy_env = REPO_ROOT / ".deploy.env"
    if deploy_env.exists():
        deploy_env.replace(REPO_ROOT / ".deploy.env.bak")
        print("Saved your previous .deploy.env as .deploy.env.bak")
    deploy_env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\nWrote .deploy.env:")
    print("\n".join("  " + l for l in lines))
    print(
        "\nCloud-init is installing Docker (about 2-3 minutes). Then run:\n"
        "  ./run.sh deploy\n"
        f"API will be at http://{ip}:5001/health"
    )


def cmd_regions(_: argparse.Namespace) -> None:
    for r in _api("GET", "/regions?per_page=500").get("regions", []):
        print(f"{r['id']:6} {r.get('city', '')}, {r.get('country', '')}")


def cmd_plans(args: argparse.Namespace) -> None:
    ptype = "vcg" if args.gpu else "vc2"
    plans = _api("GET", f"/plans?type={ptype}&per_page=500").get("plans", [])
    for p in sorted(plans, key=lambda p: p.get("monthly_cost", 0)):
        regions = p.get("locations") or []
        here = "yes" if args.region in regions else "no"
        gpu = f" gpu={p.get('gpu_type')} vram={p.get('gpu_vram_gb')}GB" if args.gpu else ""
        print(f"{p['id']:28} {p.get('vcpu_count')} vCPU  {p.get('ram')} MB RAM  "
              f"${p.get('monthly_cost')}/mo  in {args.region}: {here}{gpu}")


def cmd_list(_: argparse.Namespace) -> None:
    for i in _api("GET", "/instances?per_page=500").get("instances", []):
        print(f"{i['id']}  {i.get('label'):20} {i.get('main_ip'):16} {i.get('region')}  {i.get('plan')}  {i.get('status')}")


def cmd_status(args: argparse.Namespace) -> None:
    print(json.dumps(_api("GET", f"/instances/{args.instance_id}")["instance"], indent=2))


def cmd_destroy(args: argparse.Namespace) -> None:
    _api("DELETE", f"/instances/{args.instance_id}")
    print(f"Deleted instance {args.instance_id}")


def main() -> None:
    _load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", help="create a Koov server")
    c.add_argument("--region", default=os.environ.get("VULTR_REGION", "dfw"))
    c.add_argument("--plan", default=os.environ.get("VULTR_PLAN", "vc2-2c-4gb"),
                   help="vc2-2c-4gb fits the API + Docker build; see `plans` (use a vcg-* plan for Cloud GPU)")
    c.add_argument("--os-id", type=int, default=UBUNTU_2404_OS_ID, help="2284 = Ubuntu 24.04 LTS x64")
    c.add_argument("--label", default=os.environ.get("VULTR_LABEL", "koov-hackuta"))
    c.add_argument("--ssh-pubkey", default=os.environ.get("VULTR_SSH_PUBKEY"),
                   help="path to your public key, e.g. ~/.ssh/id_ed25519.pub (recommended)")
    c.set_defaults(func=cmd_create)

    sub.add_parser("regions").set_defaults(func=cmd_regions)
    p = sub.add_parser("plans")
    p.add_argument("--gpu", action="store_true", help="list Cloud GPU plans")
    p.add_argument("--region", default=os.environ.get("VULTR_REGION", "dfw"))
    p.set_defaults(func=cmd_plans)
    sub.add_parser("list").set_defaults(func=cmd_list)
    s = sub.add_parser("status")
    s.add_argument("instance_id")
    s.set_defaults(func=cmd_status)
    d = sub.add_parser("destroy")
    d.add_argument("instance_id")
    d.set_defaults(func=cmd_destroy)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
