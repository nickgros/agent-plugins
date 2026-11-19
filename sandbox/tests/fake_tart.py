"""Test doubles for the Tart backend that never spawn `tart` or `launchctl`.

`FakeTart` is the runner of an `asbxlib.tart.Tart`; `FakeLaunchctl` replaces
`asbxlib.launchd.run_launchctl`. Linked together they keep a small VM
inventory consistent: loading a VM's launchd job marks it running, as the
real `tart run` would, and the job's program runs until the VM stops.
"""
from __future__ import annotations

import json
import plistlib
import shlex
import subprocess
from pathlib import Path
from typing import Optional, Union

from asbxlib.tart import Tart

EXECUTABLE = "/opt/homebrew/bin/tart"


def _matches(argv: list[str], tokens: tuple[str, ...]) -> bool:
    """Same rule as `fake_incus`: each token is a substring of the
    newline-joined argv, in order, so a token can match inside the `bash -c`
    script `TartGuest` builds."""
    joined = "\n".join(argv)
    pos = 0
    for tok in tokens:
        i = joined.find(tok, pos)
        if i == -1:
            return False
        pos = i + len(tok)
    return True


class FakeTart:
    """A `Tart` runner: records argv, replays scripted results, and tracks a
    VM inventory for `list`, `clone`, `set`, `stop`, `delete` and `rename`
    (stub rules win over the inventory)."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []                        # argv after the tart executable
        self.inputs: list[Union[str, bytes, None]] = []         # stdin per call, parallel to calls
        self.events: list[list[str]] = []                       # tart and launchctl calls, in order
        self.vms: dict[str, dict] = {}                          # name -> tart list fields
        self.launched: set[str] = set()                         # VMs a loaded launchd job's `tart run` runs
        self._rules: list[tuple[tuple[str, ...], int, str, str]] = []
        self._hangs: list[tuple[str, ...]] = []
        self.ignore_poweroff = False                            # a guest that won't shut itself down

    def tart(self) -> Tart:
        return Tart(runner=self, executable=EXECUTABLE)

    def add_vm(self, name: str, state: str = "stopped", disk: int = 20) -> None:
        self.vms[name] = {"State": state, "Disk": disk, "Source": "local"}

    def stub(self, *tokens: str, rc: int = 0, stdout: str = "", stderr: str = "") -> None:
        """First matching rule wins; a rule matches when every token appears in
        argv in order (subsequence match)."""
        self._rules.append((tokens, rc, stdout, stderr))

    def hang(self, *tokens: str) -> None:
        """Matching calls never finish, like an exec into a guest agent that
        never answers. Given a timeout they raise `subprocess.TimeoutExpired`,
        as the real runner does; without one the test fails, since asbx
        would hang there."""
        self._hangs.append(tokens)

    def set_state(self, name: str, state: str) -> None:
        """What `tart list` reports for `name` from now on. A VM that stops
        takes its `tart run`, and so its launchd job's program, with it."""
        self.vms[name]["State"] = state
        if state != "running":
            self.launched.discard(name)

    def pushed(self, remote: str) -> Union[str, bytes, None]:
        """The body of the last push to `remote`."""
        marker = f'mv -f -- "$tmp" {shlex.quote(remote)}'
        for argv, body in zip(reversed(self.calls), reversed(self.inputs)):
            if argv and argv[0] == "exec" and marker in argv[-1]:
                return body
        raise AssertionError(f"nothing pushed to {remote}")

    def index(self, *tokens: str) -> int:
        """Position in `events` of the first event matching `tokens`."""
        for i, event in enumerate(self.events):
            if _matches(event, tokens):
                return i
        raise AssertionError(f"no event matches {tokens}")

    def __call__(self, argv: list[str], *, input: Union[str, bytes, None] = None,
                 capture: bool = True, tty: bool = False,
                 timeout: Optional[float] = None) -> subprocess.CompletedProcess:
        assert argv[0] == EXECUTABLE
        args = list(argv[1:])
        self.calls.append(args)
        self.inputs.append(input)
        self.events.append(["tart", *args])
        if any(_matches(args, tokens) for tokens in self._hangs):
            if timeout is None:
                raise AssertionError(f"tart {' '.join(args)} would hang forever: no timeout")
            raise subprocess.TimeoutExpired(argv, timeout)
        for tokens, rule_rc, rule_stdout, rule_stderr in self._rules:
            if _matches(args, tokens):
                rc, stdout, stderr = rule_rc, rule_stdout, rule_stderr
                break
        else:
            rc, stdout, stderr = self._inventory(args)
        if tty or not capture:
            stdout = stderr = None  # a real tty or streamed run captures nothing
        return subprocess.CompletedProcess(args=argv, returncode=rc, stdout=stdout, stderr=stderr)

    def _inventory(self, args: list[str]) -> tuple[int, str, str]:
        cmd = args[0] if args else ""
        if cmd == "list":
            return 0, json.dumps([{"Name": name, **vm} for name, vm in self.vms.items()]), ""
        if cmd == "clone":
            source, name = args[1], args[2]
            if name in self.vms:
                return 1, "", f"VM {name} already exists"
            self.add_vm(name, disk=self.vms.get(source, {}).get("Disk", 20))
        elif cmd == "delete":
            missing = [name for name in args[1:] if name not in self.vms]
            if missing:
                return 1, "", f"VM {missing[0]} does not exist"
            for name in args[1:]:
                del self.vms[name]
        elif cmd == "rename":
            self.vms[args[2]] = self.vms.pop(args[1])
        elif cmd == "stop":
            name = args[1]
            if name not in self.vms:
                return 1, "", f"VM {name} does not exist"
            if self.vms[name]["State"] == "stopped":
                return 1, "", f'VM "{name}" is not running'   # as real tart fails
            self.set_state(name, "stopped")
        elif cmd == "exec" and args[-4:] == ["sudo", "-n", "systemctl", "poweroff"]:
            vm = args[-5]
            if self.vms.get(vm, {}).get("State") == "running" and not self.ignore_poweroff:
                self.set_state(vm, "stopped")   # the guest shuts itself down
        elif cmd == "set" and "--disk-size" in args:
            self.vms[args[1]]["Disk"] = int(args[args.index("--disk-size") + 1])
        return 0, "", ""


