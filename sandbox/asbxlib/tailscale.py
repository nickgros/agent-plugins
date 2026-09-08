from __future__ import annotations

import json
import sys
from typing import Optional

from .errors import AsbxError
from .incus import Incus
from . import ui


def tailscale_status(incus: Incus, instance: str) -> Optional[dict]:
    proc = incus.run("exec", instance, "--", "tailscale", "status", "--json", check=False)
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


TAILNET_POLICY_SNIPPET = """
  "tagOwners": { "tag:sandbox": ["autogroup:admin"] },
  "grants": [
    { "src": ["autogroup:member"], "dst": ["autogroup:member"], "ip": ["*"] },
    { "src": ["autogroup:member"], "dst": ["tag:sandbox"], "ip": ["tcp:22"] }
  ],
  "tests": [
    { "src": "you@example.com", "accept": ["tag:sandbox:22"] },
    { "src": "tag:sandbox", "deny": ["tag:sandbox:22"] }
  ]
"""


def run_tailscale_up(incus: Incus, instance: str, cfg: dict) -> None:
    status = tailscale_status(incus, instance)
    if status and status.get("BackendState") == "Running":
        ui.ok(f"{instance}: tailscale already running")
        return

    auth_key = cfg["tailscale"]["auth_key"]
    if not auth_key:
        ui.warn("tailscale.auth_key empty; instance reachable only via 'asbx shell'")
        return

    tags = ",".join(cfg["tailscale"]["tags"])
    proc = incus.exec_in(
        instance,
        ["tailscale", "up", "--authkey", auth_key, "--hostname", instance,
         "--advertise-tags", tags, "--ssh=false", "--accept-dns=false"],
        user="root", tty=True, check=False,
    )
    if proc.returncode != 0:
        print(TAILNET_POLICY_SNIPPET, file=sys.stderr)
        raise AsbxError(f"tailscale up failed in {instance}; tailnet policy snippet printed above")
