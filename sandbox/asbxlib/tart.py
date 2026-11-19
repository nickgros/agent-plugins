"""`Tart`: the only place asbx spawns `tart`, the macOS VM manager behind the
Tart backend (`tartbackend.py`, ADR 0006).

Every call goes through one injectable runner, so tests replay scripted
results instead of spawning tart (`tests/fake_tart.py`).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Callable, Optional, Union

from .errors import AsbxError

Input = Union[str, bytes, None]
Runner = Callable[..., subprocess.CompletedProcess]


def _decode(data: Optional[bytes]) -> Optional[str]:
    return None if data is None else data.decode("utf-8", errors="replace")


def spawn(argv: list[str], *, input: Input = None, capture: bool = True,
          tty: bool = False) -> subprocess.CompletedProcess:
    """Runs `argv`. `input` may be bytes (a pushed binary), yet output is
    always decoded text so callers inspect str. A tty run inherits the
    terminal and captures nothing; an uncaptured run streams to it."""
    if tty:
        return subprocess.run(argv, check=False)
    data = input.encode() if isinstance(input, str) else input
    pipe = subprocess.PIPE if capture else None
    proc = subprocess.run(argv, input=data, stdout=pipe, stderr=pipe, check=False)
    return subprocess.CompletedProcess(argv, proc.returncode, _decode(proc.stdout), _decode(proc.stderr))


class Tart:
    def __init__(self, runner: Runner = spawn, executable: Optional[str] = None) -> None:
        self._runner = runner
        self._executable = executable

    @property
    def executable(self) -> str:
        """Absolute path of `tart` on PATH. launchd jobs run it by this path,
        since they don't get the user's PATH."""
        if self._executable is None:
            found = shutil.which("tart")
            if not found:
                raise AsbxError("tart not found on PATH; install it: brew install openai/tools/tart")
            self._executable = os.path.abspath(found)
        return self._executable

    def run(self, *args: str, input: Input = None, capture: bool = True, tty: bool = False,
            check: bool = True) -> subprocess.CompletedProcess:
        proc = self._runner([self.executable, *args], input=input, capture=capture, tty=tty)
        if check and proc.returncode != 0:
            stderr = getattr(proc, "stderr", None) or ""
            raise AsbxError(f"command failed: tart {' '.join(args)}\n{stderr.strip()}")
        return proc

    def version(self) -> str:
        """'' when tart can't report one."""
        proc = self.run("--version", check=False)
        return (proc.stdout or "").strip() if proc.returncode == 0 else ""

    def list(self) -> list[dict]:
        """Local VMs as `tart list` reports them (Name, State, Disk, ...)."""
        proc = self.run("list", "--source", "local", "--format", "json")
        try:
            data = json.loads(proc.stdout or "[]")
        except json.JSONDecodeError as e:
            raise AsbxError(f"tart list printed unreadable JSON: {e}")
        if not isinstance(data, list):
            raise AsbxError("tart list printed JSON that is not a list")
        return data

    def vm(self, name: str) -> Optional[dict]:
        return next((vm for vm in self.list() if vm.get("Name") == name), None)

    def state(self, name: str) -> Optional[str]:
        """Tart's state ("running", "stopped", "suspended"), None when absent."""
        vm = self.vm(name)
        return None if vm is None else str(vm.get("State", ""))

    def exists(self, name: str) -> bool:
        return self.vm(name) is not None

    def clone(self, source: str, name: str) -> None:
        """Pulls `source` first when it is a remote image; the progress streams to the terminal."""
        self.run("clone", source, name, capture=False)

    def set(self, name: str, *, cpu: Optional[int] = None, memory_mb: Optional[int] = None,
            disk_gb: Optional[int] = None) -> None:
        """`disk_gb` can only grow the disk; the root filesystem grows on the next boot."""
        args = ["set", name]
        if cpu is not None:
            args += ["--cpu", str(cpu)]
        if memory_mb is not None:
            args += ["--memory", str(memory_mb)]
        if disk_gb is not None:
            args += ["--disk-size", str(disk_gb)]
        self.run(*args)

    def ip(self, name: str, wait: int = 0) -> Optional[str]:
        """The address from Softnet's root-owned DHCP reservations. Never
        `--resolver agent`, which asks the guest (ADR 0004)."""
        args = ["ip", name, "--resolver", "dhcp"]
        if wait:
            args += ["--wait", str(wait)]
        proc = self.run(*args, check=False)
        address = (proc.stdout or "").strip()
        return address if proc.returncode == 0 and address else None

    def exec(self, name: str, argv: list[str], *, input: Input = None, tty: bool = False,
             check: bool = True) -> subprocess.CompletedProcess:
        """Runs `argv` through the guest agent as whatever user the agent runs
        as. `input` is fed to the command's stdin. `tty` inherits the host's
        stdio; a guest pty is requested only when that stdio is a terminal,
        since `tart exec -t` aborts without one (e.g. output to a log file)."""
        if tty:
            flags = ["-t", "-i"] if os.isatty(0) and os.isatty(1) else ["-i"]
        else:
            flags = ["-i"] if input is not None else []
        return self.run("exec", *flags, name, *argv, input=input, tty=tty, check=check)

    def stop(self, name: str, timeout: Optional[int] = None) -> None:
        args = ["stop", name]
        if timeout is not None:
            args += ["--timeout", str(timeout)]
        self.run(*args)

    def delete(self, *names: str) -> None:
        self.run("delete", *names)

    def rename(self, name: str, new_name: str) -> None:
        self.run("rename", name, new_name)
