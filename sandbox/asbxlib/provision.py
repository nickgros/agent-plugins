from __future__ import annotations

import os

from . import ui
from .errors import AsbxError
from .incus import Incus
from .manifest import Manifest, Repo, manifest_sha256, resolve_remote_url


# ---------------------------------------------------------------------------
# Provision phase — repos, services, hook
# ---------------------------------------------------------------------------

def configure_repo_remotes(incus: Incus, instance: str, guest_user: str, workdir: str, repo: Repo) -> None:
    """Adds/updates git remotes on an already-cloned repo. Idempotent: leaves
    a remote alone if its URL already matches, `set-url`s it if it drifted
    (e.g. the manifest's org changed), and `add`s it if missing."""
    for remote_name, remote_spec in repo.remotes.items():
        remote_url = resolve_remote_url(repo.url, remote_spec)
        existing = incus.exec_in(instance, ["git", "-C", workdir, "remote", "get-url", remote_name],
                                  user=guest_user, check=False)
        if existing.returncode == 0:
            if existing.stdout.strip() != remote_url:
                incus.exec_in(instance, ["git", "-C", workdir, "remote", "set-url", remote_name, remote_url],
                               user=guest_user, check=False)
            continue
        proc = incus.exec_in(instance, ["git", "-C", workdir, "remote", "add", remote_name, remote_url],
                              user=guest_user, check=False)
        if proc.returncode != 0:
            raise AsbxError(
                f"provision failed: repo {repo.dir}: remote add {remote_name} exited {proc.returncode}"
            )


def run_provision(incus: Incus, instance: str, manifest: Manifest, guest_user: str) -> None:
    home = f"/home/{guest_user}"
    incus.exec_in(instance, ["mkdir", "-p", "/workspace", f"{home}/.asbx"], user=guest_user)
    guest_uid = incus.uid_of(instance, guest_user)

    for repo in manifest.repos:
        workdir = f"/workspace/{repo.dir}"
        check = incus.exec_in(instance, ["test", "-d", workdir], user=guest_user, check=False)
        cloned_now = check.returncode != 0
        if not cloned_now:
            ui.ok(f"skip {repo.dir} (exists)")
        else:
            clone_argv = ["git", "clone"]
            if repo.ref:
                clone_argv += ["--branch", repo.ref]
            clone_argv += [repo.url, workdir]
            proc = incus.exec_in(instance, clone_argv, user=guest_user, check=False)
            if proc.returncode != 0:
                stderr = proc.stderr or ""
                if "Authentication" in stderr or "authentication" in stderr or "could not read Username" in stderr:
                    raise AsbxError(f"provision failed: repo {repo.dir}: clone exited {proc.returncode}; run 'asbx auth {manifest.name}' first")
                raise AsbxError(f"provision failed: repo {repo.dir}: clone exited {proc.returncode}")

        configure_repo_remotes(incus, instance, guest_user, workdir, repo)

        # Repo setup is a first-clone step, not a per-provision one: it is the
        # manifest's `setup:` hook that is documented as re-running every time.
        if repo.setup and cloned_now:
            proc = incus.exec_in(instance, ["bash", "-lc", repo.setup], user=guest_user,
                                  cwd=workdir, env=manifest.env, check=False)
            if proc.returncode != 0:
                raise AsbxError(f"provision failed: repo {repo.dir}: setup exited {proc.returncode}")

    if manifest.compose_path:
        basename = os.path.basename(manifest.compose_path)
        remote_path = f"{home}/.asbx/services/{basename}"
        incus.exec_in(instance, ["mkdir", "-p", f"{home}/.asbx/services"], user=guest_user)
        incus.file_push(instance, manifest.compose_path, remote_path, mode="0644",
                         uid=guest_uid, gid=guest_uid)
        incus.exec_in(instance, ["bash", "-c",
                                  "for i in $(seq 1 30); do docker info >/dev/null 2>&1 && exit 0; sleep 2; done; exit 1"],
                       user="root", check=False)
        proc = incus.exec_in(instance, ["docker", "compose", "-f", remote_path, "up", "-d"],
                              user=guest_user, env=manifest.env, check=False)
        if proc.returncode != 0:
            raise AsbxError(f"provision failed: services: docker compose up exited {proc.returncode}")

    if manifest.setup_path:
        remote_path = f"{home}/.asbx/provision.sh"
        incus.file_push(instance, manifest.setup_path, remote_path, mode="0755",
                         uid=guest_uid, gid=guest_uid)
        proc = incus.exec_in(instance, ["bash", remote_path], user=guest_user,
                              cwd="/workspace", env=manifest.env, check=False)
        if proc.returncode != 0:
            raise AsbxError(f"provision failed: setup hook exited {proc.returncode}")

    incus.config_set(instance, "user.asbx.manifest_sha256", manifest_sha256(manifest.path))
    ui.ok(f"{instance}: provision complete")
