import signal
import subprocess
import sys

import pytest

from asbxlib import tart as tart_mod
from asbxlib.errors import AsbxError
from asbxlib.tart import Tart, spawn
from fake_tart import EXECUTABLE, FakeTart


@pytest.fixture
def fake():
    return FakeTart()


def test_run_raises_with_stderr_on_failure(fake):
    fake.stub("delete", rc=1, stderr="VM nope does not exist")
    with pytest.raises(AsbxError, match="tart delete nope\nVM nope does not exist"):
        fake.tart().delete("nope")


def test_run_unchecked_returns_the_failure(fake):
    fake.stub("ip", rc=1)
    assert fake.tart().run("ip", "x", check=False).returncode == 1


def test_a_run_past_its_timeout_fails_checked_or_not():
    """The runner kills tart and raises, as `subprocess.run` does; Tart
    reports that as a failed run, so an unchecked caller carries on."""
    seen = {}

    def runner(argv, *, input=None, capture=True, tty=False, timeout=None):
        seen["timeout"] = timeout
        raise subprocess.TimeoutExpired(argv, timeout)

    t = Tart(runner=runner, executable=EXECUTABLE)
    proc = t.exec("vm", ["sleep", "9"], check=False, timeout=5)
    assert seen["timeout"] == 5
    assert proc.returncode == -signal.SIGKILL
    assert proc.stderr == "timed out after 5s"
    with pytest.raises(AsbxError, match="tart exec vm sleep 9\ntimed out after 5s"):
        t.exec("vm", ["sleep", "9"], timeout=5)


def test_missing_tart_is_a_user_facing_error(monkeypatch):
    monkeypatch.setattr(tart_mod.shutil, "which", lambda name: None)
    with pytest.raises(AsbxError, match="tart not found on PATH"):
        Tart(runner=FakeTart()).list()


def test_executable_is_the_absolute_path_on_path(monkeypatch):
    monkeypatch.setattr(tart_mod.shutil, "which", lambda name: EXECUTABLE)
    assert Tart(runner=FakeTart()).executable == EXECUTABLE


def test_list_state_and_exists_read_local_vms(fake):
    fake.add_vm("a", state="running")
    t = fake.tart()
    assert [vm["Name"] for vm in t.list()] == ["a"]
    assert fake.calls[0] == ["list", "--source", "local", "--format", "json"]
    assert t.state("a") == "running"
    assert t.state("b") is None
    assert t.exists("a") and not t.exists("b")


def test_list_rejects_unreadable_json(fake):
    fake.stub("list", stdout="not json")
    with pytest.raises(AsbxError, match="unreadable JSON"):
        fake.tart().list()


def test_set_passes_only_the_given_resources(fake):
    fake.add_vm("a")
    t = fake.tart()
    t.set("a", cpu=4, memory_mb=8192, disk_gb=33)
    t.set("a", cpu=2)
    assert fake.calls == [["set", "a", "--cpu", "4", "--memory", "8192", "--disk-size", "33"],
                          ["set", "a", "--cpu", "2"]]


def test_ip_uses_the_dhcp_resolver_never_the_agent(fake):
    fake.stub("ip", stdout="192.168.64.5\n")
    assert fake.tart().ip("a", wait=10) == "192.168.64.5"
    assert fake.calls == [["ip", "a", "--resolver", "dhcp", "--wait", "10"]]
    assert "agent" not in fake.calls[0]


def test_ip_is_none_on_failure_or_empty_output(fake):
    fake.stub("ip", "a", rc=1, stderr="no IP")
    fake.stub("ip", "b", stdout="\n")
    assert fake.tart().ip("a") is None
    assert fake.tart().ip("b") is None


def test_exec_flags(fake, monkeypatch):
    monkeypatch.setattr(tart_mod.os, "isatty", lambda fd: True)
    t = fake.tart()
    t.exec("vm", ["true"])
    t.exec("vm", ["cat"], input=b"body")
    t.exec("vm", ["bash"], tty=True)
    assert fake.calls == [["exec", "vm", "true"],
                          ["exec", "-i", "vm", "cat"],
                          ["exec", "-t", "-i", "vm", "bash"]]
    assert fake.inputs[1] == b"body"


def test_tty_exec_without_a_terminal_requests_no_guest_pty(fake, monkeypatch):
    """`tart exec -t` aborts when stdout isn't a terminal (e.g. build-base
    output redirected to a log), so it must not be requested then."""
    monkeypatch.setattr(tart_mod.os, "isatty", lambda fd: False)
    fake.tart().exec("vm", ["bash"], tty=True)
    assert fake.calls == [["exec", "-i", "vm", "bash"]]


def test_clone_streams_its_output(fake):
    seen = {}

    def runner(argv, *, input=None, capture=True, tty=False, timeout=None):
        seen["capture"] = capture
        return fake(argv, input=input, capture=capture, tty=tty, timeout=timeout)

    Tart(runner=runner, executable=EXECUTABLE).clone("ghcr.io/cirruslabs/debian:trixie", "b")
    assert fake.calls == [["clone", "ghcr.io/cirruslabs/debian:trixie", "b"]]
    assert seen["capture"] is False


def test_stop_delete_rename_argv(fake):
    fake.add_vm("a", state="running")
    for name in ("b", "c"):
        fake.add_vm(name)
    t = fake.tart()
    t.stop("a")
    t.delete("a", "b")
    t.rename("c", "d")
    assert fake.calls == [["stop", "a"], ["delete", "a", "b"], ["rename", "c", "d"]]
    assert list(fake.vms) == ["d"]


def test_stop_of_a_stopped_vm_fails_unless_unchecked(fake):
    """Real `tart stop` fails on a stopped VM; so does the fake."""
    fake.add_vm("a")
    with pytest.raises(AsbxError, match='tart stop a\nVM "a" is not running'):
        fake.tart().stop("a")
    assert fake.tart().stop("a", check=False).returncode == 1


def test_version_is_empty_when_tart_fails(fake):
    fake.stub("--version", rc=1)
    assert fake.tart().version() == ""


def test_spawn_feeds_bytes_and_decodes_output():
    echo = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"]
    assert spawn(echo, input=b"caf\xc3\xa9").stdout == "café"
    assert spawn(echo, input="text").stdout == "text"


def test_spawn_reports_exit_code_and_stderr():
    proc = spawn([sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"])
    assert proc.returncode == 3
    assert proc.stderr == "boom"


def test_spawn_kills_a_run_past_its_timeout():
    with pytest.raises(subprocess.TimeoutExpired):
        spawn([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.2)
