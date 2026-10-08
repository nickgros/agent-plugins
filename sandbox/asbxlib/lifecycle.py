from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Optional

from .errors import AsbxError
from .incus import Incus
from . import config
from . import host
from . import guest
from . import sshconf
from . import ui
from . import up
from . import manifest as manifest_mod


# ---------------------------------------------------------------------------
# Rebuild safety classification (pure, testable without Incus)
# ---------------------------------------------------------------------------

def classify_repo(dir_name: str, status_porcelain: str, remote_output: str, unpushed_log: str) -> Optional[str]:
    if not remote_output.strip():
        return f"{dir_name}: no remote configured — work cannot be recovered"
    if status_porcelain.strip():
        return f"{dir_name}: dirty (uncommitted changes)"
    if unpushed_log.strip():
        return f"{dir_name}: unpushed commits"
    return None


# ---------------------------------------------------------------------------
# Lifecycle commands
# ---------------------------------------------------------------------------

def cmd_list(incus: Incus, cfg: dict) -> None:
    groups = manifest_mod.manifest_groups(cfg)
    rows = []
    for group, instance in groups:
        exists = incus.instance_exists(instance)
        state = incus.instance_state(instance) if exists else "absent"
        egress_mode = incus.config_get(instance, "user.asbx.egress_mode") if exists else ""
        manifest = manifest_mod.load_manifest(group, cfg)
        drift = ""
        if exists:
            recorded = incus.config_get(instance, "user.asbx.manifest_sha256")
            current = manifest_mod.manifest_sha256(manifest.path)
            if recorded and recorded != current:
                drift = "manifest: drifted"
        rows.append((instance, group, state, egress_mode, drift))

    if not rows:
        print("no manifests found in " + str(config.projects_dir()))
        return
    header = ("INSTANCE", "GROUP", "STATE", "EGRESS", "NOTE")
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(header)]
    def fmt(row):
        return "  ".join(str(c).ljust(w) for c, w in zip(row, widths))
    print(fmt(header))
    for row in rows:
        print(fmt(row))


def cmd_ssh(incus: Incus, cfg: dict, group: str, extra: list[str]) -> None:
    manifest = manifest_mod.load_manifest(group, cfg)
    instance = manifest.instance
    conf_path = sshconf.config_file(cfg, instance)
    if not conf_path.exists():
        raise AsbxError(f"no ssh config for {instance}; run 'asbx up {group}' first")
    argv = ["ssh", "-F", str(conf_path), instance] + extra
    host.exec_ssh(argv)


def cmd_shell(incus: Incus, cfg: dict, group: str, root: bool) -> None:
    manifest = manifest_mod.load_manifest(group, cfg)
    instance = manifest.instance
    user = "root" if root else cfg["guest_user"]
    incus.exec_in(instance, ["bash", "-l"], user=user, tty=True, check=False)


def cmd_start(incus: Incus, cfg: dict, group: str) -> None:
    manifest = manifest_mod.load_manifest(group, cfg)
    incus.run("start", manifest.instance)
    ui.ok(f"started {manifest.instance}")


def cmd_stop(incus: Incus, cfg: dict, group: str) -> None:
    manifest = manifest_mod.load_manifest(group, cfg)
    incus.run("stop", manifest.instance)
    ui.ok(f"stopped {manifest.instance}")


def cmd_restart(incus: Incus, cfg: dict, group: str) -> None:
    manifest = manifest_mod.load_manifest(group, cfg)
    incus.run("restart", manifest.instance)
    ui.ok(f"restarted {manifest.instance}")


def _snapshot_copy_name(instance: str, label: str) -> str:
    return f"{instance}-snap-{label}"


def cmd_snapshot(incus: Incus, cfg: dict, group: str, label: Optional[str]) -> None:
    manifest = manifest_mod.load_manifest(group, cfg)
    instance = manifest.instance
    if not label:
        label = f"pre-{datetime.now(timezone.utc):%Y%m%d%H%M}"
    if incus.storage_driver("default") == "dir":
        print("notice: dir storage pool; snapshot copies the full root disk", file=sys.stderr)

    if label in incus.snapshot_list(instance):
        raise AsbxError(f"snapshot {instance}/{label} already exists; pick another label")

    proc = incus.snapshot_create(instance, label)
    if proc.returncode == 0:
        ui.ok(f"snapshot {instance}/{label} created")
        return

    state = incus.instance_state(instance)
    if state == "Running":
        incus.run("stop", instance)
        proc = incus.snapshot_create(instance, label)
        incus.run("start", instance)
        if proc.returncode == 0:
            ui.ok(f"snapshot {instance}/{label} created (stopped for copy)")
            return

    ui.warn("pool driver does not support VM snapshots; falling back to incus copy")
    copy_name = _snapshot_copy_name(instance, label)
    incus.run("copy", instance, copy_name, "--instance-only")
    ui.ok(f"snapshot {instance}/{label} created via copy ({copy_name})")


