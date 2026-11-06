from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from typing import Optional

from .errors import AsbxError
from .incus import Incus
from . import config
from . import host
from . import guest
from . import ingress
from . import netacl
from . import sshconf
from . import ui
from . import up
from . import manifest as manifest_mod
from . import provision


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
    shell = ["bash", "-l"] if root else [config.SHELL_PATHS[cfg["shell"]], "-l"]
    incus.exec_in(instance, shell, user=user, tty=True, check=False)


def cmd_start(incus: Incus, cfg: dict, group: str) -> None:
    manifest = manifest_mod.load_manifest(group, cfg)
    incus.run("start", manifest.instance)
    _wait_for_ssh(cfg, manifest.instance)
    ui.ok(f"started {manifest.instance}")


def cmd_stop(incus: Incus, cfg: dict, group: str) -> None:
    manifest = manifest_mod.load_manifest(group, cfg)
    incus.run("stop", manifest.instance)
    ui.ok(f"stopped {manifest.instance}")


def _wait_for_ssh(cfg: dict, instance: str) -> None:
    """`incus start/restart` return at power-on; sshd and the bridge DNS entry
    come up later, so a prompt `asbx ssh` would hit 'No route to host'. The
    instance did start, so a port that never answers is a warning, not a failure."""
    hostname = sshconf.instance_hostname(instance, cfg)
    if not host.wait_for_port(hostname, 22):
        ui.warn(f"{hostname}:22 not reachable yet; run 'asbx doctor'")


def cmd_restart(incus: Incus, cfg: dict, group: str) -> None:
    manifest = manifest_mod.load_manifest(group, cfg)
    incus.run("restart", manifest.instance)
    _wait_for_ssh(cfg, manifest.instance)
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
    # Incus refuses to start a NIC whose static IPv4 another NIC already has,
    # stopped or not, so the copy must not keep the pin; restore puts it back.
    incus.run("config", "device", "unset", copy_name, "eth0", "ipv4.address", check=False)
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
        ipv4 = netacl.pinned_ipv4(incus, instance)
        incus.run("delete", instance, "--force")
        incus.run("copy", copy_name, instance)
        if ipv4:
            netacl.pin_nic(incus, instance, cfg, address=ipv4)
    incus.run("start", instance)
    guest.wait_for_agent(incus, instance)
    sshconf.write_ssh_config(instance, cfg)
    ui.ok(f"{instance} restored to {label}")


def parse_worktree_list(porcelain: str) -> list[str]:
    """Work-tree paths from `git worktree list --porcelain`. Bare entries have
    no work tree and prunable ones no longer exist on disk."""
    paths = []
    for block in porcelain.strip().split("\n\n"):
        fields = block.splitlines()
        if not fields or not fields[0].startswith("worktree "):
            continue
        if any(f == "bare" or f == "prunable" or f.startswith("prunable ") for f in fields[1:]):
            continue
        paths.append(fields[0][len("worktree "):])
    return paths


def _find_path_pattern(path: str) -> str:
    """`path` as a literal `find -path` pattern."""
    return re.sub(r"([\\*?\[])", r"\\\1", path)


# Args: workspace dir, guide name, guide marker, then `find -path` patterns
# for every repo and worktree. Prints, for each top-level workspace entry, one
# file under it that lies in no repo: a rebuild cannot recreate those.
# Directories, and the guide when asbx generated it, are recreated by
# provision. Exits non-zero when find cannot read everything.
UNRECOVERABLE_SCRIPT = provision.GUIDE_IS_GENERATED_SH + r"""
set -o pipefail
ws=$1; guide=$2; marker=$3; shift 3
args=()
for p in "$@"; do args+=(-path "$p" -prune -o); done
if guide_is_generated "$ws/$guide" "$marker"; then args+=(-path "$ws/$guide" -prune -o); fi
find "$ws" -mindepth 1 "${args[@]}" ! -type d -print |
  awk -v n="${#ws}" '{ rel = substr($0, n + 2); top = rel; sub(/\/.*/, "", top);
                       if (!(top in seen)) { seen[top] = 1; print rel } }'
"""


