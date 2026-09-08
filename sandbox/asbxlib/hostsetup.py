from __future__ import annotations

import os
from pathlib import Path

import yaml

from .errors import AsbxError
from .incus import Incus
from . import config
from . import host
from . import netacl
from . import sshconf
from . import ui

ENTRY_POINT = Path(__file__).resolve().parents[1] / "asbx"


def cmd_init(incus: Incus) -> None:
    if config.config_path().exists():
        print(str(config.config_path()))
        return

    config.config_dir().mkdir(parents=True, exist_ok=True)
    config.projects_dir().mkdir(parents=True, exist_ok=True)

    cfg = dict(config.DEFAULT_CONFIG)
    auth_key = ui.ask("Tailscale reusable auth key for tag:sandbox (blank to skip): ")
    cfg["tailscale"] = dict(cfg["tailscale"])
    cfg["tailscale"]["auth_key"] = auth_key

    host.write_private_text(config.config_path(), yaml.safe_dump(cfg, sort_keys=False))
    ui.ok(f"wrote {config.config_path()}")

    ssh_key = config.expand(cfg["ssh"]["key"])
    ssh_pub = Path(str(ssh_key) + ".pub")
    if not ssh_key.exists():
        ssh_key.parent.mkdir(parents=True, exist_ok=True)
        host.generate_ssh_key(ssh_key)
        ui.ok(f"generated {ssh_key}")

    try:
        netacl.ensure_acl(incus, cfg)
    except AsbxError as e:
        ui.warn(f"could not create network ACL yet: {e}")

    sshconf.ensure_ssh_include()

    config.expand(cfg["ssh"]["config_dir"]).mkdir(parents=True, exist_ok=True)
    ui.ok("asbx init complete")


def cmd_install(force: bool) -> None:
    if not ENTRY_POINT.is_file():
        raise AsbxError(f"entry point not found at {ENTRY_POINT}")
    target_dir = Path.home() / ".local" / "bin"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "asbx"
    if target.exists() or target.is_symlink():
        if not force:
            raise AsbxError(f"{target} already exists; pass --force to replace")
        target.unlink()
    target.symlink_to(ENTRY_POINT)
    ui.ok(f"linked {target} -> {ENTRY_POINT}")
    if str(target_dir) not in os.environ.get("PATH", "").split(os.pathsep):
        ui.warn(f"{target_dir} is not on PATH")
