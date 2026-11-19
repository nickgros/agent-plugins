"""The Tart implementation of the sandbox backend (macOS hosts, ADR 0006).

Sandboxes are Tart VMs. A running VM is the program of a per-VM launchd job,
`com.asbx.vm.<vm>`, whose command line is `tart run` with the Softnet egress
rules on it, so a sandbox can't boot without them (ADR 0003). The job plist
is kept under `<config dir>/launchd/`, not ~/Library/LaunchAgents, so
sandboxes don't autostart at login: only `start` (and `up`) load the job.
launchd restarts `tart run` when it crashes, not after a clean stop.

Softnet runs as root, so it must be a root-owned setuid binary in a
root-owned directory that comes first on the job's PATH; `preflight` and
every boot refuse otherwise.

Read-only host dirs are virtiofs shares, mounted in the guest from a managed
block in /etc/fstab. Snapshots are VM clones named `<instance>-snap-<label>`.
Metadata, the base image id and a hash of the job each VM last booted from
live in `<config dir>/state/<vm>.json`.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import platform
import posixpath
import re
import shlex
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from fractions import Fraction
from pathlib import Path
from typing import Callable, Optional

from . import config, guest, host, hostsetup, launchd, ui
from .backend import (ABSENT, META_EGRESS_MODE, RUNNING, STOPPED, Guest, SandboxSpec, SshRoute,
                      add_check, command_script, runuser_argv)
from .errors import AsbxError
from .manifest import Mount
from .tart import Tart

BASE_BUILD_VM = "asbx-base-build"
# The stock Cirrus image's agent runs as `admin`, which has passwordless sudo.
STOCK_ADMIN = "admin"
AGENT_DROPIN_PATH = "/etc/systemd/system/tart-guest-agent.service.d/asbx.conf"
AGENT_DROPIN = "[Service]\nUser=root\nGroup=root\n"
EGRESS_MODE = "softnet"
MIN_MACOS = 26
READY_ATTEMPTS = 90
READY_INTERVAL = 2.0
POWEROFF_TIMEOUT = 60
FSTAB_BEGIN = "# asbx mounts begin"
FSTAB_END = "# asbx mounts end"

_BUILT_AT = "built_at"
_SNAPSHOT_OF = "snapshot_of"
_BOOTED_JOB = "booted_job_sha256"


# ---------------------------------------------------------------------------
# Softnet: egress rules and the setuid guard (pure, testable without Tart)
# ---------------------------------------------------------------------------

def softnet_rules(block_cidrs: list[str]) -> tuple[str, str]:
    """(block, allow) for `--net-softnet-block` / `--net-softnet-allow`.
    Blocks new connections to the Mac (`@host`) and to every IPv4 range in
    `network.block_cidrs`; allows DNS to the Mac (the guest's resolver is the
    gateway) and connections the Mac opens, such as ssh. IPv6 entries are
    left out because Softnet drops all IPv6 anyway."""
    block = ["out @host"]
    for cidr in block_cidrs:
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            raise AsbxError(f"network.block_cidrs: '{cidr}' is not a CIDR")
        if net.version == 4:
            block.append(f"out {net}")
    allow = ["out proto udp @host port 53", "in @host"]
    return ",".join(block), ",".join(allow)


def _softnet_dir_problem(softnet_dir: str, stat_fn: Callable[[str], os.stat_result]) -> Optional[str]:
    if not os.path.isabs(softnet_dir):
        return f"tart.softnet_dir {softnet_dir} is not an absolute path"
    try:
        st = stat_fn(softnet_dir)
    except OSError:
        return f"{softnet_dir} does not exist"
    if not stat.S_ISDIR(st.st_mode):
        return f"{softnet_dir} is not a directory"
    if st.st_uid != 0:
        return f"{softnet_dir} is not owned by root"
    if st.st_mode & 0o022:
        return f"{softnet_dir} is writable by group or others"
    return None


def _softnet_binary_problem(binary: str, stat_fn: Callable[[str], os.stat_result]) -> Optional[str]:
    try:
        st = stat_fn(binary)
    except OSError:
        return f"{binary} does not exist"
    if not stat.S_ISREG(st.st_mode):
        return f"{binary} is not a regular file"
    if st.st_uid != 0:
        return f"{binary} is not owned by root"
    if not st.st_mode & stat.S_ISUID:
        return f"{binary} is not setuid"
    if st.st_mode & 0o022:
        return f"{binary} is writable by group or others"
    return None


def softnet_problem(softnet_dir: str,
                    stat_fn: Callable[[str], os.stat_result] = os.stat) -> Optional[str]:
    """None when `<softnet_dir>/softnet` is safe to let Tart run as root, else
    what is wrong and the commands that fix it. Tart runs the first `softnet`
    on its PATH; if the user could replace that file or its directory, any
    process running as the user could become root (ADR 0006). A Softnet
    that isn't setuid would try sudo and fail, which is also refused here."""
    binary = posixpath.join(softnet_dir, "softnet")
    problem = _softnet_dir_problem(softnet_dir, stat_fn) or _softnet_binary_problem(binary, stat_fn)
    if problem is None:
        return None
    return (f"{problem}; Softnet must be root-owned and setuid in a root-owned directory. "
            "Install it (after 'brew install openai/tools/softnet') with:\n"
            f"  sudo install -d -o root -g wheel -m 0755 {shlex.quote(softnet_dir)}\n"
            f"  sudo install -o root -g wheel -m 4555 \"$(brew --prefix softnet)/bin/softnet\" "
            f"{shlex.quote(binary)}")