class FakeLaunchctl:
    """A `launchd.run_launchctl` that tracks loaded jobs. Bootstrapping a
    `tart run` job marks its VM running in the linked `FakeTart`, with the
    job's program running; bootout marks it stopped. `print` shows a pid
    only while that program runs: a job stays loaded after its VM stops on
    its own. Jobs that don't run a VM run once at load and exit."""

    def __init__(self, tart: Optional[FakeTart] = None) -> None:
        self.tart = tart
        self.calls: list[list[str]] = []
        self.loaded: dict[str, bytes] = {}          # label -> plist body
        self.bootstrapped: list[bytes] = []         # every plist body bootstrapped, in order
        self.bootstrap_rc = 0

    def _vm(self, body: bytes) -> Optional[str]:
        args = plistlib.loads(body)["ProgramArguments"]
        return args[2] if len(args) > 2 and args[1] == "run" else None

    def _set_state(self, body: bytes, state: str) -> None:
        if self.tart is None:
            return
        vm = self._vm(body)
        if vm in self.tart.vms:
            self.tart.set_state(vm, state)
            if state == "running":
                self.tart.launched.add(vm)

    def _print(self, target: str, body: bytes) -> str:
        """The parts of real `launchctl print` output asbx reads."""
        running = self.tart is not None and self._vm(body) in self.tart.launched
        process = "\tstate = running\n\tpid = 4242\n" if running else "\tstate = not running\n"
        return f"{target} = {{\n\tactive count = {int(running)}\n{process}}}\n"

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        if self.tart is not None:
            self.tart.events.append(["launchctl", *args])
        rc, stdout, stderr = 0, "", ""
        cmd = args[0]
        if cmd == "print":
            label = args[1].rsplit("/", 1)[-1]
            body = self.loaded.get(label)
            if body is None:
                rc, stderr = 113, f'Could not find service "{label}" in domain for port'
            else:
                stdout = self._print(args[1], body)
        elif cmd == "bootstrap":
            body = Path(args[2]).read_bytes()
            label = plistlib.loads(body)["Label"]
            if self.bootstrap_rc or label in self.loaded:
                rc, stderr = self.bootstrap_rc or 5, "Bootstrap failed: 5: Input/output error"
            else:
                self.loaded[label] = body
                self.bootstrapped.append(body)
                self._set_state(body, "running")
        elif cmd == "bootout":
            body = self.loaded.pop(args[1].rsplit("/", 1)[-1], None)
            if body:
                self._set_state(body, "stopped")
        return subprocess.CompletedProcess(args=["launchctl", *args], returncode=rc,
                                           stdout=stdout, stderr=stderr)
