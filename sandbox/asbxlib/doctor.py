from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from .errors import AsbxError
from .incus import Incus
from . import ui
from . import host
from . import config
from . import guest
from . import sshconf
from . import netacl
from . import manifest as manifest_mod


def doctor_mounts(cfg: dict) -> list[manifest_mod.Mount]:
    """Every mount `up` would attach for any manifest, deduplicated by guest
    path, so doctor checks what the sandboxes actually use rather than only
    the config defaults."""
    by_guest: dict[str, manifest_mod.Mount] = {
        m.guest: m for m in [manifest_mod.Mount.from_dict(d) for d in cfg["defaults"]["mounts"]]
    }
    for group, _instance in manifest_mod.manifest_groups(cfg):
        try:
            loaded = manifest_mod.load_manifest(group, cfg)
        except AsbxError:
            continue
        for mount in loaded.mounts:
            by_guest[mount.guest] = mount
    return list(by_guest.values())


def cmd_doctor(incus: Incus, cfg: Optional[dict], fix: bool) -> int:
    results: list[tuple[str, str]] = []  # (PASS/FAIL/WARN, message)

    def check(label, ok_cond, fail_msg, warn_cond=False):
        if ok_cond:
            results.append(("PASS", label))
        elif warn_cond:
            results.append(("WARN", f"{label}: {fail_msg}"))
        else:
            results.append(("FAIL", f"{label}: {fail_msg}"))

    incus_on_path = host.which("incus")
    check("incus on PATH", incus_on_path, "not found")

    server_ok = False
    drivers_info = ""
    if incus_on_path:
        proc = incus.info()
        server_ok = proc.returncode == 0
        drivers_info = proc.stdout
    check("incus server reachable", server_ok, "incus info failed")
    check("qemu driver available", "qemu" in drivers_info.lower(), "qemu driver not listed in incus info")

    kvm_ok = os.access("/dev/kvm", os.R_OK | os.W_OK)
    check("/dev/kvm readable", kvm_ok, "/dev/kvm not accessible")

    check("PyYAML importable", True, "")  # we're already running with yaml imported

    ts_on_path = host.which("tailscale")
    check("tailscale on PATH", ts_on_path, "not found")
    ts_running = False
    if ts_on_path:
        status = host.tailscale_status()
        ts_running = bool(status) and status.get("BackendState") == "Running"
    check("tailscale backend Running", ts_running, "backend not Running")

    if cfg is None:
        check("config present", False, f"no config at {config.config_path()}; run 'asbx init' first")
        for status, msg in results:
            print(f"{status:4} {msg}")
        return 1 if any(s == "FAIL" for s, _ in results) else 0

    check("tailscale.auth_key set", bool(cfg["tailscale"]["auth_key"]), "empty in config")

    ssh_key = config.expand(cfg["ssh"]["key"])
    ssh_pub = Path(str(ssh_key) + ".pub")
    check("ssh key exists", ssh_key.exists() and ssh_pub.exists(), f"missing {ssh_key} or {ssh_pub}")

    ssh_config = Path.home() / ".ssh" / "config"
    include_present = False
    if ssh_config.exists():
        first_lines = ssh_config.read_text().splitlines()[:5]
        include_present = any("agent-sandbox.d" in l for l in first_lines)
    if not include_present and fix:
        sshconf.ensure_ssh_include()
        include_present = True
    check("Include line in ~/.ssh/config", include_present, "missing or not near top")

    if incus_on_path and server_ok:
        fingerprint = incus.image_alias_exists(cfg["image_alias"])
        check("base image alias exists", bool(fingerprint), f"{cfg['image_alias']} not found; run 'asbx build-base'")

        net_cfg = cfg["network"]
        acl_exists = incus.network_acl_exists(net_cfg["acl"])
        if not acl_exists and fix:
            try:
                netacl.ensure_acl(incus, cfg)
                acl_exists = incus.network_acl_exists(net_cfg["acl"])
            except AsbxError as e:
                ui.warn(f"--fix could not create the ACL: {e}")
        check("network ACL exists", acl_exists, f"{net_cfg['acl']} not found"
              + ("" if fix else "; re-run with --fix to create it"))
        if acl_exists:
            matches = netacl.acl_matches_config(incus, cfg)
            if not matches and fix:
                try:
                    netacl.ensure_acl(incus, cfg)
                    matches = netacl.acl_matches_config(incus, cfg)
                except AsbxError as e:
                    ui.warn(f"--fix could not update the ACL: {e}")
            check("ACL rule set matches computed drop sets", matches,
                  "rule set differs from expected"
                  + ("" if fix else "; re-run with --fix to rewrite it"),
                  warn_cond=not fix)

        driver = incus.storage_driver("default")
        driver_dir = driver == "dir"
        check("storage pool driver", not driver_dir,
              "dir pool: snapshots are full root-disk copies", warn_cond=driver_dir)

    for mount in doctor_mounts(cfg):
        host_path = config.expand(mount.host)
        if not host_path.exists():
            check(f"mount {mount.host}", False, "host path does not exist")
            continue
        err = guest.mount_readable_error(host_path)
        check(f"mount {mount.host}", err is None, err or "")

    for group, instance in manifest_mod.manifest_groups(cfg):
        exists = incus_on_path and server_ok and incus.instance_exists(instance)
        state = incus.instance_state(instance) if exists else "absent"
        egress_mode = incus.config_get(instance, "user.asbx.egress_mode") if exists else "n/a"
        results.append(("PASS", f"manifest {group}: instance {instance} state={state} egress_mode={egress_mode}"))

    for status, msg in results:
        print(f"{status:4} {msg}")
    return 1 if any(s == "FAIL" for s, _ in results) else 0
