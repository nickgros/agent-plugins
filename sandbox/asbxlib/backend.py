"""The seam between asbx and the VM service that runs sandboxes.

`up`, `lifecycle`, `doctor` and `build-base` talk to a `SandboxBackend`;
everything that acts inside a guest (guest setup, provision, auth, AWS
credentials, harness settings) talks to a `Guest`. Neither knows which VM
service is underneath. Incus backs Linux hosts (`incusbackend.py`), Tart with
Softnet backs macOS hosts (`tartbackend.py`); see ADR 0006.

Invariant every backend keeps: a sandbox never boots without its egress
policy. `create` binds the policy and `start` refuses to boot without it, so
there is no window in which a guest runs unfiltered (ADR 0003).
"""
from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

from .manifest import Mount

# Normalized instance states. Backends map their own vocabulary onto these.
ABSENT = "absent"
STOPPED = "stopped"
RUNNING = "running"

# Instance metadata keys (`SandboxBackend.meta_get`/`meta_set`).
META_GROUP = "group"
META_BASE_IMAGE = "base_image"
META_MANIFEST_SHA256 = "manifest_sha256"
META_EGRESS_MODE = "egress_mode"


@dataclass(frozen=True)
class SandboxSpec:
    """Everything a backend needs to create and boot one sandbox safely."""
    instance: str
    group: str
    cpu: int
    memory: str                 # Incus size string, e.g. "8GiB"
    disk: str                   # Incus size string, e.g. "30GiB"
    mounts: list[Mount] = field(default_factory=list)   # read-only host dirs
    ipv4: Optional[str] = None  # address to pin on a new sandbox (Incus only; rebuild keeps it)


@dataclass(frozen=True)
class SshRoute:
    """How the generated ssh config reaches a sandbox. Built from host-side
    data only, never from anything the guest reports (ADR 0004).

    Exactly one of `hostname` (a fixed name or address) and `proxy_command`
    (resolves the current address at connect time) is set. `host_key_alias`
    is the name the sandbox's host key is stored under in known_hosts."""
    host_key_alias: str
    hostname: Optional[str] = None
    proxy_command: Optional[str] = None


def command_script(argv: list[str], cwd: Optional[str] = None,
                   env: Optional[dict[str, str]] = None) -> str:
    """`argv` as one bash command line behind `cd <cwd> &&` and `env K=V`
    prefixes, for exec channels that take no cwd or env of their own."""
    parts = []
    if cwd:
        parts.append(f"cd {shlex.quote(cwd)} &&")
    env_prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in (env or {}).items())
    if env_prefix:
        parts.append(f"env {env_prefix}")
    parts.append(" ".join(shlex.quote(a) for a in argv))
    return " ".join(parts)


def runuser_argv(argv: list[str], user: str, cwd: Optional[str] = None,
                 env: Optional[dict[str, str]] = None) -> list[str]:
    """The guest command that runs `argv` as non-root `user`. `runuser`
    initializes the session, so it gets the user's supplementary groups
    (docker), which exec'ing straight as the uid leaves out. The cwd
    defaults to the user's home: the exec channel's own cwd is often one
    the user can't even stat."""
    return ["runuser", "-u", user, "--", "bash", "-c",
            command_script(argv, cwd or f"/home/{user}", env)]


class Guest(Protocol):
    """Runs commands in, and pushes files into, one running sandbox.

    Non-root commands run through `runuser` so the session gets the user's
    supplementary groups (docker) and starts in `cwd`, default the user's
    home (`runuser_argv` builds that command for every backend)."""
    instance: str

    def exec(self, argv: list[str], user: str, cwd: Optional[str] = None,
             env: Optional[dict[str, str]] = None, tty: bool = False,
             check: bool = True) -> subprocess.CompletedProcess: ...

    def push_file(self, local: str, remote: str, mode: Optional[str] = None,
                  uid: Optional[int] = None, gid: Optional[int] = None) -> None: ...

    def push_text(self, remote: str, body: str, mode: Optional[str] = None,
                  uid: Optional[int] = None, gid: Optional[int] = None) -> None: ...

    def uid_of(self, user: str) -> int: ...


