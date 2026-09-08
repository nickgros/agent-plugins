"""Tests for asbxlib.incus: Incus.exec_in — the runuser wrapper is the only
shell asbx builds."""
from __future__ import annotations

import shlex
import subprocess

from asbxlib.incus import Incus


class _RecordingIncus(Incus):
    """An Incus whose `run` records argv instead of spawning incus."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[list[str]] = []

    def run(self, *args: str, **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")


def test_exec_in_non_root_shell_quotes_cwd_env_and_argv(tmp_path):
    workdir = tmp_path / "work dir"
    workdir.mkdir()
    canary = tmp_path / "pwned"
    payload = f"x y'z; touch {canary}"

    incus = _RecordingIncus()
    incus.exec_in(
        "sandbox-demo",
        ["printf", "%s\n", payload],
        user="agent",
        cwd=str(workdir),
        env={"MSG": f"a b; touch {canary}"},
    )

    argv = incus.calls[0]
    assert argv[:9] == ["exec", "sandbox-demo", "--", "runuser", "-u", "agent", "--",
                        "bash", "-c"]
    script = argv[9]
    assert shlex.split(script) == [
        "cd", str(workdir), "&&", "env", f"MSG=a b; touch {canary}",
        "printf", "%s\n", payload,
    ]

    # The script is handed to a real shell, so prove the shell agrees.
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True)
    assert proc.stdout == payload + "\n"
    assert not canary.exists(), "argument was interpreted as a command"


def test_exec_in_non_root_defaults_cwd_to_the_guest_home():
    incus = _RecordingIncus()
    incus.exec_in("sandbox-demo", ["pwd"], user="agent")
    # An unset cwd would otherwise inherit root's cwd, which the guest user
    # often cannot even stat.
    assert shlex.split(incus.calls[0][9]) == ["cd", "/home/agent", "&&", "pwd"]


def test_exec_in_root_passes_argv_verbatim_without_a_shell():
    incus = _RecordingIncus()
    incus.exec_in(
        "sandbox-demo",
        ["printf", "a b; touch /tmp/pwned"],
        user="root",
        cwd="/work dir",
        env={"MSG": "a b"},
    )
    assert incus.calls[0] == [
        "exec", "sandbox-demo", "--cwd", "/work dir", "--env", "MSG=a b",
        "--", "printf", "a b; touch /tmp/pwned",
    ]