def require_softnet(softnet_dir: str,
                    stat_fn: Callable[[str], os.stat_result] = os.stat) -> None:
    """Raises what `softnet_problem` finds, if anything."""
    problem = softnet_problem(softnet_dir, stat_fn)
    if problem:
        raise AsbxError(problem)


def macos_major() -> Optional[int]:
    """The macOS major version, None when not on macOS."""
    try:
        return int(platform.mac_ver()[0].split(".")[0])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Sizes: Incus-style strings to tart's units
# ---------------------------------------------------------------------------

_SIZE_RE = re.compile(r"\s*(\d+(?:\.\d+)?)\s*([A-Za-z]*)\s*")
_SIZE_UNITS = {"": 1, "B": 1, "kB": 10**3, "KB": 10**3, "MB": 10**6, "GB": 10**9, "TB": 10**12,
               "KiB": 2**10, "MiB": 2**20, "GiB": 2**30, "TiB": 2**40}


def size_bytes(size: str) -> Fraction:
    m = _SIZE_RE.fullmatch(str(size))
    if not m or m.group(2) not in _SIZE_UNITS:
        raise AsbxError(f"size '{size}' is not a number with a unit such as GiB or MB")
    return Fraction(m.group(1)) * _SIZE_UNITS[m.group(2)]


def memory_mb(size: str) -> int:
    """`tart set --memory` takes MiB, rounded up."""
    return math.ceil(size_bytes(size) / 2**20)


def disk_gb(size: str) -> int:
    """`tart set --disk-size` takes 10^9-byte GB, rounded up so the disk is at least `size`."""
    return math.ceil(size_bytes(size) / 10**9)


# ---------------------------------------------------------------------------
# Mounts: virtiofs shares and the guest's fstab block
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Share:
    """One read-only virtiofs share: host dir `host` appears at `guest` via `tag`."""
    tag: str
    host: str
    guest: str


def mount_tag(host_path: str, guest_path: str) -> str:
    """The share's virtiofs tag, derived from what it shares and where. A tag
    therefore always means the same host dir at the same guest path, so the
    fstab a guest kept from its previous boot can never mount a different
    host dir at an old path after the mount list changes."""
    digest = hashlib.sha256(f"{host_path}\0{guest_path}".encode()).hexdigest()[:12]
    return f"asbx-{digest}"


