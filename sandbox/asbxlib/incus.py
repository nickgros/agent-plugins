from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
from typing import Optional

import yaml

from .errors import AsbxError


# ---------------------------------------------------------------------------
# Incus provider seam — the sole place subprocess touches `incus`.
# ---------------------------------------------------------------------------

class Incus:
    def __init__(self) -> None:
        self._uid_cache: dict[tuple[str, str], int] = {}

    def run(self, *args: str, check: bool = True, capture: bool = True,
            tty: bool = False, stdin: Optional[str] = None) -> subprocess.CompletedProcess:
        argv = ["incus", *args]
        if tty:
            proc = subprocess.run(argv, check=False)
        else:
            proc = subprocess.run(
                argv,
                check=False,
                capture_output=capture,
                text=True,
                input=stdin,
            )
        if check and proc.returncode != 0:
            stderr = getattr(proc, "stderr", None) or ""
            raise AsbxError(f"command failed: {' '.join(argv)}\n{stderr.strip()}")
        return proc

    def uid_of(self, instance: str, user: str) -> int:
        key = (instance, user)
        if key not in self._uid_cache:
            proc = self.run("exec", instance, "--", "id", "-u", user)
            self._uid_cache[key] = int(proc.stdout.strip())
        return self._uid_cache[key]

    def exec_in(self, instance: str, argv: list[str], user: str,
                cwd: Optional[str] = None, env: Optional[dict[str, str]] = None,
                tty: bool = False, check: bool = True) -> subprocess.CompletedProcess:
        cmd = ["exec", instance]
        if tty:
            cmd.append("-t")
        if user == "root":
            if cwd:
                cmd += ["--cwd", cwd]
            for k, v in (env or {}).items():
                cmd += ["--env", f"{k}={v}"]
            cmd += ["--", *argv]
        else:
            # incus exec --user <uid> alone leaves gid=0 with no supplementary
            # groups (e.g. missing the docker group), so route non-root execs
            # through runuser, which initializes the session normally. Default
            # cwd to the user's home: an unset cwd otherwise inherits root's
            # cwd, which the target user often cannot even stat.
            effective_cwd = cwd or f"/home/{user}"
            parts = [f"cd {shlex.quote(effective_cwd)} &&"]
            env_prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in (env or {}).items())
            if env_prefix:
                parts.append(f"env {env_prefix}")
            parts.append(" ".join(shlex.quote(a) for a in argv))
            script = " ".join(parts)
            cmd += ["--", "runuser", "-u", user, "--", "bash", "-c", script]
        return self.run(*cmd, tty=tty, check=check)


    def file_push(self, instance: str, local: str, remote: str,
                  mode: Optional[str] = None, uid: Optional[int] = None,
                  gid: Optional[int] = None) -> None:
        args = ["file", "push", local, f"{instance}{remote}"]
        if mode is not None:
            args += ["--mode", mode]
        if uid is not None:
            args += ["--uid", str(uid)]
        if gid is not None:
            args += ["--gid", str(gid)]
        self.run(*args)

    def push_text(self, instance: str, remote: str, body: str, mode: Optional[str] = None,
                  uid: Optional[int] = None, gid: Optional[int] = None) -> None:
        """Pushes in-memory text to a guest path. `incus file push` needs a real
        file, so the body goes through a host temp file that is always removed."""
        with tempfile.NamedTemporaryFile("w", delete=False) as tmp:
            tmp.write(body)
            tmp_path = tmp.name
        try:
            self.file_push(instance, tmp_path, remote, mode=mode, uid=uid, gid=gid)
        finally:
            os.unlink(tmp_path)

    def instance_exists(self, instance: str) -> bool:
        proc = self.run("info", instance, check=False)
        return proc.returncode == 0

    def instance_state(self, instance: str) -> Optional[str]:
        proc = self.run("list", instance, "--format", "json", check=False)
        if proc.returncode != 0:
            return None
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return None
        if not data:
            return None
        return data[0]["status"]

    def config_get(self, target: str, key: str) -> str:
        proc = self.run("config", "get", target, key, check=False)
        return proc.stdout.strip()

    def config_set(self, instance: str, key: str, value: str) -> None:
        self.run("config", "set", instance, key, value)

    def device_add(self, instance: str, name: str, *device_args: str) -> subprocess.CompletedProcess:
        return self.run("config", "device", "add", instance, name, *device_args, check=False)

    def device_remove(self, instance: str, name: str) -> None:
        self.run("config", "device", "remove", instance, name, check=False)

    def network_get(self, network: str, key: str) -> str:
        proc = self.run("network", "get", network, key)
        return proc.stdout.strip()

    def network_acl_exists(self, acl: str) -> bool:
        proc = self.run("network", "acl", "show", acl, check=False)
        return proc.returncode == 0

    def network_acl_create(self, acl: str) -> None:
        self.run("network", "acl", "create", acl)

    def network_acl_edit(self, acl: str, yaml_body: str) -> None:
        self.run("network", "acl", "edit", acl, stdin=yaml_body)

    def image_alias_exists(self, alias: str) -> Optional[str]:
        proc = self.run("image", "alias", "list", "--format", "json", check=False)
        if proc.returncode != 0:
            return None
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return None
        for entry in data:
            if entry.get("name") == alias:
                return entry["target"]
        return None

    def image_alias_delete(self, alias: str) -> None:
        self.run("image", "alias", "delete", alias, check=False)

    def image_alias_create(self, alias: str, fingerprint: str) -> None:
        self.run("image", "alias", "create", alias, fingerprint)

    def snapshot_create(self, instance: str, label: str) -> subprocess.CompletedProcess:
        return self.run("snapshot", "create", instance, label, check=False)

    def snapshot_restore(self, instance: str, label: str) -> subprocess.CompletedProcess:
        return self.run("snapshot", "restore", instance, label, check=False)

    def snapshot_list(self, instance: str) -> list[str]:
        proc = self.run("query", f"/1.0/instances/{instance}/snapshots", check=False)
        if proc.returncode != 0:
            return []
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return []
        return [entry.rsplit("/", 1)[-1] for entry in data]

    def info(self) -> subprocess.CompletedProcess:
        return self.run("info", check=False)

    def network_acl_show(self, acl: str) -> subprocess.CompletedProcess:
        return self.run("network", "acl", "show", acl, check=False)

    def storage_driver(self, pool: str) -> str:
        """'' when the pool or the driver cannot be read."""
        proc = self.run("storage", "get", pool, "driver", check=False)
        return proc.stdout.strip() if proc.returncode == 0 else ""

    def devices(self, instance: str) -> dict[str, dict[str, str]]:
        """The instance's devices as incus reports them, device name -> config."""
        proc = self.run("config", "show", instance, check=False)
        if proc.returncode != 0:
            return {}
        try:
            data = yaml.safe_load(proc.stdout) or {}
        except yaml.YAMLError:
            return {}
        devices = data.get("devices") or {}
        return {name: {k: str(v) for k, v in dev.items()} for name, dev in devices.items()}
