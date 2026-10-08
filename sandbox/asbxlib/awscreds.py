"""Host-minted AWS credentials pushed into sandboxes.

The host logs in to SSO and exports short-lived credentials for the allowed
profiles (`Manifest.aws_profiles`); this module pushes them into the guest as
`credential_process` files. The guest never holds an SSO token or any profile
outside that set, and there is no guest-to-host channel. A systemd user timer
on the host re-pushes them before they expire.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from . import config
from . import host
from . import hostsetup
from . import manifest
from . import ui
from .errors import AsbxError
from .incus import Incus

PROVIDER = "aws-creds"
TIMER_UNIT = "asbx-aws-refresh.timer"
SERVICE_UNIT = "asbx-aws-refresh.service"
WARN_BELOW = timedelta(minutes=15)

_REQUIRED_FIELDS = ("AccessKeyId", "SecretAccessKey", "SessionToken", "Expiration")


def guest_creds_dir(guest_user: str) -> str:
    return f"/home/{guest_user}/.asbx/aws"


def guest_creds_file(guest_user: str, profile: str) -> str:
    return f"{guest_creds_dir(guest_user)}/{profile}.json"


def parse_exported(profile: str, stdout: str) -> dict:
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict) or data.get("Version") != 1:
        raise AsbxError(f"aws profile {profile}: unreadable export-credentials output")
    if not data.get("AccessKeyId") or not data.get("SecretAccessKey"):
        raise AsbxError(f"aws profile {profile}: unreadable export-credentials output")
    if not data.get("SessionToken") or not data.get("Expiration"):
        raise AsbxError(f"aws profile {profile}: yields long-term credentials; "
                        f"only temporary credentials are pushed to sandboxes")
    return {k: data[k] for k in ("Version", *_REQUIRED_FIELDS)}


def export_profile(profile: str) -> tuple[str, str]:
    """(credential_process JSON, region) for a host profile."""
    proc = host.aws_export_credentials(profile)
    if proc.returncode != 0:
        raise AsbxError(
            f"aws profile {profile}: could not export credentials on the host; "
            f"if the SSO session expired, run 'aws sso login' for it on the host:\n"
            f"{(proc.stderr or '').strip()}")
    return json.dumps(parse_exported(profile, proc.stdout)), host.aws_profile_region(profile)


def render_guest_aws_config(guest_user: str, regions: dict[str, str]) -> str:
    lines = ["# managed by asbx: rewritten by 'asbx auth' and 'asbx aws-refresh'"]
    for name, region in regions.items():
        lines.append("")
        lines.append("[default]" if name == "default" else f"[profile {name}]")
        lines.append(f"credential_process = cat {guest_creds_file(guest_user, name)}")
        if region:
            lines.append(f"region = {region}")
    return "\n".join(lines) + "\n"


def push(incus: Incus, instance: str, guest_user: str, creds: dict[str, tuple[str, str]]) -> None:
    """Writes each profile's credentials into the guest, replaces the guest's
    `~/.aws/config`, and removes credential files for profiles not in `creds`."""
    uid = incus.uid_of(instance, guest_user)
    home = f"/home/{guest_user}"
    aws_dir = guest_creds_dir(guest_user)
    incus.exec_in(instance, ["install", "-d", "-m", "0700", "-o", str(uid), "-g", str(uid),
                              f"{home}/.asbx", aws_dir, f"{home}/.aws"], user="root")
    for profile, (body, _region) in creds.items():
        tmp = f"{aws_dir}/{profile}.json.tmp"
        incus.push_text(instance, tmp, body, mode="0600", uid=uid, gid=uid)
        # Atomic rename: `cat` in credential_process never sees a half-written file.
        incus.exec_in(instance, ["mv", "-f", tmp, guest_creds_file(guest_user, profile)], user=guest_user)
    regions = {p: region for p, (_body, region) in creds.items()}
    incus.push_text(instance, f"{home}/.aws/config", render_guest_aws_config(guest_user, regions),
                    mode="0600", uid=uid, gid=uid)
    prune = ["find", aws_dir, "-maxdepth", "1", "-type", "f", "-name", "*.json"]
    for profile in creds:
        prune += ["!", "-name", f"{profile}.json"]
    prune.append("-delete")
    incus.exec_in(instance, prune, user=guest_user)


def aws_targets(cfg: dict, group: Optional[str]) -> list[tuple[str, manifest.Manifest]]:
    """(group, manifest) for each manifest that uses the aws-creds provider.
    With `group`, only that one, and it must use the provider. Without, every
    manifest on disk; one that fails to load is warned about and skipped."""
    if group:
        loaded = manifest.load_manifest(group, cfg)
        if PROVIDER not in loaded.auth:
            raise AsbxError(f"{group} does not use the {PROVIDER} auth provider")
        return [(group, loaded)]
    targets = []
    for g, _instance in manifest.manifest_groups(cfg):
        try:
            loaded = manifest.load_manifest(g, cfg)
        except AsbxError as e:
            ui.warn(f"{g}: {e}")
            continue
        if PROVIDER in loaded.auth:
            targets.append((g, loaded))
    return targets


def refresh(incus: Incus, guest_user: str, targets: list[manifest.Manifest],
            export: Callable[[str], tuple[str, str]] = export_profile) -> int:
    """Pushes credentials into each running target; each profile is exported
    once per call, and a failed export is remembered rather than retried.
    Returns 1 if any target failed, else 0."""
    exported: dict[str, object] = {}  # profile -> (body, region) | AsbxError

    def get(profile: str) -> tuple[str, str]:
        if profile not in exported:
            try:
                exported[profile] = export(profile)
            except AsbxError as e:
                exported[profile] = e
        result = exported[profile]
        if isinstance(result, AsbxError):
            raise result
        return result  # type: ignore[return-value]

    failed = False
    for target in targets:
        instance = target.instance
        state = incus.instance_state(instance) if incus.instance_exists(instance) else "absent"
        if state != "Running":
            ui.ok(f"{instance}: skipped ({state})")
            continue
        try:
            creds = {p: get(p) for p in target.aws_profiles}
            push(incus, instance, guest_user, creds)
        except AsbxError as e:
            ui.warn(f"{instance}: {e}")
            failed = True
            continue
        ui.ok(f"{instance}: aws credentials refreshed ({', '.join(target.aws_profiles)})")
    return 1 if failed else 0


def cmd_aws_refresh(incus: Incus, cfg: dict, group: Optional[str]) -> int:
    targets = [m for _g, m in aws_targets(cfg, group)]
    return refresh(incus, cfg["guest_user"], targets)


def install_timer() -> None:
    if not host.which("aws"):
        raise AsbxError("aws CLI not found on the host PATH")
    unit_dir = Path.home() / ".config/systemd/user"
    unit_dir.mkdir(parents=True, exist_ok=True)

    service = ["[Unit]", "Description=Push fresh AWS credentials into asbx sandboxes", "",
               "[Service]", "Type=oneshot",
               f"Environment=PATH={os.environ.get('PATH', '')}"]
    if os.environ.get(config.CONFIG_DIR_ENV):
        service.append(f"Environment={config.CONFIG_DIR_ENV}={os.environ[config.CONFIG_DIR_ENV]}")
    service.append(f"ExecStart={sys.executable} {hostsetup.ENTRY_POINT} aws-refresh")
    timer = ["[Unit]", "Description=Refresh AWS credentials in asbx sandboxes every 10 minutes", "",
             "[Timer]", "OnActiveSec=10s", "OnUnitActiveSec=10min", "",
             "[Install]", "WantedBy=timers.target"]
    (unit_dir / SERVICE_UNIT).write_text("\n".join(service) + "\n")
    (unit_dir / TIMER_UNIT).write_text("\n".join(timer) + "\n")

    for args in (("daemon-reload",), ("enable", "--now", TIMER_UNIT)):
        proc = host.systemctl_user(*args)
        if proc.returncode != 0:
            raise AsbxError(f"systemctl --user {' '.join(args)} failed:\n{(proc.stderr or '').strip()}")
    ui.ok(f"installed and started {TIMER_UNIT}")


def credential_status(body: str, now: datetime) -> tuple[str, str]:
    """(PASS/WARN/FAIL, detail) for a pushed credential file's expiry."""
    try:
        expiry = datetime.fromisoformat(json.loads(body)["Expiration"])
    except (ValueError, KeyError, TypeError):
        return "FAIL", "unreadable"
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    remaining = expiry - now
    if remaining <= timedelta(0):
        return "FAIL", "expired"
    minutes = int(remaining.total_seconds() // 60)
    return ("WARN" if remaining < WARN_BELOW else "PASS"), f"expires in {minutes} min"