def find_unsafe_repos(incus: Incus, instance: str, group: str, guest_user: str) -> list[str]:
    """Everything whose loss a rebuild would cause: each repo found within
    three levels of /workspace, plus every worktree git has registered for
    one, wherever it lives (e.g. /workspace/worktrees/<repo>/<branch>, or omp's
    ~/.omp/wt)."""
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
    found = incus.exec_in(instance, ["find", "/workspace", "-maxdepth", "3", "-name", ".git",
                                      "-printf", "%h\\n"], user=guest_user, check=False)
    found_roots = {r for r in (found.stdout or "").splitlines() if r.startswith("/workspace/")}
    repo_roots = set(found_roots)
    # Worktrees share their repository's branches, so `git log --branches`
    # runs once per repository: from the first entry git lists for it, which
    # is the main working tree unless that is bare.
    log_roots: set[str] = set()
    listed: set[str] = set()
    blocking = []
    for root in sorted(found_roots):
        if root in listed:
            continue
        proc = incus.exec_in(instance, ["git", "-C", root, "worktree", "list", "--porcelain"],
                             user=guest_user, check=False)
        if proc.returncode != 0:
            blocking.append(f"{root[len('/workspace/'):]}: cannot list its worktrees — "
                            f"their work cannot be checked")
            log_roots.add(root)
            continue
        worktrees = parse_worktree_list(proc.stdout) or [root]
        repo_roots.update(worktrees)
        listed.update(worktrees)
        log_roots.add(worktrees[0])

    # Registered worktrees can sit next to anything (one shared worktrees/
    # dir holds every repo's), so the check covers every file outside a repo
    # rather than whole top-level entries.
    checked = incus.exec_in(instance, ["bash", "-c", UNRECOVERABLE_SCRIPT, "_", "/workspace",
                                       manifest_mod.GUIDE_NAME, provision.GUIDE_MARKER,
                                       *(_find_path_pattern(r) for r in sorted(repo_roots))],
                            user=guest_user, check=False)
    if checked.returncode != 0:
        blocking.append("/workspace: cannot check it for files outside git repositories — "
                        "they could be lost")
    for rel in sorted((checked.stdout or "").splitlines()) if checked.returncode == 0 else []:
        top = rel.split("/", 1)[0]
        blocking.append(f"{top}: not a git repository — contents cannot be recovered" if rel == top else
                        f"{top}: holds files outside any git repository (e.g. {rel}) — "
                        f"they cannot be recovered")

    for root in sorted(repo_roots):
        name = root[len("/workspace/"):] if root.startswith("/workspace/") else root
        status = incus.exec_in(instance, ["git", "-C", root, "status", "--porcelain"],
                               user=guest_user, check=False)
        remote = incus.exec_in(instance, ["git", "-C", root, "remote"],
                               user=guest_user, check=False)
        unpushed = ""
        if root in log_roots:
            unpushed = incus.exec_in(instance, ["git", "-C", root, "log", "--branches", "--not",
                                                 "--remotes", "--oneline"],
                                     user=guest_user, check=False).stdout
        msg = classify_repo(name, status.stdout, remote.stdout, unpushed)
        if msg:
            blocking.append(msg)
    return blocking


def cmd_rebuild(incus: Incus, cfg: dict, group: str, force: bool, yes: bool) -> None:
    manifest = manifest_mod.load_manifest(group, cfg)
    instance = manifest.instance

    ipv4 = None
    if incus.instance_exists(instance):
        blocking = find_unsafe_repos(incus, instance, group, cfg["guest_user"])
        if blocking and not force:
            raise AsbxError("rebuild refused, unsafe repos:\n  " + "\n  ".join(blocking))
        # Kept so client configs and `ssh-config --direct` stanzas survive the rebuild.
        ipv4 = netacl.pinned_ipv4(incus, instance)

    # `up` would refuse after the delete; refuse before it instead.
    ingress.require_firewall(cfg)

    if not yes:
        if not ui.confirm_exact(f"type '{group}' to confirm rebuild of {instance}: ", group):
            raise AsbxError("aborted")

    cmd_rm(incus, cfg, group, yes=True)
    up.cmd_up(incus, cfg, group, no_auth=False, no_provision=False, ipv4=ipv4)


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
