from __future__ import annotations

from pathlib import Path
from typing import Optional

from .errors import AsbxError
from .incus import Incus
from . import config
from . import host
from . import tailscale
from . import ui


def render_ssh_config_block(instance: str, hostname: str, ssh_key: str, known_hosts: str,
                            guest_user: str) -> str:
    return (
        f"# managed by asbx — do not edit\n"
        f"Host {instance}\n"
        f"    HostName {hostname}\n"
        f"    User {guest_user}\n"
        f"    IdentityFile {ssh_key}\n"
        f"    IdentitiesOnly yes\n"
        f"    StrictHostKeyChecking accept-new\n"
        f"    UserKnownHostsFile {known_hosts}\n"
    )


def config_file(cfg: dict, instance: str) -> Path:
    return config.expand(cfg["ssh"]["config_dir"]) / f"{instance}.conf"


def remove_ssh_config(cfg: dict, instance: str) -> None:
    path = config_file(cfg, instance)
    if path.exists():
        path.unlink()


def write_ssh_config(incus: Incus, instance: str, cfg: dict) -> Optional[str]:
    status = tailscale.tailscale_status(incus, instance)
    if not status:
        return None
    self_info = status.get("Self", {})
    hostname = self_info.get("DNSName", "").rstrip(".")
    if not hostname:
        ips = self_info.get("TailscaleIPs") or []
        if not ips:
            return None
        hostname = ips[0]

    known_hosts = config.expand(cfg["ssh"]["known_hosts"])
    host.forget_host_key(known_hosts, hostname)

    conf_path = config_file(cfg, instance)
    conf_path.parent.mkdir(parents=True, exist_ok=True)
    ssh_key = config.expand(cfg["ssh"]["key"])
    block = render_ssh_config_block(instance, hostname, str(ssh_key), str(known_hosts),
                                    cfg["guest_user"])
    conf_path.write_text(block)
    ui.ok(f"wrote {conf_path}")
    return hostname


def ensure_ssh_include() -> None:
    """Prepends the asbx Include line to ~/.ssh/config. It has to be the first
    line: OpenSSH takes the first matching value for each option, so a later
    Include would lose to any earlier Host block."""
    ssh_config = Path.home() / ".ssh" / "config"
    include_line = "Include ~/.ssh/agent-sandbox.d/*.conf"
    if ssh_config.exists():
        content = ssh_config.read_text()
        if include_line in content.splitlines():
            return
        ssh_config.write_text(include_line + "\n\n" + content)
        ui.ok(f"prepended Include line to {ssh_config}")
        return
    ssh_config.parent.mkdir(parents=True, exist_ok=True)
    host.write_private_text(ssh_config, include_line + "\n")
    ui.ok(f"created {ssh_config}")
