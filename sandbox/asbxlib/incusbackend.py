"""The Incus implementation of the sandbox backend (Linux hosts).

Everything Incus-specific that `up`, `lifecycle`, `doctor` and `build-base`
need lives here: instance creation, the per-NIC egress ACL and NIC pinning
(`netacl`), mount devices, readiness waits, snapshots, the base image alias,
metadata as `user.asbx.<key>` config keys, and the Incus rows of `doctor`.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from functools import partial
from typing import Callable, Optional

from . import config, guest, host, ingress, netacl, ui
from . import manifest as manifest_mod
from .backend import (ABSENT, META_EGRESS_MODE, RUNNING, STOPPED, Guest, SandboxSpec,
                      SshRoute, add_check)
from .errors import AsbxError
from .incus import Incus
from .manifest import Mount

BASE_BUILD_INSTANCE = "asbx-base-build"


class IncusGuest:
    """`backend.Guest` over `incus exec` and `incus file push`."""

    def __init__(self, incus: Incus, instance: str) -> None:
        self.incus = incus
        self.instance = instance

    def exec(self, argv: list[str], user: str, cwd: Optional[str] = None,
             env: Optional[dict[str, str]] = None, tty: bool = False,
             check: bool = True) -> subprocess.CompletedProcess:
        return self.incus.exec_in(self.instance, argv, user=user, cwd=cwd, env=env,
                                  tty=tty, check=check)

    def push_file(self, local: str, remote: str, mode: Optional[str] = None,
                  uid: Optional[int] = None, gid: Optional[int] = None) -> None:
        self.incus.file_push(self.instance, local, remote, mode=mode, uid=uid, gid=gid)

    def push_text(self, remote: str, body: str, mode: Optional[str] = None,
                  uid: Optional[int] = None, gid: Optional[int] = None) -> None:
        self.incus.push_text(self.instance, remote, body, mode=mode, uid=uid, gid=gid)

    def uid_of(self, user: str) -> int:
        return self.incus.uid_of(self.instance, user)


def _meta_key(key: str) -> str:
    return f"user.asbx.{key}"


def wait_for_agent(incus: Incus, instance: str, attempts: int = 60, interval: float = 2.0) -> None:
    last_stderr = ""
    for _ in range(attempts):
        proc = incus.run("exec", instance, "--", "true", check=False)
        if proc.returncode == 0:
            return
        # Early boot produces a mix of "VM agent isn't running", connection
        # refusals, and transient errors, so every failure is retried; the last
        # one is surfaced when the attempt budget runs out.
        last_stderr = (proc.stderr or "").strip()
        time.sleep(interval)
    suffix = f"\n{last_stderr}" if last_stderr else ""
    raise AsbxError(f"incus agent in {instance} did not come up in time{suffix}")


def _snapshot_copy_name(instance: str, label: str) -> str:
    return f"{instance}-snap-{label}"


class IncusBackend:
    """`backend.SandboxBackend` over the `incus` CLI."""

    name = "incus"

    def __init__(self, incus: Optional[Incus] = None) -> None:
        self.incus = incus or Incus()

    def guest(self, instance: str) -> Guest:
        return IncusGuest(self.incus, instance)

    def state(self, instance: str) -> str:
        status = self.incus.instance_state(instance)
        if status is None:
            return ABSENT
        return RUNNING if status == "Running" else STOPPED

    # -- host preparation ---------------------------------------------------

    def preflight(self, cfg: dict) -> None:
        # ADR 0005: once the ingress firewall is opted in, no sandbox runs without it.
        ingress.require_firewall(cfg)
        try:
            netacl.ensure_acl(self.incus, cfg)
        except AsbxError as e:
            ui.warn(f"could not refresh network ACL: {e}")

    def base_image(self, cfg: dict) -> Optional[str]:
        return self.incus.image_alias_exists(cfg["image_alias"])

    def build_base(self, cfg: dict, prepare: Callable[[Guest], None]) -> str:
        incus = self.incus
        b = BASE_BUILD_INSTANCE
        if incus.instance_exists(b):
            raise AsbxError(f"instance {b} already exists; delete it first: incus delete --force {b}")

        ui.ok(f"launching {b} from {cfg['base_image_source']}")
        resources = cfg["defaults"]["resources"]
        incus.run(
            "launch", cfg["base_image_source"], b, "--vm",
            "-c", f"limits.cpu={resources['cpu']}",
            "-c", f"limits.memory={resources['memory']}",
            "-d", f"root,size={resources['disk']}",
        )
        wait_for_agent(incus, b, attempts=150, interval=2.0)
        g = self.guest(b)
        guest.wait_for_cloud_init(g)
        try:
            prepare(g)
        except AsbxError as e:
            raise AsbxError(f"{e}; inspect it with 'incus shell {b}', "
                            f"then 'incus delete --force {b}'") from e

        ui.ok(f"stopping and publishing {b}")
        incus.run("stop", b)
        date_alias = f"{cfg['image_alias']}-{datetime.now(timezone.utc):%Y%m%d}"
        incus.image_alias_delete(date_alias)
        incus.run("publish", b, "--alias", date_alias, "--force")

        fingerprint = incus.image_alias_exists(date_alias)
        if not fingerprint:
            raise AsbxError(f"publish succeeded but alias {date_alias} not found")

        incus.image_alias_delete(cfg["image_alias"])
        incus.image_alias_create(cfg["image_alias"], fingerprint)
        incus.run("delete", b, "--force")
        ui.ok(f"base image ready: {cfg['image_alias']} -> {date_alias} ({fingerprint[:12]})")
        return fingerprint

    # -- instance lifecycle -------------------------------------------------

    def create(self, cfg: dict, spec: SandboxSpec) -> None:
        """No cloud-init user-data: `up` writes keys and hostname itself. The
        ACL and any kept address go on before the first start, so the guest's
        first packet is already filtered and its first lease already pinned."""
        ui.ok(f"creating {spec.instance}")
        self.incus.run(
            "create", cfg["image_alias"], spec.instance, "--vm",
            "-c", f"limits.cpu={spec.cpu}",
            "-c", f"limits.memory={spec.memory}",
            "-d", f"root,size={spec.disk}",
        )
        netacl.apply_nic_acl(self.incus, spec.instance, cfg)
        if spec.ipv4:
            netacl.pin_nic(self.incus, spec.instance, cfg, address=spec.ipv4)

    def _ensure_egress(self, cfg: dict, spec: SandboxSpec) -> None:
        """A first `up` that died between create and the ACL attach leaves an
        unfiltered instance, so the ACL is (re)applied rather than assumed."""
        mode = self.meta_get(spec.instance, META_EGRESS_MODE)
        if mode == "guest-nft":
            raise AsbxError(
                f"{spec.instance} was built with the removed in-guest firewall fallback, "
                f"which the agent can disable; run 'asbx rebuild {spec.group}'"
            )
        if mode != "acl":
            netacl.apply_nic_acl(self.incus, spec.instance, cfg)

    def _sync_mounts(self, mounts: list[Mount], instance: str) -> bool:
        """Makes the instance's `mnt-*` disk devices match `mounts`; True when
        devices changed."""
        desired: dict[str, dict[str, str]] = {}
        for i, mount in enumerate(mounts):
            host_path = config.expand(mount.host)
            if not host_path.exists():
                raise AsbxError(f"mount {mount.host}: host path does not exist")
            err = guest.mount_readable_error(host_path)
            if err:
                raise AsbxError(err)
            desired[f"mnt-{i}"] = {
                "type": "disk",
                "source": str(host_path),
                "path": mount.guest,
                "readonly": "true",
            }

        existing = {name: dev for name, dev in self.incus.devices(instance).items()
                    if name.startswith("mnt-")}
        if existing == desired:
            return False

        for name in existing:
            self.incus.device_remove(instance, name)
        for name, dev in desired.items():
            proc = self.incus.device_add(
                instance, name, "disk",
                f"source={dev['source']}", f"path={dev['path']}", "readonly=true",
            )
            if proc.returncode != 0:
                raise AsbxError(
                    f"mount {dev['source']} -> {dev['path']}: incus refused the device:\n"
                    f"{(proc.stderr or '').strip()}"
                )
        return True

    def apply(self, cfg: dict, spec: SandboxSpec) -> bool:
        self._ensure_egress(cfg, spec)
        return self._sync_mounts(spec.mounts, spec.instance)

    def _wait_ready(self, instance: str) -> None:
        ui.ok(f"waiting for {instance} agent")
        wait_for_agent(self.incus, instance)
        guest.wait_for_cloud_init(self.guest(instance))

    def start(self, cfg: dict, spec: SandboxSpec) -> None:
        self._ensure_egress(cfg, spec)
        if self.state(spec.instance) != RUNNING:
            ui.ok(f"starting {spec.instance}")
            self.incus.run("start", spec.instance)
        self._wait_ready(spec.instance)
        # Pinned to the address the guest holds now (or the one create pinned).
        pinned = netacl.pin_nic(self.incus, spec.instance, cfg)
        ui.ok(f"{spec.instance}: eth0 pinned to {pinned} with IP/MAC filtering")

    def stop(self, instance: str) -> None:
        self.incus.run("stop", instance)

    def restart(self, cfg: dict, spec: SandboxSpec) -> None:
        self._ensure_egress(cfg, spec)
        self.incus.run("restart", spec.instance)
        self._wait_ready(spec.instance)

    def delete(self, instance: str) -> None:
        self.incus.run("delete", instance, "--force", check=False)

    # -- addressing ---------------------------------------------------------

    def _hostname(self, cfg: dict, instance: str) -> str:
        """The name the host's resolver gives the instance: its Incus name under
        the bridge's DNS domain. Derived from config, never read back from the
        guest (ADR 0004)."""
        return f"{instance}.{cfg['network']['dns_domain']}"

    def ssh_route(self, cfg: dict, instance: str) -> SshRoute:
        name = self._hostname(cfg, instance)
        return SshRoute(host_key_alias=name, hostname=name)

    def ssh_address(self, cfg: dict, instance: str) -> Optional[str]:
        return self._hostname(cfg, instance)

    def pinned_ipv4(self, instance: str) -> Optional[str]:
        return netacl.pinned_ipv4(self.incus, instance)

    def bridge_subnet(self, cfg: dict):
        """The bridge's IPv4 subnet, for ingress (Incus-only)."""
        return netacl.bridge_subnet(self.incus, cfg)

    # -- snapshots ----------------------------------------------------------

    def snapshots(self, instance: str) -> list[str]:
        """Snapshot labels, plus labels kept as fallback copies."""
        labels = self.incus.snapshot_list(instance)
        prefix = _snapshot_copy_name(instance, "")
        copies = [n[len(prefix):] for n in self.incus.instance_names(prefix)]
        return labels + [c for c in copies if c and c not in labels]

    def snapshot(self, cfg: dict, spec: SandboxSpec, label: str) -> None:
        incus = self.incus
        instance = spec.instance
        if incus.storage_driver("default") == "dir":
            print("notice: dir storage pool; snapshot copies the full root disk", file=sys.stderr)

        proc = incus.snapshot_create(instance, label)
        if proc.returncode == 0:
            ui.ok(f"snapshot {instance}/{label} created")
            return

        if self.state(instance) == RUNNING:
            incus.run("stop", instance)
            proc = incus.snapshot_create(instance, label)
            incus.run("start", instance)
            if proc.returncode == 0:
                ui.ok(f"snapshot {instance}/{label} created (stopped for copy)")
                return

        ui.warn("pool driver does not support VM snapshots; falling back to incus copy")
        copy_name = _snapshot_copy_name(instance, label)
        incus.run("copy", instance, copy_name, "--instance-only")
        # Incus refuses to start a NIC whose static IPv4 another NIC already has,
        # stopped or not, so the copy must not keep the pin; restore puts it back.
        incus.run("config", "device", "unset", copy_name, "eth0", "ipv4.address", check=False)
        ui.ok(f"snapshot {instance}/{label} created via copy ({copy_name})")

    def restore(self, cfg: dict, spec: SandboxSpec, label: str) -> None:
        incus = self.incus
        instance = spec.instance
        # Resolve what `label` names BEFORE touching the instance: a snapshot,
        # a fallback copy, or nothing. The instance is only destroyed once the
        # copy it is to be rebuilt from is known to exist.
        from_snapshot = label in incus.snapshot_list(instance)
        copy_name = _snapshot_copy_name(instance, label)
        from_copy = not from_snapshot and incus.instance_exists(copy_name)
        if not from_snapshot and not from_copy:
            raise AsbxError(f"{instance}: no snapshot or copy named '{label}'")

        incus.run("stop", instance, check=False)
        if from_snapshot:
            proc = incus.snapshot_restore(instance, label)
            if proc.returncode != 0:
                raise AsbxError(f"{instance}: restoring snapshot '{label}' failed:\n{(proc.stderr or '').strip()}")
        else:
            ipv4 = netacl.pinned_ipv4(incus, instance)
            incus.run("delete", instance, "--force")
            incus.run("copy", copy_name, instance)
            if ipv4:
                netacl.pin_nic(incus, instance, cfg, address=ipv4)
        self.start(cfg, spec)

    # -- metadata -----------------------------------------------------------

    def meta_get(self, instance: str, key: str) -> str:
        return self.incus.config_get(instance, _meta_key(key))

    def meta_set(self, instance: str, key: str, value: str) -> None:
        self.incus.config_set(instance, _meta_key(key), value)

    # -- doctor -------------------------------------------------------------

    def doctor_checks(self, cfg: Optional[dict], fix: bool) -> list[tuple[str, str]]:
        incus = self.incus
        results: list[tuple[str, str]] = []
        check = partial(add_check, results)

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

        version = netacl.parse_incus_version(drivers_info) if server_ok else None
        if server_ok:
            check("Incus supports bridged-NIC ACLs",
                  version is not None and netacl.bridged_nic_acl_supported(version),
                  f"server_version {'.'.join(map(str, version)) if version else 'unreadable'}; need >= 6.0.4 "
                  "on the 6.0 LTS or >= 6.10 (7.0 LTS recommended); sandboxes cannot be filtered without it")

        if cfg is None:
            return results

        # ADR 0005: ineligible interfaces only warn here; `up` and `ingress setup` refuse them.
        interface_problems = ingress.interface_problems(cfg)
        for name, why in interface_problems.items():
            check(f"ingress interface {name}", False,
                  f"it {why}; 'asbx up' and 'asbx ingress setup' refuse it", warn_cond=True)
        if ingress.firewall_opted_in(cfg):
            firewall_ok = ingress.firewall_installed_matches(cfg) and ingress.firewall_unit_active()
            if not firewall_ok and fix and not interface_problems:
                try:
                    ingress.install(cfg, yes=False, print_only=False)
                except AsbxError as e:
                    ui.warn(f"--fix could not install the ingress firewall: {e}")
            fix_hint = "" if fix else "; run 'asbx ingress setup' (or doctor --fix)"
            check(f"{ingress.FIREWALL_PATH} matches config", ingress.firewall_installed_matches(cfg),
                  f"missing or out of date; 'asbx up' refuses until it matches{fix_hint}")
            check(f"{ingress.UNIT_NAME} active", ingress.firewall_unit_active(),
                  f"the inbound default-deny is not loaded at boot{fix_hint}")
        else:
            results.append(("PASS", "ingress firewall not installed (opt-in: 'asbx ingress setup')"))
        managed = ingress.managed_interface(cfg)
        if managed:
            check(f"wg-quick@{managed} active", ingress.unit_active(f"wg-quick@{managed}"),
                  "managed WireGuard is enabled but not running; run 'asbx ingress setup'", warn_cond=True)
            check("network.ingress.wireguard.endpoint set", bool(cfg["network"]["ingress"]["wireguard"]["endpoint"]),
                  "'asbx client-config' needs the host:port clients dial", warn_cond=True)

        if not (incus_on_path and server_ok):
            return results

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

        for group, instance in manifest_mod.manifest_groups(cfg):
            state = self.state(instance)
            if state == ABSENT:
                continue
            egress_mode = self.meta_get(instance, META_EGRESS_MODE)
            check(f"{instance}: egress ACL attached", egress_mode == "acl",
                  f"egress_mode={egress_mode or 'unset'}; "
                  + (f"run 'asbx rebuild {group}' (the in-guest firewall fallback is gone)"
                     if egress_mode == "guest-nft" else f"run 'asbx up {group}'"))
            check(f"{instance}: eth0 pinned with IP/MAC filtering", self.pinned_ipv4(instance) is not None,
                  f"anti-spoofing is off or the IPv4 isn't pinned; run 'asbx up {group}'")
            if state == RUNNING:
                hostname = self._hostname(cfg, instance)
                check(f"{instance}: {hostname} resolves on the host", host.resolves(hostname),
                      f"the host resolver does not know {hostname}; point systemd-resolved at the bridge: "
                      f"resolvectl dns {net_cfg['name']} <bridge ipv4 address> && "
                      f"resolvectl domain {net_cfg['name']} '~{net_cfg['dns_domain']}' "
                      "(see sandbox/docs/host-setup.md, 'Name resolution', to make it persistent)")
        return results
