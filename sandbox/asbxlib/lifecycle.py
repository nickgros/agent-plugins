from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from .backend import ABSENT, META_EGRESS_MODE, META_MANIFEST_SHA256, SandboxBackend
from .errors import AsbxError
from . import config
from . import host
from . import sshconf
from . import ui
from . import up
from . import manifest as manifest_mod
from . import provision


# ---------------------------------------------------------------------------
# Rebuild safety classification (pure, testable without a VM)
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

def cmd_list(backend: SandboxBackend, cfg: dict) -> None:
    groups = manifest_mod.manifest_groups(cfg)
    rows = []
    for group, instance in groups:
        state = backend.state(instance)
        exists = state != ABSENT
        egress_mode = backend.meta_get(instance, META_EGRESS_MODE) if exists else ""
        manifest = manifest_mod.load_manifest(group, cfg)
        drift = ""
        if exists:
            recorded = backend.meta_get(instance, META_MANIFEST_SHA256)
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


def cmd_ssh(backend: SandboxBackend, cfg: dict, group: str, extra: list[str]) -> None:
    manifest = manifest_mod.load_manifest(group, cfg)
    instance = manifest.instance
    conf_path = sshconf.config_file(cfg, instance)
    if not conf_path.exists():
        raise AsbxError(f"no ssh config for {instance}; run 'asbx up {group}' first")
    argv = ["ssh", "-F", str(conf_path), instance] + extra
    host.exec_ssh(argv)


def cmd_shell(backend: SandboxBackend, cfg: dict, group: str, root: bool) -> None:
    manifest = manifest_mod.load_manifest(group, cfg)
    user = "root" if root else cfg["guest_user"]
    shell = ["bash", "-l"] if root else [config.SHELL_PATHS[cfg["shell"]], "-l"]
    backend.guest(manifest.instance).exec(shell, user=user, tty=True, check=False)


def cmd_start(backend: SandboxBackend, cfg: dict, group: str) -> None:
    manifest = manifest_mod.load_manifest(group, cfg)
    backend.start(cfg, up.spec_for(manifest))
    _wait_for_ssh(backend, cfg, manifest.instance)
    ui.ok(f"started {manifest.instance}")


def cmd_stop(backend: SandboxBackend, cfg: dict, group: str) -> None:
    manifest = manifest_mod.load_manifest(group, cfg)
    backend.stop(manifest.instance)
    ui.ok(f"stopped {manifest.instance}")


def _wait_for_ssh(backend: SandboxBackend, cfg: dict, instance: str) -> None:
    """sshd and the address the host reaches it on come up after the guest
    agent, so a prompt `asbx ssh` would hit 'No route to host'. The sandbox
    did start, so a port that never answers is a warning, not a failure."""
    address = backend.ssh_address(cfg, instance)
    if not address or not host.wait_for_port(address, 22):
        ui.warn(f"{instance}: ssh ({address or 'no address yet'}:22) not reachable yet; run 'asbx doctor'")


def cmd_restart(backend: SandboxBackend, cfg: dict, group: str) -> None:
    manifest = manifest_mod.load_manifest(group, cfg)
    backend.restart(cfg, up.spec_for(manifest))
    _wait_for_ssh(backend, cfg, manifest.instance)
    ui.ok(f"restarted {manifest.instance}")


def cmd_snapshot(backend: SandboxBackend, cfg: dict, group: str, label: Optional[str]) -> None:
    manifest = manifest_mod.load_manifest(group, cfg)
    instance = manifest.instance
    if not label:
        label = f"pre-{datetime.now(timezone.utc):%Y%m%d%H%M}"
    if label in backend.snapshots(instance):
        raise AsbxError(f"snapshot {instance}/{label} already exists; pick another label")
    backend.snapshot(cfg, up.spec_for(manifest), label)


