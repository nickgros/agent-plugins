"""A test double for asbxlib.incus.Incus that never spawns `incus`."""
from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from asbxlib.errors import AsbxError
from asbxlib.incus import Incus


def _matches(argv: list[str], tokens: tuple[str, ...]) -> bool:
    """`tokens` matches when each token is a substring of the joined argv, in
    order. Elements are newline-joined (not space-joined) so a token can match
    within a single multi-word element (e.g. the `bash -c "<script>"` string
    non-root `exec_in` builds) while still requiring later tokens to be found
    after earlier ones, even across separate argv elements."""
    joined = "\n".join(argv)
    pos = 0
    for tok in tokens:
        i = joined.find(tok, pos)
        if i == -1:
            return False
        pos = i + len(tok)
    return True


class FakeIncus(Incus):
    """An Incus that never spawns incus: records argv, replays scripted results,
    and captures pushed file bodies."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[list[str]] = []          # argv after "incus"
        self.pushed: dict[str, str] = {}          # guest path -> body
        self._rules: list[tuple[tuple[str, ...], int, str, str]] = []
        self.stub("id", "-u", stdout="1000\n")    # uid_of, unless a test overrides it first

    def stub(self, *tokens: str, rc: int = 0, stdout: str = "", stderr: str = "") -> None:
        """First matching rule wins; a rule matches when every token appears in
        argv in order (subsequence match)."""
        self._rules.append((tokens, rc, stdout, stderr))

    def set_instance(self, exists: bool = True, state: str = "Running") -> None:
        """Scripts `info` (exists) and `list --format json` (state)."""
        self.stub("info", rc=0 if exists else 1)
        if exists:
            self.stub("list", "--format", "json", stdout=f'[{{"status": "{state}"}}]')
        else:
            self.stub("list", "--format", "json", stdout="[]")

    def set_devices(self, devices: dict[str, dict[str, str]]) -> None:
        """Scripts `config show` with a yaml body carrying these devices."""
        body = yaml.safe_dump({"devices": devices})
        self.stub("config", "show", stdout=body)

    def set_snapshots(self, labels: list[str]) -> None:
        """Scripts `query /1.0/instances/<name>/snapshots`."""
        body = "[" + ", ".join(f'"/1.0/instances/x/snapshots/{label}"' for label in labels) + "]"
        self.stub("query", stdout=body)

    def run(self, *args: str, check: bool = True, capture: bool = True,
            tty: bool = False, stdin: str | None = None) -> subprocess.CompletedProcess:
        argv = list(args)
        self.calls.append(argv)
        rc, stdout, stderr = 0, "", ""
        for tokens, rule_rc, rule_stdout, rule_stderr in self._rules:
            if _matches(argv, tokens):
                rc, stdout, stderr = rule_rc, rule_stdout, rule_stderr
                break
        proc = subprocess.CompletedProcess(args=["incus", *argv], returncode=rc,
                                            stdout=stdout, stderr=stderr)
        if check and rc != 0:
            raise AsbxError(f"command failed: {' '.join(['incus', *argv])}\n{stderr.strip()}")
        return proc

    def file_push(self, instance: str, local: str, remote: str,
                  mode: str | None = None, uid: int | None = None,
                  gid: int | None = None) -> None:
        args = ["file", "push", local, f"{instance}{remote}"]
        if mode is not None:
            args += ["--mode", mode]
        if uid is not None:
            args += ["--uid", str(uid)]
        if gid is not None:
            args += ["--gid", str(gid)]
        self.calls.append(args)
        self.pushed[remote] = Path(local).read_text()
