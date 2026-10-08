from __future__ import annotations

from pathlib import Path

from . import config
from . import host
from . import manifest as manifest_mod
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


def render_remote_stanza(instance: str, hostname: str, guest_user: str, jump: str,
                         identity: str) -> str:
    """Host block for a machine that is not the Incus host. A literal alias, not
    a `sandbox-*` wildcard: VSCode's host list shows only literal Host names, and
    adds its own bare-name entry on connect when it finds none."""
    return (
        f"Host {instance}\n"
        f"    HostName {hostname}\n"
        f"    User {guest_user}\n"
        f"    ProxyJump {jump}\n"
        f"    IdentityFile {identity}\n"
        f"    IdentitiesOnly yes\n"
        f"    StrictHostKeyChecking no\n"
        f"    UserKnownHostsFile /dev/null\n"
        f"    LogLevel ERROR\n"
    )


def cmd_ssh_config(cfg: dict, group: str, jump: str, identity: str) -> None:
    instance = manifest_mod.load_manifest(group, cfg).instance
    print(render_remote_stanza(instance, instance_hostname(instance, cfg),
                               cfg["guest_user"], jump, identity), end="")


def config_file(cfg: dict, instance: str) -> Path:
    return config.expand(cfg["ssh"]["config_dir"]) / f"{instance}.conf"


def remove_ssh_config(cfg: dict, instance: str) -> None:
    path = config_file(cfg, instance)
    if path.exists():
        path.unlink()


def instance_hostname(instance: str, cfg: dict) -> str:
    """The name the host's resolver gives the instance: its Incus name under
    the bridge's DNS domain. Derived from config, never read back from the
    guest, so nothing the agent controls reaches the generated SSH config."""
    return f"{instance}.{cfg['network']['dns_domain']}"


def write_ssh_config(instance: str, cfg: dict) -> str:
    hostname = instance_hostname(instance, cfg)

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