def shares(mounts: list[Mount]) -> list[Share]:
    """Validates `mounts` (the same readability rule as Incus) and resolves
    each host dir, so the VM shares exactly the directory that was checked."""
    out = []
    for mount in mounts:
        host_path = config.expand(mount.host)
        if not host_path.exists():
            raise AsbxError(f"mount {mount.host}: host path does not exist")
        err = guest.mount_readable_error(host_path)
        if err:
            raise AsbxError(err)
        resolved = str(host_path.resolve())
        if ":" in resolved:
            raise AsbxError(f"mount {mount.host}: tart can't share a path containing ':'")
        out.append(Share(tag=mount_tag(resolved, mount.guest), host=resolved, guest=mount.guest))
    return out


def _fstab_escape(path: str) -> str:
    return "".join(f"\\{ord(c):03o}" if c in " \t\n\\" else c for c in path)


def render_fstab(current: str, vm_shares: list[Share]) -> str:
    """`current` with the asbx block replaced by one line per share, or
    removed when there are none. `nofail`, because a share the VM didn't
    boot with must not stop the boot."""
    kept: list[str] = []
    in_block = False
    for line in current.splitlines():
        if line.strip() == FSTAB_BEGIN:
            in_block = True
        elif in_block:
            in_block = line.strip() != FSTAB_END
        else:
            kept.append(line)
    if vm_shares:
        kept += [FSTAB_BEGIN,
                 *(f"{s.tag} {_fstab_escape(s.guest)} virtiofs ro,nofail 0 0" for s in vm_shares),
                 FSTAB_END]
    return "\n".join(kept) + "\n" if kept else ""


