import os
import plistlib
import stat
import subprocess

import pytest

from asbxlib import launchd
from asbxlib.errors import AsbxError
from fake_tart import FakeLaunchctl


@pytest.fixture
def launchctl(monkeypatch):
    fake = FakeLaunchctl()
    monkeypatch.setattr(launchd, "run_launchctl", fake)
    monkeypatch.setattr(launchd.time, "sleep", lambda s: None)
    return fake


def test_render_plist_minimal_job_runs_at_load_only():
    job = plistlib.loads(launchd.render_plist("com.asbx.x", ["/bin/echo", "hi"]))
    assert job == {"Label": "com.asbx.x", "ProgramArguments": ["/bin/echo", "hi"], "RunAtLoad": True}


def test_render_plist_all_options():
    job = plistlib.loads(launchd.render_plist(
        "com.asbx.x", ["/bin/sh", "-c", "true"], env={"PATH": "/a:/b"}, start_interval=600,
        watch_paths=["/w"], keep_alive_on_failure=True, log_path="/tmp/x.log"))
    assert job["EnvironmentVariables"] == {"PATH": "/a:/b"}
    assert job["StartInterval"] == 600
    assert job["WatchPaths"] == ["/w"]
    # Restart only after a failure, so a clean stop stays stopped.
    assert job["KeepAlive"] == {"SuccessfulExit": False}
    assert job["StandardOutPath"] == job["StandardErrorPath"] == "/tmp/x.log"


def test_render_plist_is_deterministic():
    a = launchd.render_plist("l", ["p"], env={"B": "2", "A": "1"})
    b = launchd.render_plist("l", ["p"], env={"A": "1", "B": "2"})
    assert a == b


def test_write_plist_replaces_atomically_with_mode_0644(tmp_path):
    path = tmp_path / "jobs" / "x.plist"
    launchd.write_plist(path, b"one")
    launchd.write_plist(path, b"two")
    assert path.read_bytes() == b"two"
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert [p.name for p in path.parent.iterdir()] == ["x.plist"]


def test_agent_loaded_prints_the_gui_domain_label(launchctl):
    assert not launchd.agent_loaded("com.asbx.x")
    assert launchctl.calls == [["print", f"gui/{os.getuid()}/com.asbx.x"]]
    launchctl.loaded["com.asbx.x"] = b""
    assert launchd.agent_loaded("com.asbx.x")


# Trimmed from real `launchctl print gui/<uid>/<label>` output.
PRINT_RUNNING = """gui/501/com.asbx.vm.x = {
\tactive count = 1
\tpath = /Users/me/.config/agent-sandbox/launchd/com.asbx.vm.x.plist
\tstate = running

\tprogram = /opt/homebrew/bin/tart
\targuments = {
\t\t/opt/homebrew/bin/tart
\t\trun
\t\tx
\t}

\truns = 1
\tpid = 4242
\timmediate reason = speculative
\tlast exit code = (never exited)
}
"""
PRINT_EXITED = """gui/501/com.asbx.vm.x = {
\tactive count = 0
\tpath = /Users/me/.config/agent-sandbox/launchd/com.asbx.vm.x.plist
\tstate = not running

\tprogram = /opt/homebrew/bin/tart
\truns = 1
\tlast exit code = 0
}
"""


@pytest.mark.parametrize("rc, stdout, running", [(0, PRINT_RUNNING, True), (0, PRINT_EXITED, False),
                                                 (113, "", False)])
def test_job_running_needs_a_loaded_job_with_a_pid(monkeypatch, rc, stdout, running):
    """A job stays loaded after its program exits cleanly, as when a guest
    powers itself off, so being loaded isn't enough."""
    calls = []

    def run(args):
        calls.append(args)
        return subprocess.CompletedProcess(args, rc, stdout, "")

    monkeypatch.setattr(launchd, "run_launchctl", run)
    assert launchd.job_running("com.asbx.vm.x") is running
    assert calls == [["print", f"gui/{os.getuid()}/com.asbx.vm.x"]]


def test_install_agent_writes_launch_agents_and_bootstraps(launchctl, tmp_path, monkeypatch):
    monkeypatch.setattr(launchd.Path, "home", classmethod(lambda cls: tmp_path))
    body = launchd.render_plist("com.asbx.x", ["/bin/true"])
    path = launchd.install_agent("com.asbx.x", body)
    assert path == tmp_path / "Library" / "LaunchAgents" / "com.asbx.x.plist"
    assert path.read_bytes() == body
    assert ["bootstrap", f"gui/{os.getuid()}", str(path)] in launchctl.calls
    assert not any(c[0] == "bootout" for c in launchctl.calls)
    assert launchctl.loaded["com.asbx.x"] == body


def test_install_agent_boots_out_a_loaded_job_first(launchctl, tmp_path, monkeypatch):
    monkeypatch.setattr(launchd.Path, "home", classmethod(lambda cls: tmp_path))
    launchctl.loaded["com.asbx.x"] = b"old"
    body = launchd.render_plist("com.asbx.x", ["/bin/true"])
    launchd.install_agent("com.asbx.x", body)
    verbs = [c[0] for c in launchctl.calls]
    assert verbs.index("bootout") < verbs.index("bootstrap")
    assert launchctl.loaded["com.asbx.x"] == body


def test_bootout_of_absent_job_is_not_an_error(launchctl):
    launchd.bootout("com.asbx.gone")
    assert [c[0] for c in launchctl.calls] == ["print"]


def test_bootout_raises_when_the_job_stays_loaded(monkeypatch):
    def stuck(args):
        return subprocess.CompletedProcess(args, 0 if args[0] == "print" else 1, "", "denied")
    monkeypatch.setattr(launchd, "run_launchctl", stuck)
    monkeypatch.setattr(launchd, "BOOTOUT_POLLS", 2)
    monkeypatch.setattr(launchd.time, "sleep", lambda s: None)
    with pytest.raises(AsbxError, match="denied"):
        launchd.bootout("com.asbx.x")


def test_bootstrap_failure_raises_with_stderr(launchctl, tmp_path):
    path = tmp_path / "x.plist"
    path.write_bytes(launchd.render_plist("com.asbx.x", ["/bin/true"]))
    launchctl.bootstrap_rc = 5
    with pytest.raises(AsbxError, match="Input/output error"):
        launchd.bootstrap(path)


def test_reload_bootstraps_from_an_arbitrary_path(launchctl, tmp_path):
    path = tmp_path / "anywhere" / "com.asbx.vm.x.plist"
    body = launchd.render_plist("com.asbx.vm.x", ["/bin/true"])
    launchd.write_plist(path, body)
    launchd.reload("com.asbx.vm.x", path)
    assert launchctl.loaded["com.asbx.vm.x"] == body
