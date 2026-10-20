"""Host-side effects outside Incus: the single seam ``subprocess``/``os`` calls
that touch the host (not a guest instance) go through, mirroring the rule that
``Incus`` is the only place ``subprocess`` touches ``incus``.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, Optional

from .errors import AsbxError


def which(name: str) -> bool:
    return shutil.which(name) is not None


SYSTEM_BIN_DIRS = ("/usr/sbin", "/sbin", "/usr/bin", "/bin")


def system_binary(name: str) -> Optional[str]:
    """`name` from a system bin dir, root-owned and writable by root alone,
    for paths that root will run; the user's PATH is never consulted."""
    for d in SYSTEM_BIN_DIRS:
        path = os.path.join(d, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        if st.st_uid == 0 and not st.st_mode & 0o022 and os.access(path, os.X_OK):
            return path
    return None


def git_config_get(key: str) -> str:
    proc = subprocess.run(["git", "config", "--get", key], capture_output=True, text=True)
    return proc.stdout.strip()


def aws_export_credentials(profile: str) -> subprocess.CompletedProcess:
    if not which("aws"):
        raise AsbxError("aws CLI not found on the host PATH")
    return subprocess.run(
        ["aws", "configure", "export-credentials", "--profile", profile, "--format", "process"],
        capture_output=True, text=True, check=False)


def aws_profile_region(profile: str) -> str:
    proc = subprocess.run(["aws", "configure", "get", "region", "--profile", profile],
                          capture_output=True, text=True, check=False)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def systemctl_user(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, check=False)


def systemctl_system(*args: str) -> subprocess.CompletedProcess:
    """Unprivileged system-manager queries such as `is-active`."""
    return subprocess.run(["systemctl", *args], capture_output=True, text=True, check=False)


def sudo(argv: list[str]) -> None:
    """Runs `argv` as root. The terminal is inherited so sudo can prompt."""
    proc = subprocess.run(["sudo", *argv], check=False)
    if proc.returncode != 0:
        raise AsbxError(f"command failed: sudo {' '.join(argv)}")


def sudo_install_file(dest: str, body: str, mode: str) -> None:
    """Writes `body` to root-owned `dest` with `mode`, creating parent dirs.
    The temp copy is 0600 so a secret body is never readable by others."""
    fd, tmp = tempfile.mkstemp(prefix="asbx-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(body)
        sudo(["install", "-D", "-m", mode, "-o", "root", "-g", "root", tmp, dest])
    finally:
        os.unlink(tmp)


def read_text(path: str) -> Optional[str]:
    """The file's text, or None when it is missing or unreadable."""
    try:
        return Path(path).read_text()
    except OSError:
        return None


def path_exists(path: str) -> bool:
    return os.path.lexists(path)


@dataclass(frozen=True)
class InterfaceFacts:
    devtype: str               # uevent DEVTYPE ("wireguard", "bridge", "wlan", ...), "" for none
    tun_flags: Optional[int]   # set for tun and tap devices
    is_bridge: bool


def interface_facts(name: str) -> Optional[InterfaceFacts]:
    """What sysfs says about a network interface, or None if it doesn't exist."""
    base = Path("/sys/class/net") / name
    if not base.exists():
        return None
    devtype = ""
    for line in (read_text(str(base / "uevent")) or "").splitlines():
        if line.startswith("DEVTYPE="):
            devtype = line[len("DEVTYPE="):]
    try:
        tun_flags: Optional[int] = int((read_text(str(base / "tun_flags")) or "").strip(), 16)
    except ValueError:
        tun_flags = None
    return InterfaceFacts(devtype=devtype, tun_flags=tun_flags, is_bridge=(base / "bridge").is_dir())


def interface_names() -> list[str]:
    try:
        return sorted(os.listdir("/sys/class/net"))
    except OSError:
        return []


def default_route_interfaces() -> set[str]:
    """Interfaces carrying an IPv4 or IPv6 default route."""
    names: set[str] = set()
    for line in (read_text("/proc/net/route") or "").splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 8 and fields[1] == "00000000" and fields[7] == "00000000":
            names.add(fields[0])
    for line in (read_text("/proc/net/ipv6_route") or "").splitlines():
        fields = line.split()
        if len(fields) == 10 and fields[0] == "0" * 32 and fields[1] == "00" and fields[9] != "lo":
            names.add(fields[9])
    return names


def wg_genkey() -> str:
    return _wg(["genkey"])


def wg_pubkey(private_key: str) -> str:
    return _wg(["pubkey"], stdin=private_key + "\n")


def _wg(args: list[str], stdin: Optional[str] = None) -> str:
    if not which("wg"):
        raise AsbxError("`wg` not found; install wireguard-tools")
    proc = subprocess.run(["wg", *args], input=stdin, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise AsbxError(f"wg {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def resolves(name: str) -> bool:
    """True when the host's resolver can turn `name` into an address."""
    try:
        socket.getaddrinfo(name, 22, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    return True


def wait_for_port(name: str, port: int, timeout: float = 120.0, interval: float = 1.0) -> bool:
    """True once a TCP connection to `name:port` succeeds, False on timeout."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            with socket.create_connection((name, port), timeout=3):
                return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(interval)


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
