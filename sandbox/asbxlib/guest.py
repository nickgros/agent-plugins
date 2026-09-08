"""Guest-side operations: acting on a live instance's OS — mounts,
cloud-init rendering, agent/cloud-init readiness, and guest env/git identity."""
from __future__ import annotations

import json
import stat
import time
from pathlib import Path
from typing import Optional

from . import config
from . import host
from .errors import AsbxError
from .incus import Incus
from .manifest import Mount


# ---------------------------------------------------------------------------
# Mount readability check (pure, testable without Incus)
# ---------------------------------------------------------------------------

def mount_readable_error(host_path: Path) -> Optional[str]:
    """Checks the mount source directory itself. Ancestor directories are
    NOT walked: virtiofs mounts are served by the Incus daemon running as
    host root, so parent-directory traversal permissions never block the
    guest.

    The guest's uid is its own namespace's, unrelated to any host uid, so
    world read+execute is what the guest actually needs — the same bar
    doctor and `up` both apply."""
    resolved = host_path.expanduser().resolve()
    msg = f"mount {host_path}: not readable by the sandbox (chmod o+rX {host_path})"
    try:
        st = resolved.stat()
    except OSError:
        return msg
    needed = stat.S_IROTH | stat.S_IXOTH
    if (st.st_mode & needed) != needed:
        return msg
    return None


def render_env_file(env: dict[str, str]) -> str:
    lines = []
    for k, v in env.items():
        escaped = str(v).replace("'", "'\\''")
        lines.append(f"export {k}='{escaped}'")
    return "\n".join(lines) + ("\n" if lines else "")


def render_instance_cloud_init(instance: str, guest_user: str, pubkey: str) -> str:
    return (
        "#cloud-config\n"
        f"hostname: {instance}\n"
        "users:\n"
        f"  - name: {guest_user}\n"
        "    ssh_authorized_keys:\n"
        f"      - {pubkey}\n"
        "runcmd:\n"
        "  - systemctl restart ssh\n"
    )


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


def wait_for_cloud_init(incus: Incus, instance: str) -> None:
    incus.run("exec", instance, "--", "cloud-init", "status", "--wait", check=False)
    status = incus.run("exec", instance, "--", "cloud-init", "status", check=False)
    if "error" in status.stdout:
        tail = incus.run("exec", instance, "--", "cloud-init", "status", "--long", check=False)
        raise AsbxError(f"cloud-init failed in {instance}:\n{tail.stdout}")


def sync_mounts(incus: Incus, instance: str, mounts: list[Mount]) -> bool:
    """Makes the instance's `mnt-*` disk devices match `mounts`. Returns True
    when devices changed, meaning the caller must restart the instance for the
    guest to see them."""
    desired: dict[str, dict[str, str]] = {}
    for i, mount in enumerate(mounts):
        host_path = config.expand(mount.host)
        if not host_path.exists():
            raise AsbxError(f"mount {mount.host}: host path does not exist")
        err = mount_readable_error(host_path)
        if err:
            raise AsbxError(err)
        desired[f"mnt-{i}"] = {
            "type": "disk",
            "source": str(host_path),
            "path": mount.guest,
            "readonly": "true",
        }

    existing = {name: dev for name, dev in incus.devices(instance).items()
                if name.startswith("mnt-")}
    if existing == desired:
        return False

    for name in existing:
        incus.device_remove(instance, name)
    for name, dev in desired.items():
        proc = incus.device_add(
            instance, name, "disk",
            f"source={dev['source']}", f"path={dev['path']}", "readonly=true",
        )
        if proc.returncode != 0:
            raise AsbxError(
                f"mount {dev['source']} -> {dev['path']}: incus refused the device:\n"
                f"{(proc.stderr or '').strip()}"
            )
    return True


def write_guest_env(incus: Incus, instance: str, cfg: dict, manifest_env: dict) -> None:
    guest_user = cfg["guest_user"]
    env = {**cfg["harness_env"], **cfg["defaults"]["env"], **manifest_env}
    incus.push_text(instance, "/etc/profile.d/asbx-env.sh", render_env_file(env),
                    mode="0644", uid=0, gid=0)

    claude_settings = json.dumps({"env": {**cfg["harness_env"], **manifest_env}}, indent=2) + "\n"
    guest_uid = incus.uid_of(instance, guest_user)
    incus.exec_in(instance, ["mkdir", "-p", f"/home/{guest_user}/.claude"], user=guest_user)
    incus.push_text(instance, f"/home/{guest_user}/.claude/settings.json", claude_settings,
                    mode="0644", uid=guest_uid, gid=guest_uid)


def write_git_identity(incus: Incus, instance: str, cfg: dict) -> None:
    name = cfg["defaults"]["git"]["name"]
    email = cfg["defaults"]["git"]["email"]
    if not name:
        name = host.git_config_get("user.name")
    if not email:
        email = host.git_config_get("user.email")
    guest_user = cfg["guest_user"]
    if name:
        incus.exec_in(instance, ["git", "config", "--global", "user.name", name], user=guest_user)
    if email:
        incus.exec_in(instance, ["git", "config", "--global", "user.email", email], user=guest_user)
    incus.exec_in(instance, ["git", "config", "--global", "init.defaultBranch", "main"], user=guest_user)
    incus.exec_in(instance, ["git", "config", "--global", "push.autoSetupRemote", "true"], user=guest_user)