class SandboxBackend(Protocol):
    name: str                   # "incus" | "tart"

    def guest(self, instance: str) -> Guest: ...

    def state(self, instance: str) -> str:
        """ABSENT, STOPPED or RUNNING."""

    def preflight(self, cfg: dict) -> None:
        """Host-side checks and refreshes `up` runs before touching a sandbox
        (Incus: refresh the egress ACL, require the ingress firewall; Tart:
        require the root-owned Softnet). Raises when a sandbox can't be run safely."""

    def base_image(self, cfg: dict) -> Optional[str]:
        """An id for the current base image (recorded as META_BASE_IMAGE), or
        None when `build-base` has not run."""

    def build_base(self, cfg: dict, prepare: Callable[[Guest], None]) -> str:
        """Boots a throwaway VM from the backend's source image, makes it a
        VM asbx can image (backend-specific hardening), then hands it to
        `prepare`, which runs the base recipe as root, customizes and cleans
        it (`baseimage.prepare_build_vm`). Publishes the result as the base
        image. Refuses when a previous build VM is still around. Returns the
        new base image id."""

    def create(self, cfg: dict, spec: SandboxSpec) -> None:
        """Creates a stopped sandbox from the base image with its egress policy bound."""

    def start(self, cfg: dict, spec: SandboxSpec) -> None:
        """Boots the sandbox with its egress policy and `spec.mounts` attached,
        then waits until `guest()` can run commands and first-boot setup is
        done. A no-op boot-wise when already running. Raises rather than boot
        without the egress policy."""

    def apply(self, cfg: dict, spec: SandboxSpec) -> bool:
        """Reconciles host-side config of an existing sandbox (mounts, egress
        policy, NIC pinning) with `spec`. Returns True when the change only
        reaches the guest after a restart."""

    def stop(self, instance: str) -> None: ...

    def restart(self, cfg: dict, spec: SandboxSpec) -> None: ...

    def delete(self, instance: str) -> None:
        """Removes the sandbox and everything the backend keeps for it,
        including snapshots and metadata. Absent is not an error."""

    def ssh_route(self, cfg: dict, instance: str) -> SshRoute: ...

    def ssh_address(self, cfg: dict, instance: str) -> Optional[str]:
        """The address port 22 answers on right now, from host-side data, or
        None when it can't be determined yet."""

    def pinned_ipv4(self, instance: str) -> Optional[str]:
        """The address ingress clients and `rebuild` rely on, or None when the
        backend doesn't pin addresses."""

    def snapshot(self, cfg: dict, spec: SandboxSpec, label: str) -> None:
        """Saves the sandbox as `label`. A running sandbox may be stopped for
        the copy; it is booted again as `start` would boot it."""

    def snapshots(self, instance: str) -> list[str]: ...

    def restore(self, cfg: dict, spec: SandboxSpec, label: str) -> None:
        """Rolls the sandbox back to `label` and boots it. Raises before
        changing anything when `label` doesn't exist."""

    def meta_get(self, instance: str, key: str) -> str:
        """'' when unset."""

    def meta_set(self, instance: str, key: str, value: str) -> None: ...

    def doctor_checks(self, cfg: Optional[dict], fix: bool) -> list[tuple[str, str]]:
        """Backend-specific host readiness as (PASS/WARN/FAIL, message) rows
        (`add_check` builds them)."""


def add_check(rows: list[tuple[str, str]], label: str, ok_cond: object, fail_msg: str,
              warn_cond: object = False) -> None:
    """Appends one doctor row: PASS `label` when `ok_cond` holds, otherwise
    WARN (when `warn_cond` holds) or FAIL, with `fail_msg` after the label."""
    if ok_cond:
        rows.append(("PASS", label))
    else:
        rows.append(("WARN" if warn_cond else "FAIL", f"{label}: {fail_msg}"))
