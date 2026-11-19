from __future__ import annotations

from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Optional

from .backend import ABSENT, META_EGRESS_MODE, RUNNING, Guest, SandboxBackend, add_check
from .errors import AsbxError
from . import awscreds
from . import config
from . import guest
from . import host
from . import sshconf
from . import netcheck
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


def aws_credential_results(g: Guest, manifest: manifest_mod.Manifest, group: str,
                           guest_user: str, now: datetime) -> list[tuple[str, str]]:
    """One (status, message) per allowed profile, from the credential file the
    guest holds for it."""
    results = []
    for profile in manifest.aws_profiles:
        proc = g.exec(["cat", awscreds.guest_creds_file(guest_user, profile)],
                      user=guest_user, check=False)
        if proc.returncode != 0:
            status, msg = "FAIL", "missing"
        else:
            status, msg = awscreds.credential_status(proc.stdout, now)
        if status != "PASS":
            msg += (f"; run 'asbx aws-refresh {group}'; "
                    "if that fails, run 'aws sso login' on the host")
        results.append((status, f"{manifest.instance}: aws profile {profile}: {msg}"))
    return results


def cmd_doctor(backend: SandboxBackend, cfg: Optional[dict], fix: bool) -> int:
    results: list[tuple[str, str]] = []  # (PASS/FAIL/WARN, message)
    check = partial(add_check, results)

    results.append(("PASS", f"backend {backend.name}"))
    check("PyYAML importable", True, "")  # we're already running with yaml imported
    results.extend(backend.doctor_checks(cfg, fix))

    if cfg is None:
        check("config present", False, f"no config at {config.config_path()}; run 'asbx init' first")
        for status, msg in results:
            print(f"{status:4} {msg}")
        return 1 if any(s == "FAIL" for s, _ in results) else 0

    ssh_key = config.expand(cfg["ssh"]["key"])
    ssh_pub = Path(str(ssh_key) + ".pub")
    check("ssh key exists", ssh_key.exists() and ssh_pub.exists(), f"missing {ssh_key} or {ssh_pub}")

    ssh_config = sshconf.user_ssh_config()
    include_present = ssh_config.exists() and sshconf.include_is_first(
        ssh_config.read_text(), sshconf.include_line(cfg))
    if not include_present and fix:
        sshconf.ensure_ssh_include(cfg)
        include_present = True
    check("Include line in ~/.ssh/config", include_present, "missing or not the first directive")

    for mount in doctor_mounts(cfg):
        host_path = config.expand(mount.host)
        if not host_path.exists():
            check(f"mount {mount.host}", False, "host path does not exist")
            continue
        err = guest.mount_readable_error(host_path)
        check(f"mount {mount.host}", err is None, err or "")

    aws_manifests = dict(awscreds.aws_targets(cfg, None))
    if aws_manifests:
        check("aws CLI on PATH", host.which("aws"), "needed by the aws-creds provider")
        for label, active in awscreds.refresh_job_status():
            check(label, active, "run 'asbx aws-refresh --install-timer'", warn_cond=True)

    for group, instance in manifest_mod.manifest_groups(cfg):
        try:
            state = backend.state(instance)
        except AsbxError:
            state = "unknown"
        exists = state not in (ABSENT, "unknown")
        egress_mode = backend.meta_get(instance, META_EGRESS_MODE) if exists else "n/a"
        results.append(("PASS", f"manifest {group}: instance {instance} state={state} egress_mode={egress_mode}"))

        if state == RUNNING:
            g = backend.guest(instance)
            has_route = netcheck.guest_has_ipv4_default_route(g)
            check(f"{instance}: guest has IPv4 default route", has_route,
                  "no default IPv4 route in the guest; DHCP likely never completed "
                  "(check whatever filters the sandbox network's inbound DHCP traffic, e.g. a "
                  "host firewall's default-deny policy on port 67/udp)")
            if has_route:
                has_egress = netcheck.guest_has_ipv4_egress(g)
                check(f"{instance}: guest IPv4 internet egress", has_egress,
                      "TCP handshake to api.github.com:443 timed out; check whatever "
                      "filters forwarded/routed traffic from the sandbox network to your "
                      "uplink (e.g. a host firewall's default-deny FORWARD policy)")

            if group in aws_manifests:
                results.extend(aws_credential_results(
                    g, aws_manifests[group], group, cfg["guest_user"], datetime.now(timezone.utc)))

    for status, msg in results:
        print(f"{status:4} {msg}")
    return 1 if any(s == "FAIL" for s, _ in results) else 0
