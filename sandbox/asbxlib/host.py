"""Host-side effects outside Incus: the single seam ``subprocess``/``os`` calls
that touch the host (not a guest instance) go through, mirroring the rule that
``Incus`` is the only place ``subprocess`` touches ``incus``.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import NoReturn, Optional


def which(name: str) -> bool:
    return shutil.which(name) is not None


def git_config_get(key: str) -> str:
    proc = subprocess.run(["git", "config", "--get", key], capture_output=True, text=True)
    return proc.stdout.strip()


def tailscale_status() -> Optional[dict]:
    proc = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def forget_host_key(known_hosts: Path, hostname: str) -> None:
    known_hosts.parent.mkdir(parents=True, exist_ok=True)
    known_hosts.touch(exist_ok=True)
    subprocess.run(["ssh-keygen", "-R", hostname, "-f", str(known_hosts)],
                    capture_output=True, text=True)


def generate_ssh_key(path: Path) -> None:
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "asbx", "-f", str(path)],
                    check=True, capture_output=True)


def write_private_text(path: Path, body: str) -> None:
    """Creates (or truncates) `path` with mode 0600 from the outset, so a file
    holding secrets is never briefly readable at the umask default."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(body)
    os.chmod(path, 0o600)


def exec_ssh(argv: list[str]) -> NoReturn:
    os.execvp("ssh", argv)