def cmd_restore(backend: SandboxBackend, cfg: dict, group: str, label: str, yes: bool) -> None:
    manifest = manifest_mod.load_manifest(group, cfg)
    instance = manifest.instance

    labels = backend.snapshots(instance)
    if label not in labels:
        raise AsbxError(f"{instance}: no snapshot named '{label}' "
                        f"(snapshots: {', '.join(labels) or 'none'})")

    if not yes:
        if not ui.confirm(f"restore {instance} to snapshot '{label}'? This discards current state. [y/N] "):
            raise AsbxError("aborted")

    backend.restore(cfg, up.spec_for(manifest), label)
    sshconf.write_ssh_config(cfg, instance, backend.ssh_route(cfg, instance))
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


def find_unsafe_repos(backend: SandboxBackend, instance: str, group: str, guest_user: str) -> list[str]:
    """Everything whose loss a rebuild would cause: each repo found within
    three levels of /workspace, plus every worktree git has registered for
    one, wherever it lives (e.g. /workspace/worktrees/<repo>/<branch>, or omp's
    ~/.omp/wt)."""
    g = backend.guest(instance)
    # An unreadable /workspace must never be mistaken for "no repos to lose":
    # that would let rebuild destroy unpushed work unwarned.
    listing = g.exec(["bash", "-c", "ls -1 /workspace"], user=guest_user, check=False)
    if listing.returncode != 0:
        raise AsbxError(
            f"{instance}: cannot list /workspace (state={backend.state(instance)}); "
            f"start it first ('asbx start {group}') so rebuild can check for unsaved work"
        )
    found = g.exec(["find", "/workspace", "-maxdepth", "3", "-name", ".git", "-printf", "%h\\n"],
                   user=guest_user, check=False)
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
        proc = g.exec(["git", "-C", root, "worktree", "list", "--porcelain"],
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
    checked = g.exec(["bash", "-c", UNRECOVERABLE_SCRIPT, "_", "/workspace",
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
        status = g.exec(["git", "-C", root, "status", "--porcelain"], user=guest_user, check=False)
        remote = g.exec(["git", "-C", root, "remote"], user=guest_user, check=False)
        unpushed = ""
        if root in log_roots:
            unpushed = g.exec(["git", "-C", root, "log", "--branches", "--not", "--remotes", "--oneline"],
                              user=guest_user, check=False).stdout
        msg = classify_repo(name, status.stdout, remote.stdout, unpushed)
        if msg:
            blocking.append(msg)
    return blocking


def cmd_rebuild(backend: SandboxBackend, cfg: dict, group: str, force: bool, yes: bool) -> None:
    manifest = manifest_mod.load_manifest(group, cfg)
    instance = manifest.instance

    ipv4 = None
    if backend.state(instance) != ABSENT:
        blocking = find_unsafe_repos(backend, instance, group, cfg["guest_user"])
        if blocking and not force:
            raise AsbxError("rebuild refused, unsafe repos:\n  " + "\n  ".join(blocking))
        # Kept so client configs and `ssh-config --direct` stanzas survive the rebuild.
        ipv4 = backend.pinned_ipv4(instance)

    # `up` would refuse after the delete; refuse before it instead.
    backend.preflight(cfg)

    if not yes:
        if not ui.confirm_exact(f"type '{group}' to confirm rebuild of {instance}: ", group):
            raise AsbxError("aborted")

    cmd_rm(backend, cfg, group, yes=True)
    up.cmd_up(backend, cfg, group, no_auth=False, no_provision=False, ipv4=ipv4)


def cmd_rm(backend: SandboxBackend, cfg: dict, group: str, yes: bool) -> None:
    manifest = manifest_mod.load_manifest(group, cfg)
    instance = manifest.instance

    if not yes:
        if not ui.confirm(f"delete {instance}? [y/N] "):
            raise AsbxError("aborted")

    route = backend.ssh_route(cfg, instance)
    backend.delete(instance)

    sshconf.remove_ssh_config(cfg, instance)
    host.forget_host_key(config.expand(cfg["ssh"]["known_hosts"]), route.host_key_alias)

    ui.ok(f"removed {instance}")