def cmd_restore(incus: Incus, cfg: dict, group: str, label: str, yes: bool) -> None:
    manifest = manifest_mod.load_manifest(group, cfg)
    instance = manifest.instance

    # Resolve what `label` actually names BEFORE touching the instance: a
    # snapshot, a fallback copy, or nothing at all. The instance is only
    # destroyed once the copy it is to be rebuilt from is known to exist.
    from_snapshot = label in incus.snapshot_list(instance)
    copy_name = _snapshot_copy_name(instance, label)
    from_copy = not from_snapshot and incus.instance_exists(copy_name)
    if not from_snapshot and not from_copy:
        raise AsbxError(
            f"{instance}: no snapshot or copy named '{label}' "
            f"(snapshots: {', '.join(incus.snapshot_list(instance)) or 'none'})"
        )

    if not yes:
        if not ui.confirm(f"restore {instance} to snapshot '{label}'? This discards current state. [y/N] "):
            raise AsbxError("aborted")

    incus.run("stop", instance, check=False)
    if from_snapshot:
        proc = incus.snapshot_restore(instance, label)
        if proc.returncode != 0:
            raise AsbxError(f"{instance}: restoring snapshot '{label}' failed:\n{(proc.stderr or '').strip()}")
    else:
        incus.run("delete", instance, "--force")
        incus.run("copy", copy_name, instance)
    incus.run("start", instance)
    guest.wait_for_agent(incus, instance)
    sshconf.write_ssh_config(instance, cfg)
    ui.ok(f"{instance} restored to {label}")


def find_unsafe_repos(incus: Incus, instance: str, group: str, guest_user: str) -> list[str]:
    """Everything under /workspace whose loss a rebuild would cause. Repos are
    found wherever .git appears within three levels, so a `<repo>.worktrees/`
    directory is checked through the worktrees inside it, not as one repo."""
    # An unreadable /workspace must never be mistaken for "no repos to lose":
    # that would let rebuild destroy unpushed work unwarned.
    listing = incus.exec_in(instance, ["bash", "-c", "ls -1 /workspace"],
                            user=guest_user, check=False)
    if listing.returncode != 0:
        state = incus.instance_state(instance)
        raise AsbxError(
            f"{instance}: cannot list /workspace (state={state}); "
            f"start it first ('asbx start {group}') so rebuild can check for unsaved work"
        )
    top_dirs = [d for d in listing.stdout.splitlines() if d.strip()]
    found = incus.exec_in(instance, ["find", "/workspace", "-maxdepth", "3", "-name", ".git",
                                      "-printf", "%h\\n"], user=guest_user, check=False)
    repo_roots = sorted({r for r in (found.stdout or "").splitlines() if r.startswith("/workspace/")})

    blocking = []
    for top in top_dirs:
        prefix = f"/workspace/{top}"
        if not any(r == prefix or r.startswith(prefix + "/") for r in repo_roots):
            blocking.append(f"{top}: not a git repository — contents cannot be recovered")
    for root in repo_roots:
        name = root[len("/workspace/"):]
        status = incus.exec_in(instance, ["git", "-C", root, "status", "--porcelain"],
                               user=guest_user, check=False)
        remote = incus.exec_in(instance, ["git", "-C", root, "remote"],
                               user=guest_user, check=False)
        unpushed = incus.exec_in(instance, ["git", "-C", root, "log", "--branches", "--not",
                                             "--remotes", "--oneline"],
                                 user=guest_user, check=False)
        msg = classify_repo(name, status.stdout, remote.stdout, unpushed.stdout)
        if msg:
            blocking.append(msg)
    return blocking


def cmd_rebuild(incus: Incus, cfg: dict, group: str, force: bool, yes: bool) -> None:
    manifest = manifest_mod.load_manifest(group, cfg)
    instance = manifest.instance

    if incus.instance_exists(instance):
        blocking = find_unsafe_repos(incus, instance, group, cfg["guest_user"])
        if blocking and not force:
            raise AsbxError("rebuild refused, unsafe repos:\n  " + "\n  ".join(blocking))

    if not yes:
        if not ui.confirm_exact(f"type '{group}' to confirm rebuild of {instance}: ", group):
            raise AsbxError("aborted")

    cmd_rm(incus, cfg, group, yes=True)
    up.cmd_up(incus, cfg, group, no_auth=False, no_provision=False)


def cmd_rm(incus: Incus, cfg: dict, group: str, yes: bool) -> None:
    manifest = manifest_mod.load_manifest(group, cfg)
    instance = manifest.instance

    if not yes:
        if not ui.confirm(f"delete {instance}? [y/N] "):
            raise AsbxError("aborted")

    incus.run("delete", instance, "--force", check=False)

    sshconf.remove_ssh_config(cfg, instance)
    host.forget_host_key(config.expand(cfg["ssh"]["known_hosts"]),
                         sshconf.instance_hostname(instance, cfg))

    ui.ok(f"removed {instance}")