def render_mount_script(vm_shares: list[Share]) -> str:
    """Mounts each share at its guest path (from fstab) unless it already is,
    and prints the guest paths that didn't mount, one per line."""
    lines = []
    for s in vm_shares:
        q = shlex.quote(s.guest)
        lines.append(f"{{ mkdir -p {q} && {{ mountpoint -q {q} || mount {q}; }}; }} >&2 "
                     f"|| printf '%s\\n' {q}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Guest
# ---------------------------------------------------------------------------

def render_push_script(remote: str, mode: Optional[str], uid: Optional[int],
                       gid: Optional[int]) -> str:
    """A root script that writes its stdin to `remote`. The body lands in a
    temp file created under umask 077 next to the target, gets its owner and
    mode, and only then is renamed into place: a secret is never readable by
    anyone else, even briefly, and the target never holds a partial body.
    Without `mode` the file stays 0600."""
    directory = posixpath.dirname(remote) or "/"
    lines = [
        "set -e",
        "umask 077",
        f"tmp=$(mktemp {shlex.quote(posixpath.join(directory, '.asbx-push.XXXXXX'))})",
        "trap 'rm -f -- \"$tmp\"' EXIT",
        'cat > "$tmp"',
    ]
    if uid is not None or gid is not None:
        owner = ("" if uid is None else str(int(uid))) + ("" if gid is None else f":{int(gid)}")
        lines.append(f'chown {owner} "$tmp"')
    if mode is not None:
        lines.append(f'chmod {shlex.quote(mode)} "$tmp"')
    lines += [f'mv -f -- "$tmp" {shlex.quote(remote)}', "trap - EXIT"]
    return "\n".join(lines) + "\n"


class TartGuest:
    """`backend.Guest` over `tart exec`, which runs commands as the user the
    guest agent runs as: root in images `build-base` made. In the stock
    image the agent still runs as `admin`, so while `build-base` makes it
    root, `root_via_sudo` prefixes every command with `sudo -n`."""

    def __init__(self, tart: Tart, instance: str, root_via_sudo: bool = False) -> None:
        self.tart = tart
        self.instance = instance
        self._sudo = ["sudo", "-n"] if root_via_sudo else []
        self._uids: dict[str, int] = {}

    def _argv(self, argv: list[str], user: str, cwd: Optional[str],
              env: Optional[dict[str, str]]) -> list[str]:
        if user == "root":
            return [*self._sudo, "bash", "-c", command_script(argv, cwd, env)]
        return [*self._sudo, *runuser_argv(argv, user, cwd, env)]

    def exec(self, argv: list[str], user: str, cwd: Optional[str] = None,
             env: Optional[dict[str, str]] = None, tty: bool = False,
             check: bool = True) -> subprocess.CompletedProcess:
        return self.tart.exec(self.instance, self._argv(argv, user, cwd, env), tty=tty, check=check)

    def _push(self, remote: str, body: bytes, mode: Optional[str], uid: Optional[int],
              gid: Optional[int]) -> None:
        script = render_push_script(remote, mode, uid, gid)
        self.tart.exec(self.instance, [*self._sudo, "bash", "-c", script], input=body)

    def push_file(self, local: str, remote: str, mode: Optional[str] = None,
                  uid: Optional[int] = None, gid: Optional[int] = None) -> None:
        """Without `mode` the guest file gets the local file's mode, like `incus file push`."""
        path = Path(local)
        if mode is None:
            mode = format(stat.S_IMODE(path.stat().st_mode), "04o")
        self._push(remote, path.read_bytes(), mode, uid, gid)

    def push_text(self, remote: str, body: str, mode: Optional[str] = None,
                  uid: Optional[int] = None, gid: Optional[int] = None) -> None:
        self._push(remote, body.encode(), mode, uid, gid)

    def uid_of(self, user: str) -> int:
        if user not in self._uids:
            proc = self.exec(["id", "-u", user], user="root")
            self._uids[user] = int(proc.stdout.strip())
        return self._uids[user]


# ---------------------------------------------------------------------------
# Host-side files: launchd jobs, logs, metadata
# ---------------------------------------------------------------------------

def job_label(vm: str) -> str:
    return f"com.asbx.vm.{vm}"


def job_plist_path(vm: str) -> Path:
    return config.config_dir() / "launchd" / f"{job_label(vm)}.plist"


def vm_log_path(vm: str) -> Path:
    return config.config_dir() / "logs" / f"{vm}.log"


def state_path(vm: str) -> Path:
    return config.config_dir() / "state" / f"{vm}.json"


def snapshot_vm(instance: str, label: str) -> str:
    return f"{instance}-snap-{label}"


def _read_state(vm: str) -> dict[str, str]:
    path = state_path(vm)
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as e:
        raise AsbxError(f"unreadable sandbox state {path}: {e}")
    if not isinstance(data, dict):
        raise AsbxError(f"unreadable sandbox state {path}: not a mapping")
    return {str(k): str(v) for k, v in data.items()}


def _write_state(vm: str, data: dict[str, str]) -> None:
    """Atomic, so a crash mid-write never leaves unreadable metadata."""
    body = json.dumps(data, indent=2, sort_keys=True) + "\n"
    host.write_atomic(state_path(vm), body.encode(), 0o600)


def _forget(vm: str) -> None:
    """Removes the job plist, log and metadata kept for `vm`."""
    for path in (job_plist_path(vm), vm_log_path(vm), state_path(vm)):
        path.unlink(missing_ok=True)


def _job_sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------

class TartBackend:
    """`backend.SandboxBackend` over Tart, Softnet and launchd."""

    name = "tart"

    def __init__(self, tart: Optional[Tart] = None,
                 stat_fn: Callable[[str], os.stat_result] = os.stat) -> None:
        """`stat_fn` is what the Softnet guard inspects files with."""
        self.tart = tart or Tart()
        self._stat = stat_fn

    def guest(self, instance: str) -> Guest:
        return TartGuest(self.tart, instance)

    def state(self, instance: str) -> str:
        vm_state = self.tart.state(instance)
        if vm_state is None:
            return ABSENT
        return RUNNING if vm_state == "running" else STOPPED

    # -- host preparation ---------------------------------------------------

    def preflight(self, cfg: dict) -> None:
        self.tart.executable   # raises, with the install hint, when tart is not on PATH
        major = macos_major()
        if major is None or major < MIN_MACOS:
            raise AsbxError(f"Tart sandboxes need macOS {MIN_MACOS} or later (Softnet requires it); "
                            f"this host runs {platform.mac_ver()[0] or 'another OS'}")
        require_softnet(cfg["tart"]["softnet_dir"], self._stat)

    def base_image(self, cfg: dict) -> Optional[str]:
        """The `built_at` id `build-base` recorded. A VM of that name that
        asbx didn't build counts as none: only a built image is known to run
        its agent as root, with `admin` gone and ssh passwords off."""
        alias = cfg["image_alias"]
        if not self.tart.exists(alias):
            return None
        return _read_state(alias).get(_BUILT_AT) or None

    def _harden(self, g: TartGuest) -> None:
        """Makes the agent run as root from the next boot, so `admin` (and
        its passwordless sudo) can go."""
        g.exec(["mkdir", "-p", posixpath.dirname(AGENT_DROPIN_PATH)], user="root")
        g.push_text(AGENT_DROPIN_PATH, AGENT_DROPIN, mode="0644", uid=0, gid=0)

    def build_base(self, cfg: dict, prepare: Callable[[Guest], None]) -> str:
        """The stock image's agent runs as `admin`, so the build VM is first
        rebooted with the agent running as root and `admin` removed; only
        then does `prepare` run, through the root agent."""
        b = BASE_BUILD_VM
        alias = cfg["image_alias"]
        if self.tart.exists(b):
            raise AsbxError(f"VM {b} already exists; delete it first: tart delete {b}")
        # Checked before cloning, so a host that can't boot the build VM is left untouched.
        require_softnet(cfg["tart"]["softnet_dir"], self._stat)

        source = cfg["tart"]["base_image_source"]
        ui.ok(f"cloning {b} from {source}")
        self.tart.clone(source, b)
        resources = cfg["defaults"]["resources"]
        self._resize(b, resources["cpu"], resources["memory"], resources["disk"])
        # Whatever happens, the build VM ends up stopped and its job unloaded,
        # so a failed build leaves only a VM that 'tart delete' removes.
        try:
            ui.ok(f"starting {b}")
            self._boot(cfg, b, [])
            ui.ok("making the guest agent run as root")
            self._harden(TartGuest(self.tart, b, root_via_sudo=True))
            self._halt(b)
            ui.ok(f"restarting {b}")
            self._boot(cfg, b, [])
            g = TartGuest(self.tart, b)
            uid = (g.exec(["id", "-u"], user="root").stdout or "").strip()
            if uid != "0":
                raise AsbxError(f"the guest agent in {b} runs as uid {uid or 'unknown'}, "
                                "not root, after the reboot")
            g.exec(["userdel", "-r", STOCK_ADMIN], user="root")
            prepare(g)
        except AsbxError as e:
            raise AsbxError(f"{e}; delete the VM before retrying: tart delete {b}") from e
        finally:
            self._halt(b)

        ui.ok(f"publishing {b} as {alias}")
        if self.tart.exists(alias):
            self.tart.delete(alias)
        self.tart.rename(b, alias)
        _forget(b)
        built_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        _write_state(alias, {_BUILT_AT: built_at})
        ui.ok(f"base image ready: {alias} ({built_at})")
        return built_at

    # -- instance lifecycle -------------------------------------------------

    def _resize(self, vm: str, cpu: int, memory: str, disk: str) -> None:
        """Tart can only grow a disk, so a clone already at least `disk` keeps its size."""
        want_disk = disk_gb(disk)
        current_disk = (self.tart.vm(vm) or {}).get("Disk")
        grow = not isinstance(current_disk, int) or want_disk > current_disk
        self.tart.set(vm, cpu=int(cpu), memory_mb=memory_mb(memory),
                      disk_gb=want_disk if grow else None)

    def _render_job(self, cfg: dict, vm: str, vm_shares: list[Share]) -> bytes:
        """The launchd job that runs `vm`. The egress rules are on the `tart
        run` command line itself, so the VM can't run without them. No
        clipboard or audio: a sandbox gets neither the Mac's pasteboard nor
        its microphone. Each share is unnamed and alone on its tag, so its
        contents sit at the root of the guest mount; a named share would
        appear one directory down, under its name."""
        tart_path = self.tart.executable
        block, allow = softnet_rules(cfg["network"]["block_cidrs"])
        args = [tart_path, "run", vm, "--no-graphics", "--no-clipboard", "--no-audio",
                *(f"--dir={s.host}:ro,tag={s.tag}" for s in vm_shares),
                f"--net-softnet-block={block}", f"--net-softnet-allow={allow}"]
        # Tart runs the first softnet on PATH: the root-owned setuid one must come first.
        path = ":".join([cfg["tart"]["softnet_dir"], os.path.dirname(tart_path), "/usr/bin", "/bin"])
        return launchd.render_plist(job_label(vm), args, env={"PATH": path},
                                    keep_alive_on_failure=True, log_path=str(vm_log_path(vm)))

    def _wait_ready(self, vm: str) -> None:
        ui.ok(f"waiting for {vm} guest agent")
        g = TartGuest(self.tart, vm)
        last_stderr = ""
        for _ in range(READY_ATTEMPTS):
            proc = g.exec(["true"], user="root", check=False)
            if proc.returncode == 0:
                break
            # Until the agent listens every exec fails, so all failures are
            # retried; the last one is surfaced when the budget runs out.
            last_stderr = (proc.stderr or "").strip()
            time.sleep(READY_INTERVAL)
        else:
            suffix = f"\n{last_stderr}" if last_stderr else ""
            raise AsbxError(f"guest agent in {vm} did not come up in time; see {vm_log_path(vm)}{suffix}")
        guest.wait_for_cloud_init(g)

    def _boot(self, cfg: dict, vm: str, vm_shares: list[Share]) -> None:
        """Stores `vm`'s job as rendered from `cfg`, loads it and waits for
        the guest. Refuses unless the softnet first on the job's PATH,
        `tart.softnet_dir`, is the root-owned setuid one. Records the hash of
        the job the VM now runs, which `apply` compares against. When the
        guest never comes up and the VM isn't running, the job is unloaded,
        so launchd doesn't keep relaunching a VM that can't boot."""
        require_softnet(cfg["tart"]["softnet_dir"], self._stat)
        body = self._render_job(cfg, vm, vm_shares)
        path = job_plist_path(vm)
        launchd.write_plist(path, body)
        vm_log_path(vm).parent.mkdir(parents=True, exist_ok=True)
        launchd.reload(job_label(vm), path)
        self.meta_set(vm, _BOOTED_JOB, _job_sha256(body))
        try:
            self._wait_ready(vm)
        except BaseException:
            if self.state(vm) != RUNNING:
                launchd.bootout(job_label(vm))
            raise

    def _halt(self, vm: str) -> None:
        """Shuts the guest down, then unloads its job. `tart stop` alone
        terminates a Linux guest at once, losing whatever the guest hadn't
        written back yet (seen: a build's last minute of changes), so the
        guest powers itself off first and `tart stop` only forces a guest
        that doesn't. A guest that powers off makes `tart run` exit cleanly,
        which launchd doesn't restart. `sudo -n` works whether the agent runs
        as root or, before `build-base` hardens it, as `admin`."""
        if self.state(vm) == RUNNING:
            self.tart.exec(vm, ["sudo", "-n", "systemctl", "poweroff"], check=False)
            deadline = time.monotonic() + POWEROFF_TIMEOUT
            while self.state(vm) == RUNNING and time.monotonic() < deadline:
                time.sleep(1)
            if self.state(vm) == RUNNING:
                ui.warn(f"{vm} did not power off within {POWEROFF_TIMEOUT}s; forcing it off")
                self.tart.stop(vm)
        launchd.bootout(job_label(vm))

    def _mount_shares(self, vm: str, vm_shares: list[Share], strict: bool) -> None:
        """Writes the managed fstab block and mounts every share. `strict` (a
        fresh boot) raises when a share doesn't mount. On a VM that was
        already running, a share added since its boot only attaches at the
        next restart (`apply` reports that), so that only warns."""
        g = TartGuest(self.tart, vm)
        current = g.exec(["cat", "/etc/fstab"], user="root").stdout or ""
        wanted = render_fstab(current, vm_shares)
        if wanted != current:
            g.push_text("/etc/fstab", wanted, mode="0644", uid=0, gid=0)
        if not vm_shares:
            return
        proc = g.exec(["bash", "-c", render_mount_script(vm_shares)], user="root")
        failed = [line for line in (proc.stdout or "").splitlines() if line]
        if failed and strict:
            raise AsbxError(f"{vm}: could not mount {', '.join(failed)}\n{(proc.stderr or '').strip()}")
        for path in failed:
            ui.warn(f"{vm}: {path} is not mounted; it attaches when {vm} restarts")

    def create(self, cfg: dict, spec: SandboxSpec) -> None:
        """The egress rules bind at `start`, which always renders them from
        config. Fresh metadata replaces any an earlier sandbox of the same
        name left behind."""
        ui.ok(f"creating {spec.instance}")
        self.tart.clone(cfg["image_alias"], spec.instance)
        self._resize(spec.instance, spec.cpu, spec.memory, spec.disk)
        _write_state(spec.instance, {META_EGRESS_MODE: EGRESS_MODE})

    def start(self, cfg: dict, spec: SandboxSpec) -> None:
        instance = spec.instance
        vm_shares = shares(spec.mounts)
        state = self.state(instance)
        if state == ABSENT:
            raise AsbxError(f"{instance} does not exist")
        if state == RUNNING:
            # Only asbx's job runs a sandbox with its egress rules; a VM
            # started any other way may have none.
            if not launchd.agent_loaded(job_label(instance)):
                raise AsbxError(f"{instance} is running outside asbx, possibly without its egress "
                                f"policy; stop it with 'tart stop {instance}' and retry")
            self._wait_ready(instance)
            self._mount_shares(instance, vm_shares, strict=False)
            return
        ui.ok(f"starting {instance}")
        self._boot(cfg, instance, vm_shares)
        self._mount_shares(instance, vm_shares, strict=True)

    def apply(self, cfg: dict, spec: SandboxSpec) -> bool:
        """Stores the job `start` would load. A running VM keeps the command
        line it booted with, so it needs a restart whenever that differs from
        the job rendered now (mounts, egress rules), even when the stored job
        already matches, as after a restart that was interrupted."""
        instance = spec.instance
        body = self._render_job(cfg, instance, shares(spec.mounts))
        path = job_plist_path(instance)
        if not path.exists() or path.read_bytes() != body:
            launchd.write_plist(path, body)
        return (self.state(instance) == RUNNING
                and self.meta_get(instance, _BOOTED_JOB) != _job_sha256(body))

    def stop(self, instance: str) -> None:
        self._halt(instance)

    def restart(self, cfg: dict, spec: SandboxSpec) -> None:
        self.stop(spec.instance)
        self.start(cfg, spec)

    def delete(self, instance: str) -> None:
        snaps = [snapshot_vm(instance, label) for label in self.snapshots(instance)]
        self._halt(instance)
        vms = ([instance] if self.tart.exists(instance) else []) + snaps
        if vms:
            self.tart.delete(*vms)
        for vm in [instance, *snaps]:
            _forget(vm)

    # -- addressing ---------------------------------------------------------

    def ssh_route(self, cfg: dict, instance: str) -> SshRoute:
        """The address changes on every boot (Softnet picks a fresh subnet),
        so ssh asks `asbx ssh-proxy` for it at connect time. ssh runs the
        command with the caller's environment, which may lack tart on PATH
        or the config dir asbx runs with now, so the command sets both."""
        env = [f"PATH={os.path.dirname(self.tart.executable)}:/usr/bin:/bin"]
        config_dir = os.environ.get(config.CONFIG_DIR_ENV)
        if config_dir:
            env.append(f"{config.CONFIG_DIR_ENV}={config_dir}")
        command = " ".join(shlex.quote(part) for part in
                           ("/usr/bin/env", *env, sys.executable, str(hostsetup.ENTRY_POINT),
                            "ssh-proxy", instance))
        return SshRoute(host_key_alias=instance, proxy_command=command)

    def ssh_address(self, cfg: dict, instance: str) -> Optional[str]:
        address = self.tart.ip(instance)
        if address is None:
            return None
        try:
            return str(ipaddress.IPv4Address(address))
        except ValueError:
            return None

    def pinned_ipv4(self, instance: str) -> Optional[str]:
        return None

    # -- snapshots ----------------------------------------------------------

    def snapshots(self, instance: str) -> list[str]:
        """Labels of the clones `snapshot` made. A VM merely named like a
        snapshot (another sandbox `<instance>-snap-x`) carries no
        `snapshot_of` mark, so `delete` never removes it."""
        prefix = snapshot_vm(instance, "")
        return [name[len(prefix):] for name in (str(vm.get("Name", "")) for vm in self.tart.list())
                if name.startswith(prefix) and _read_state(name).get(_SNAPSHOT_OF) == instance]

    def snapshot(self, cfg: dict, spec: SandboxSpec, label: str) -> None:
        """A clone of the stopped VM, with a copy of its metadata. A running
        VM is stopped for the copy and started again from config."""
        instance = spec.instance
        snap = snapshot_vm(instance, label)
        if label in self.snapshots(instance) or self.tart.exists(snap):
            raise AsbxError(f"snapshot {instance}/{label} already exists; pick another label")
        running = self.state(instance) == RUNNING
        if running:
            ui.ok(f"stopping {instance} for the snapshot")
            self._halt(instance)
        try:
            self.tart.clone(instance, snap)
            meta = _read_state(instance)
            meta.pop(_BOOTED_JOB, None)
            _write_state(snap, {**meta, _SNAPSHOT_OF: instance})
        finally:
            if running:
                self.start(cfg, spec)
        ui.ok(f"snapshot {instance}/{label} created")

    def restore(self, cfg: dict, spec: SandboxSpec, label: str) -> None:
        instance = spec.instance
        if label not in self.snapshots(instance):
            raise AsbxError(f"{instance}: no snapshot named '{label}'")
        snap = snapshot_vm(instance, label)
        self._halt(instance)
        if self.tart.exists(instance):
            self.tart.delete(instance)
        self.tart.clone(snap, instance)
        meta = _read_state(snap)
        meta.pop(_SNAPSHOT_OF, None)
        _write_state(instance, meta)
        self.start(cfg, spec)

    # -- metadata -----------------------------------------------------------

    def meta_get(self, instance: str, key: str) -> str:
        return _read_state(instance).get(key, "")

    def meta_set(self, instance: str, key: str, value: str) -> None:
        _write_state(instance, {**_read_state(instance), key: value})

    # -- doctor -------------------------------------------------------------

    def doctor_checks(self, cfg: Optional[dict], fix: bool) -> list[tuple[str, str]]:
        """`fix` changes nothing here: installing Softnet needs sudo, so its
        row prints the commands instead."""
        results: list[tuple[str, str]] = []
        check = partial(add_check, results)

        tart_on_path = host.which("tart")
        version = self.tart.version() if tart_on_path else ""
        check(f"tart {version} on PATH" if version else "tart on PATH", tart_on_path,
              "not found; install it: brew install openai/tools/tart")

        release = platform.mac_ver()[0]
        major = macos_major()
        check(f"macOS {release or 'unknown'} (Softnet needs {MIN_MACOS} or later)",
              major is not None and major >= MIN_MACOS, "too old or not macOS")
        check("Apple silicon", platform.machine() == "arm64",
              f"machine is {platform.machine()}; Tart needs Apple silicon")

        if cfg is None:
            return results

        softnet_dir = cfg["tart"]["softnet_dir"]
        problem = softnet_problem(softnet_dir, self._stat)
        check(f"softnet in {softnet_dir} is root-owned and setuid", problem is None, problem or "")

        if tart_on_path:
            try:
                built = self.base_image(cfg)
            except AsbxError as e:
                check("base image exists", False, str(e))
            else:
                check(f"base image {cfg['image_alias']} exists", built is not None,
                      "not found or not built by asbx; run 'asbx build-base'")
        return results
