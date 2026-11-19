from __future__ import annotations

import shlex
from pathlib import Path
from typing import Optional

from .backend import ABSENT, SandboxBackend, SshRoute
from .errors import AsbxError
from . import config
from . import host
from . import manifest as manifest_mod
from . import ui


def render_ssh_config_block(instance: str, route: SshRoute, ssh_key: str, known_hosts: str,
                            guest_user: str) -> str:
    """The host's stanza. `HostKeyAlias` keeps the known-hosts entry stable
    when the route is a ProxyCommand or the address changes."""
    reach = (f"    HostName {route.hostname}\n" if route.hostname
             else f"    ProxyCommand {route.proxy_command}\n")
    return (
        f"# managed by asbx — do not edit\n"
        f"Host {instance}\n"
        + reach
        + f"    HostKeyAlias {route.host_key_alias}\n"
        f"    User {guest_user}\n"
        f"    IdentityFile {ssh_key}\n"
        f"    IdentitiesOnly yes\n"
        f"    StrictHostKeyChecking accept-new\n"
        f"    UserKnownHostsFile {known_hosts}\n"
    )


def render_remote_stanza(instance: str, route: SshRoute, guest_user: str, jump: Optional[str],
                         identity: str) -> str:
    """Host block for a machine that is not the sandbox host. A literal alias, not
    a `sandbox-*` wildcard: VSCode's host list shows only literal Host names, and
    adds its own bare-name entry on connect when it finds none. Through `jump`,
    a `hostname` route is a ProxyJump, and a `proxy_command` route runs its
    command on the jump host over ssh. With no `jump`, `route.hostname` is the
    sandbox's pinned IP, reached over an ingress interface."""
    if route.hostname:
        reach = (f"    HostName {route.hostname}\n"
                 f"    User {guest_user}\n"
                 + (f"    ProxyJump {jump}\n" if jump else ""))
    else:
        # Quoted once for the local shell that runs ProxyCommand, so the jump
        # host's shell gets the route's own quoting intact.
        reach = (f"    ProxyCommand ssh {shlex.quote(jump)} {shlex.quote(route.proxy_command)}\n"
                 f"    User {guest_user}\n")
    return (
        f"Host {instance}\n"
        + reach
        + f"    IdentityFile {identity}\n"
        f"    IdentitiesOnly yes\n"
        f"    StrictHostKeyChecking no\n"
        f"    UserKnownHostsFile /dev/null\n"
        f"    LogLevel ERROR\n"
    )


def cmd_ssh_config(backend: SandboxBackend, cfg: dict, group: str, jump: Optional[str],
                   identity: str) -> None:
    """`jump` None means --direct: the pinned IPv4 comes from host-side backend
    config, never the guest."""
    instance = manifest_mod.load_manifest(group, cfg).instance
    if jump:
        route = backend.ssh_route(cfg, instance)
    else:
        pinned = backend.pinned_ipv4(instance) if backend.state(instance) != ABSENT else None
        if not pinned:
            raise AsbxError(f"{instance} has no pinned IPv4; --direct needs a backend that pins "
                            f"sandbox addresses (Incus) and 'asbx up {group}' to have run")
        route = SshRoute(host_key_alias=instance, hostname=pinned)
    print(render_remote_stanza(instance, route, cfg["guest_user"], jump, identity), end="")


def config_file(cfg: dict, instance: str) -> Path:
    return config.expand(cfg["ssh"]["config_dir"]) / f"{instance}.conf"


def remove_ssh_config(cfg: dict, instance: str) -> None:
    path = config_file(cfg, instance)
    if path.exists():
        path.unlink()


def write_ssh_config(cfg: dict, instance: str, route: SshRoute) -> None:
    """Built from `route` alone, which backends derive from host-side data,
    so nothing the guest reports reaches the host's ssh config (ADR 0004)."""
    known_hosts = config.expand(cfg["ssh"]["known_hosts"])
    host.forget_host_key(known_hosts, route.host_key_alias)

    conf_path = config_file(cfg, instance)
    conf_path.parent.mkdir(parents=True, exist_ok=True)
    ssh_key = config.expand(cfg["ssh"]["key"])
    block = render_ssh_config_block(instance, route, str(ssh_key), str(known_hosts),
                                    cfg["guest_user"])
    conf_path.write_text(block)
    ui.ok(f"wrote {conf_path}")


def include_line(cfg: dict) -> str:
    return f"Include {cfg['ssh']['config_dir']}/*.conf"


def user_ssh_config() -> Path:
    return Path.home() / ".ssh" / "config"


def include_is_first(text: str, line: str) -> bool:
    """True when `line` is the first directive. OpenSSH takes the first value it
    finds for each option, so a lower Include loses to any earlier Host block."""
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped and not stripped.startswith("#"):
            return stripped == line
    return False


def ensure_ssh_include(cfg: dict, ssh_config: Path | None = None) -> None:
    """Puts the asbx Include line first in ~/.ssh/config, moving it up if it
    exists lower down."""
    ssh_config = ssh_config or user_ssh_config()
    line = include_line(cfg)
    if not ssh_config.exists():
        ssh_config.parent.mkdir(parents=True, exist_ok=True)
        host.write_private_text(ssh_config, line + "\n")
        ui.ok(f"created {ssh_config}")
        return
    content = ssh_config.read_text()
    if include_is_first(content, line):
        return
    rest = "\n".join(l for l in content.splitlines() if l.strip() != line)
    ssh_config.write_text(line + "\n\n" + rest.lstrip("\n") + ("\n" if rest else ""))
    ui.ok(f"put the Include line first in {ssh_config}")
