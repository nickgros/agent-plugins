import json
import os
import plistlib
import shlex
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

from asbxlib import baseimage, config, hostsetup, launchd, tartbackend
from asbxlib import tart as tart_mod
from asbxlib.backend import ABSENT, META_EGRESS_MODE, META_GROUP, RUNNING, STOPPED, SandboxSpec
from asbxlib.errors import AsbxError
from asbxlib.manifest import Mount
from asbxlib.tartbackend import (AGENT_DROPIN_PATH, BASE_BUILD_VM, Share, TartBackend, TartGuest,
                                 disk_gb, job_plist_path, memory_mb, mount_tag, render_fstab,
                                 render_mount_script, render_push_script, shares, softnet_problem,
                                 softnet_rules, state_path, vm_log_path)
from fake_tart import EXECUTABLE, FakeLaunchctl, FakeTart

GUEST_SKILLS = "/home/agent/.agents/skills"
SOFTNET_DIR = config.DEFAULT_CONFIG["tart"]["softnet_dir"]


def _st(mode, uid=0):
    return os.stat_result((mode, 0, 0, 1, uid, 0, 0, 0, 0, 0))


DIR_OK = _st(stat.S_IFDIR | 0o755)
BIN_OK = _st(stat.S_IFREG | 0o4555)


def _stat_of(entries):
    def stat_fn(path):
        if path not in entries:
            raise FileNotFoundError(path)
        return entries[path]
    return stat_fn


def _safe_softnet(*dirs):
    """A stat_fn under which each of `dirs` holds a root-owned setuid softnet."""
    return _stat_of({p: st for d in dirs for p, st in ((d, DIR_OK), (f"{d}/softnet", BIN_OK))})


@pytest.fixture
def tcfg(cfg):
    cfg["backend"] = "tart"
    return cfg


@pytest.fixture
def env(config_root, monkeypatch):
    """A backend over FakeTart and FakeLaunchctl in a tmp config dir, with a
    safe softnet in the default dir and no real sleeps."""
    fake = FakeTart()
    launchctl = FakeLaunchctl(fake)
    monkeypatch.setattr(launchd, "run_launchctl", launchctl)
    monkeypatch.setattr(tartbackend.time, "sleep", lambda s: None)
    monkeypatch.setattr(tart_mod.os, "isatty", lambda fd: True)   # as when run from a terminal
    return SimpleNamespace(tart=fake, launchctl=launchctl,
                           backend=TartBackend(fake.tart(), stat_fn=_safe_softnet(SOFTNET_DIR)),
                           config_dir=config_root.parent)


@pytest.fixture
def share(tmp_path):
    d = tmp_path / "skills"
    d.mkdir()
    d.chmod(0o755)
    return d


def make_spec(mounts=(), instance="sandbox-x"):
    return SandboxSpec(instance=instance, group="x", cpu=4, memory="8GiB", disk="30GiB",
                       mounts=list(mounts))


def created(env, tcfg, spec):
    env.tart.add_vm(tcfg["image_alias"], disk=20)
    env.backend.create(tcfg, spec)


def bootstraps(env):
    return [i for i, e in enumerate(env.tart.events) if e[:2] == ["launchctl", "bootstrap"]]


# ---------------------------------------------------------------------------
# Softnet
# ---------------------------------------------------------------------------

def test_softnet_rules_block_the_mac_and_ipv4_cidrs_and_drop_ipv6():
    block, allow = softnet_rules(["10.0.0.0/8", "fc00::/7", "192.168.1.7/16", "fe80::/10"])
    assert block == "out @host,out 10.0.0.0/8,out 192.168.0.0/16"
    assert allow == "out proto udp @host port 53,in @host"


def test_softnet_rules_cover_every_default_ipv4_cidr(tcfg):
    block, _ = softnet_rules(tcfg["network"]["block_cidrs"])
    for cidr in tcfg["network"]["block_cidrs"]:
        assert (f"out {cidr}" in block.split(",")) == (":" not in cidr)


def test_softnet_rules_reject_a_bad_cidr():
    with pytest.raises(AsbxError, match="not a CIDR"):
        softnet_rules(["10.0.0.0/33"])


def test_softnet_guard_accepts_a_root_owned_setuid_binary_in_a_root_owned_dir():
    assert softnet_problem("/opt/sn", _stat_of({"/opt/sn": DIR_OK, "/opt/sn/softnet": BIN_OK})) is None


@pytest.mark.parametrize("entries, why", [
    ({"/opt/sn": _st(stat.S_IFDIR | 0o755, uid=501), "/opt/sn/softnet": BIN_OK}, "/opt/sn is not owned by root"),
    ({"/opt/sn": _st(stat.S_IFDIR | 0o775), "/opt/sn/softnet": BIN_OK}, "/opt/sn is writable by group or others"),
    ({"/opt/sn": _st(stat.S_IFREG | 0o755), "/opt/sn/softnet": BIN_OK}, "/opt/sn is not a directory"),
    ({}, "/opt/sn does not exist"),
    ({"/opt/sn": DIR_OK}, "/opt/sn/softnet does not exist"),
    ({"/opt/sn": DIR_OK, "/opt/sn/softnet": _st(stat.S_IFREG | 0o555)}, "/opt/sn/softnet is not setuid"),
    ({"/opt/sn": DIR_OK, "/opt/sn/softnet": _st(stat.S_IFREG | 0o4555, uid=501)},
     "/opt/sn/softnet is not owned by root"),
    ({"/opt/sn": DIR_OK, "/opt/sn/softnet": _st(stat.S_IFREG | 0o4557)},
     "/opt/sn/softnet is writable by group or others"),
    ({"/opt/sn": DIR_OK, "/opt/sn/softnet": _st(stat.S_IFDIR | 0o4555)}, "/opt/sn/softnet is not a regular file"),
])
def test_softnet_guard_rejects_and_prints_the_fix(entries, why):
    msg = softnet_problem("/opt/sn", _stat_of(entries))
    assert msg.startswith(why)
    assert "sudo install -d -o root -g wheel -m 0755 /opt/sn\n" in msg
    assert 'sudo install -o root -g wheel -m 4555 "$(brew --prefix softnet)/bin/softnet" /opt/sn/softnet' in msg
    assert "After 'brew install openai/tools/tart', which brings Softnet, install it with:\n" in msg


def test_softnet_guard_rejects_a_relative_dir():
    assert "is not an absolute path" in softnet_problem("libexec/asbx", _stat_of({}))


@pytest.mark.skipif(os.getuid() == 0, reason="needs a non-root owner")
def test_softnet_guard_on_real_files(tmp_path):
    d = tmp_path / "asbx"
    d.mkdir()
    d.chmod(0o755)
    binary = d / "softnet"
    binary.write_text("")
    binary.chmod(0o4555)
    assert "is not owned by root" in softnet_problem(str(d))

    def as_root(path):
        st = os.stat(path)
        return _st(st.st_mode, uid=0)

    assert softnet_problem(str(d), as_root) is None
    binary.chmod(0o555)
    assert "is not setuid" in softnet_problem(str(d), as_root)


# ---------------------------------------------------------------------------
# Sizes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("size, mb", [("8GiB", 8192), ("512MiB", 512), ("1.5GiB", 1536), ("1GB", 954)])
def test_memory_mb(size, mb):
    assert memory_mb(size) == mb


@pytest.mark.parametrize("size, gb", [("30GiB", 33), ("20GB", 20), ("1000MB", 1), ("1001MB", 2)])
def test_disk_gb_rounds_up_to_whole_decimal_gb(size, gb):
    assert disk_gb(size) == gb


def test_bad_size_raises():
    with pytest.raises(AsbxError, match="not a number with a unit"):
        memory_mb("8 gigs")


# ---------------------------------------------------------------------------
# TartGuest
# ---------------------------------------------------------------------------

def test_root_exec_is_a_bash_script_with_cd_and_env_prefixes():
    fake = FakeTart()
    g = TartGuest(fake.tart(), "vm")
    g.exec(["echo", "a b"], user="root")
    g.exec(["ls"], user="root", cwd="/work dir", env={"A": "x y"})
    assert fake.calls == [["exec", "vm", "bash", "-c", "echo 'a b'"],
                          ["exec", "vm", "bash", "-c", "cd '/work dir' && env A='x y' ls"]]


def test_non_root_exec_goes_through_runuser_and_defaults_to_home():
    fake = FakeTart()
    g = TartGuest(fake.tart(), "vm")
    g.exec(["git", "status"], user="agent")
    g.exec(["pwd"], user="agent", cwd="/workspace", env={"K": "v"})
    assert fake.calls == [
        ["exec", "vm", "runuser", "-u", "agent", "--", "bash", "-c", "cd /home/agent && git status"],
        ["exec", "vm", "runuser", "-u", "agent", "--", "bash", "-c", "cd /workspace && env K=v pwd"],
    ]


def test_tty_exec_allocates_a_pty_and_attaches_stdin(monkeypatch):
    monkeypatch.setattr(tart_mod.os, "isatty", lambda fd: True)
    fake = FakeTart()
    TartGuest(fake.tart(), "vm").exec(["bash", "-l"], user="agent", tty=True)
    assert fake.calls[0][:4] == ["exec", "-t", "-i", "vm"]


def test_sudo_mode_prefixes_every_command():
    fake = FakeTart()
    g = TartGuest(fake.tart(), "vm", root_via_sudo=True)
    g.exec(["true"], user="root")
    g.exec(["true"], user="agent")
    g.push_text("/root/x", "body")
    assert fake.calls[0] == ["exec", "vm", "sudo", "-n", "bash", "-c", "true"]
    assert fake.calls[1][2:7] == ["sudo", "-n", "runuser", "-u", "agent"]
    assert fake.calls[2][:7] == ["exec", "-i", "vm", "sudo", "-n", "bash", "-c"]


def test_exec_check():
    fake = FakeTart()
    fake.stub("exec", "false", rc=1, stderr="nope")
    g = TartGuest(fake.tart(), "vm")
    with pytest.raises(AsbxError, match="nope"):
        g.exec(["false"], user="root")
    assert g.exec(["false"], user="root", check=False).returncode == 1


def test_push_text_streams_into_a_private_temp_file_then_moves_it():
    fake = FakeTart()
    TartGuest(fake.tart(), "vm").push_text("/home/agent/.aws/credentials", "s3cret",
                                           mode="0600", uid=1000, gid=1000)
    argv = fake.calls[0]
    assert argv[:5] == ["exec", "-i", "vm", "bash", "-c"]
    assert fake.inputs[0] == b"s3cret"
    script = argv[5]
    assert "s3cret" not in script
    lines = script.splitlines()

    def at(fragment):
        return next(i for i, line in enumerate(lines) if fragment in line)

    assert at("umask 077") < at("mktemp /home/agent/.aws/.asbx-push.XXXXXX") < at('cat > "$tmp"')
    assert at('cat > "$tmp"') < at('chown 1000:1000 "$tmp"') < at('mv -f -- "$tmp" /home/agent/.aws/credentials')
    assert at('chmod 0600 "$tmp"') < at("mv -f")
    assert fake.pushed("/home/agent/.aws/credentials") == b"s3cret"


def test_push_file_sends_bytes_with_the_local_mode(tmp_path):
    local = tmp_path / "tool"
    local.write_bytes(b"\x7fELF\x00\xff")
    local.chmod(0o755)
    fake = FakeTart()
    TartGuest(fake.tart(), "vm").push_file(str(local), "/home/agent/.local/bin/tool", uid=1001, gid=1001)
    assert fake.pushed("/home/agent/.local/bin/tool") == b"\x7fELF\x00\xff"
    assert 'chmod 0755 "$tmp"' in fake.calls[0][-1]


def test_push_script_writes_the_target_with_its_mode_and_no_leftovers(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    target = out / "secret"
    target.write_text("old")
    script = render_push_script(str(target), "0640", os.getuid(), os.getgid())
    subprocess.run(["bash", "-c", script], input=b"s3cret", check=True)
    assert target.read_bytes() == b"s3cret"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert os.listdir(out) == ["secret"]


def test_push_script_without_mode_leaves_the_file_private(tmp_path):
    target = tmp_path / "f"
    subprocess.run(["bash", "-c", render_push_script(str(target), None, None, None)], input=b"x", check=True)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_uid_of_is_cached():
    fake = FakeTart()
    fake.stub("exec", "id -u agent", stdout="1001\n")
    g = TartGuest(fake.tart(), "vm")
    assert g.uid_of("agent") == 1001
    assert g.uid_of("agent") == 1001
    assert len(fake.calls) == 1


# ---------------------------------------------------------------------------
# Mounts
# ---------------------------------------------------------------------------

def test_mount_tag_names_one_host_dir_at_one_guest_path():
    tag = mount_tag("/h/a", "/g/a")
    assert tag == mount_tag("/h/a", "/g/a")
    assert tag != mount_tag("/h/b", "/g/a")
    assert tag != mount_tag("/h/a", "/g/b")
    assert tag.startswith("asbx-") and len(tag) <= 36   # virtiofs tag limit


def test_shares_validate_and_resolve(share, tmp_path):
    link = tmp_path / "link"
    link.symlink_to(share)
    [s] = shares([Mount(host=str(link), guest=GUEST_SKILLS)])
    assert s.host == str(share.resolve())
    assert s.tag == mount_tag(str(share.resolve()), GUEST_SKILLS)


def test_shares_refuse_missing_unreadable_and_colon_paths(tmp_path):
    with pytest.raises(AsbxError, match="does not exist"):
        shares([Mount(host=str(tmp_path / "gone"), guest="/g")])
    private = tmp_path / "private"
    private.mkdir()
    private.chmod(0o700)
    with pytest.raises(AsbxError, match="not readable"):
        shares([Mount(host=str(private), guest="/g")])
    colon = tmp_path / "a:b"
    colon.mkdir()
    colon.chmod(0o755)
    with pytest.raises(AsbxError, match="containing ':'"):
        shares([Mount(host=str(colon), guest="/g")])


def test_render_fstab_replaces_only_the_managed_block():
    s = Share(tag="asbx-aaa", host="/h", guest="/home/agent/my dir")
    current = ("UUID=1 / ext4 defaults 0 1\n"
               "# asbx mounts begin\nasbx-old /old virtiofs ro,nofail 0 0\n# asbx mounts end\n"
               "/dev/vdb /data ext4 defaults 0 2\n")
    out = render_fstab(current, [s])
    assert out == ("UUID=1 / ext4 defaults 0 1\n/dev/vdb /data ext4 defaults 0 2\n"
                   "# asbx mounts begin\nasbx-aaa /home/agent/my\\040dir virtiofs ro,nofail 0 0\n"
                   "# asbx mounts end\n")
    assert render_fstab(out, [s]) == out
    assert render_fstab(out, []) == "UUID=1 / ext4 defaults 0 1\n/dev/vdb /data ext4 defaults 0 2\n"


def _stub_bin(directory, name, rc):
    path = directory / name
    path.write_text(f"#!/bin/sh\nexit {rc}\n")
    path.chmod(0o755)


@pytest.mark.parametrize("mountpoint_rc, mount_rc, unmounted", [(1, 32, True), (0, 32, False), (1, 0, False)])
def test_mount_script_prints_only_the_paths_that_did_not_mount(tmp_path, mountpoint_rc, mount_rc, unmounted):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub_bin(bin_dir, "mountpoint", mountpoint_rc)
    _stub_bin(bin_dir, "mount", mount_rc)
    guest_path = str(tmp_path / "guest dir")
    script = render_mount_script([Share(tag="asbx-a", host="/h", guest=guest_path)])
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          env={"PATH": f"{bin_dir}:/usr/bin:/bin"})
    assert proc.stdout == (f"{guest_path}\n" if unmounted else "")
    assert os.path.isdir(guest_path)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def test_create_clones_resizes_and_records_softnet(env, tcfg):
    env.backend.meta_set("sandbox-x", META_GROUP, "stale")
    created(env, tcfg, make_spec())
    assert ["clone", tcfg["image_alias"], "sandbox-x"] in env.tart.calls
    assert ["set", "sandbox-x", "--cpu", "4", "--memory", "8192", "--disk-size", "33"] in env.tart.calls
    assert env.backend.state("sandbox-x") == STOPPED
    assert env.backend.meta_get("sandbox-x", META_EGRESS_MODE) == "softnet"
    assert env.backend.meta_get("sandbox-x", META_GROUP) == ""


def test_create_never_shrinks_a_larger_base_disk(env, tcfg):
    env.tart.add_vm(tcfg["image_alias"], disk=40)
    env.backend.create(tcfg, make_spec())
    assert ["set", "sandbox-x", "--cpu", "4", "--memory", "8192"] in env.tart.calls


def test_create_then_start_boots_only_through_a_softnet_job(env, tcfg, share):
    spec = make_spec([Mount(host=str(share), guest=GUEST_SKILLS)])
    created(env, tcfg, spec)
    env.backend.start(tcfg, spec)

    assert env.backend.state("sandbox-x") == RUNNING
    assert not any(call[0] == "run" for call in env.tart.calls)   # `tart run` only ever runs under launchd
    [body] = env.launchctl.bootstrapped
    job = plistlib.loads(body)
    block, allow = softnet_rules(tcfg["network"]["block_cidrs"])
    host_dir = str(share.resolve())
    tag = mount_tag(host_dir, GUEST_SKILLS)
    assert job["Label"] == "com.asbx.vm.sandbox-x"
    assert job["ProgramArguments"] == [
        EXECUTABLE, "run", "sandbox-x", "--no-graphics", "--no-clipboard", "--no-audio",
        f"--dir={host_dir}:ro,tag={tag}",
        f"--net-softnet-block={block}", f"--net-softnet-allow={allow}"]
    assert job["EnvironmentVariables"]["PATH"] == \
        f"{tcfg['tart']['softnet_dir']}:/opt/homebrew/bin:/usr/bin:/bin"
    assert job["KeepAlive"] == {"SuccessfulExit": False}
    assert job["StandardErrorPath"] == str(env.config_dir / "logs" / "sandbox-x.log")

    # Bootstrapped from the config dir, so it doesn't autostart at login.
    plist = env.config_dir / "launchd" / "com.asbx.vm.sandbox-x.plist"
    assert ["bootstrap", f"gui/{os.getuid()}", str(plist)] in env.launchctl.calls
    assert plist.read_bytes() == body

    fstab = env.tart.pushed("/etc/fstab").decode()
    assert f"{tag} {GUEST_SKILLS} virtiofs ro,nofail 0 0" in fstab
    assert env.tart.index("launchctl", "bootstrap") < env.tart.index("tart", "exec", "mountpoint -q")


def test_boot_guards_the_configured_softnet_dir(env, tcfg):
    created(env, tcfg, make_spec())
    tcfg["tart"]["softnet_dir"] = "/opt/elsewhere"
    with pytest.raises(AsbxError, match="/opt/elsewhere does not exist"):
        env.backend.start(tcfg, make_spec())
    assert not any(call[0] == "bootstrap" for call in env.launchctl.calls)
    assert env.backend.state("sandbox-x") == STOPPED

    env.backend = TartBackend(env.tart.tart(), stat_fn=_safe_softnet("/opt/elsewhere"))
    env.backend.start(tcfg, make_spec())
    [body] = env.launchctl.bootstrapped
    assert plistlib.loads(body)["EnvironmentVariables"]["PATH"].startswith("/opt/elsewhere:")


def test_start_refuses_a_vm_running_outside_asbx(env, tcfg):
    env.tart.add_vm("sandbox-x", state="running")
    with pytest.raises(AsbxError, match="running outside asbx"):
        env.backend.start(tcfg, make_spec())
    assert not any(call[0] == "bootstrap" for call in env.launchctl.calls)


def test_start_refuses_a_vm_run_by_hand_after_its_job_exited(env, tcfg):
    """When the guest powers itself off, `tart run` exits cleanly and the
    job stays loaded with no process; a `tart run` by hand then runs the VM
    without the job's egress rules."""
    created(env, tcfg, make_spec())
    env.backend.start(tcfg, make_spec())
    env.tart.set_state("sandbox-x", "stopped")   # the guest powers itself off
    env.tart.set_state("sandbox-x", "running")   # `tart run sandbox-x` by hand
    assert "com.asbx.vm.sandbox-x" in env.launchctl.loaded
    with pytest.raises(AsbxError, match="running outside asbx"):
        env.backend.start(tcfg, make_spec())
    assert len(env.launchctl.bootstrapped) == 1


def test_start_boots_again_a_vm_whose_guest_powered_itself_off(env, tcfg):
    created(env, tcfg, make_spec())
    env.backend.start(tcfg, make_spec())
    env.tart.set_state("sandbox-x", "stopped")   # the guest powers itself off; the job stays loaded
    assert env.backend.state("sandbox-x") == STOPPED
    env.backend.start(tcfg, make_spec())
    assert len(env.launchctl.bootstrapped) == 2
    assert env.backend.state("sandbox-x") == RUNNING
    assert launchd.job_running("com.asbx.vm.sandbox-x")


def test_start_absent_raises(env, tcfg):
    with pytest.raises(AsbxError, match="does not exist"):
        env.backend.start(tcfg, make_spec())


def test_start_when_running_only_ensures_mounts(env, tcfg, share):
    spec = make_spec([Mount(host=str(share), guest=GUEST_SKILLS)])
    created(env, tcfg, spec)
    env.backend.start(tcfg, spec)
    before = len(env.tart.calls)
    env.backend.start(tcfg, spec)
    assert len(env.launchctl.bootstrapped) == 1
    assert any("mountpoint -q" in call[-1] for call in env.tart.calls[before:])


def test_mount_failure_raises_after_boot_but_only_warns_when_already_running(env, tcfg, share, capsys):
    spec = make_spec([Mount(host=str(share), guest=GUEST_SKILLS)])
    created(env, tcfg, spec)
    env.tart.stub("exec", "mountpoint -q", stdout=f"{GUEST_SKILLS}\n")
    with pytest.raises(AsbxError, match=f"could not mount {GUEST_SKILLS}"):
        env.backend.start(tcfg, spec)
    env.backend.start(tcfg, spec)
    assert f"{GUEST_SKILLS} is not mounted" in capsys.readouterr().err


def test_start_times_out_when_the_agent_never_answers(env, tcfg, monkeypatch):
    created(env, tcfg, make_spec())
    monkeypatch.setattr(tartbackend, "READY_ATTEMPTS", 2)
    env.tart.stub("exec", "sandbox-x", "-c\ntrue", rc=1, stderr="agent unreachable")
    with pytest.raises(AsbxError, match="did not come up in time.*sandbox-x.log\nagent unreachable"):
        env.backend.start(tcfg, make_spec())
    assert "com.asbx.vm.sandbox-x" in env.launchctl.loaded   # the VM runs, so its job stays


def test_a_boot_that_never_comes_up_unloads_the_job_of_a_vm_that_is_not_running(env, tcfg, monkeypatch):
    """Otherwise launchd keeps relaunching `tart run` for a VM that can't boot."""
    created(env, tcfg, make_spec())
    monkeypatch.setattr(tartbackend, "READY_ATTEMPTS", 2)
    env.tart.stub("exec", "sandbox-x", "-c\ntrue", rc=1)
    env.tart.stub("list", stdout=json.dumps([{"Name": "sandbox-x", "State": "stopped"}]))
    with pytest.raises(AsbxError, match="did not come up in time"):
        env.backend.start(tcfg, make_spec())
    assert ["bootout", f"gui/{os.getuid()}/com.asbx.vm.sandbox-x"] in env.launchctl.calls
    assert not env.launchctl.loaded


def test_start_surfaces_cloud_init_errors(env, tcfg):
    created(env, tcfg, make_spec())
    env.tart.stub("exec", "cloud-init status", stdout="status: error\n")
    with pytest.raises(AsbxError, match="cloud-init failed in sandbox-x"):
        env.backend.start(tcfg, make_spec())


def test_apply_returns_true_only_when_a_running_vm_booted_another_job(env, tcfg, share):
    plain = make_spec()
    mounted = make_spec([Mount(host=str(share), guest=GUEST_SKILLS)])
    created(env, tcfg, plain)
    assert env.backend.apply(tcfg, mounted) is False      # stopped: stored for the next start
    stored = plistlib.loads(job_plist_path("sandbox-x").read_bytes())
    assert any(arg.startswith("--dir=") for arg in stored["ProgramArguments"])
    env.backend.start(tcfg, plain)
    assert env.backend.apply(tcfg, plain) is False        # unchanged
    assert env.backend.apply(tcfg, mounted) is True       # running VM keeps its old command line

    tcfg["network"]["block_cidrs"] = ["10.0.0.0/8"]
    assert env.backend.apply(tcfg, plain) is True         # egress rules changed


def test_apply_keeps_asking_for_a_restart_until_the_vm_boots_the_new_job(env, tcfg, share):
    """An `up` interrupted between storing the new job and restarting leaves
    the VM running the old one, with the new job already on disk."""
    plain = make_spec()
    mounted = make_spec([Mount(host=str(share), guest=GUEST_SKILLS)])
    created(env, tcfg, plain)
    env.backend.start(tcfg, plain)
    assert env.backend.apply(tcfg, mounted) is True       # stored; the restart never happens
    assert env.backend.apply(tcfg, mounted) is True       # the stored job matches, the running one doesn't
    env.backend.restart(tcfg, mounted)
    assert env.backend.apply(tcfg, mounted) is False


def test_stop_powers_the_guest_off_then_unloads_its_job(env, tcfg):
    """`tart stop` terminates a Linux guest at once and loses unwritten
    data, so the guest shuts itself down and `tart stop` is never needed."""
    created(env, tcfg, make_spec())
    env.backend.start(tcfg, make_spec())
    env.backend.stop("sandbox-x")
    assert env.tart.index("tart", "exec", "sandbox-x", "systemctl\npoweroff") < env.tart.index("launchctl", "bootout")
    assert not any(call[0] == "stop" for call in env.tart.calls)
    assert env.backend.state("sandbox-x") == STOPPED
    assert not env.launchctl.loaded


@pytest.fixture
def fake_clock(monkeypatch):
    """Every `time.monotonic()` is 10s after the last, so poweroff waits run out."""
    clock = iter(range(0, 10_000, 10))
    monkeypatch.setattr(tartbackend.time, "monotonic", lambda: next(clock))


def test_stop_forces_off_a_guest_that_ignores_poweroff(env, tcfg, fake_clock):
    created(env, tcfg, make_spec())
    env.backend.start(tcfg, make_spec())
    env.tart.ignore_poweroff = True
    env.backend.stop("sandbox-x")
    assert env.tart.index("tart", "exec", "sandbox-x", "systemctl\npoweroff") < env.tart.index("tart", "stop", "sandbox-x")
    assert env.backend.state("sandbox-x") == STOPPED


def test_stop_bounds_the_poweroff_request_to_an_agent_that_never_answers(env, tcfg, fake_clock):
    """The agent is guest-controlled: one that accepts the exec and never
    replies must not hang `stop`."""
    created(env, tcfg, make_spec())
    env.backend.start(tcfg, make_spec())
    env.tart.hang("exec", "sandbox-x", "systemctl\npoweroff")
    env.backend.stop("sandbox-x")
    assert ["stop", "sandbox-x"] in env.tart.calls
    assert env.backend.state("sandbox-x") == STOPPED
    assert not env.launchctl.loaded


def test_forced_stop_tolerates_a_guest_that_powers_off_just_before_it(env, tcfg, fake_clock):
    """Real `tart stop` fails on a VM that has just stopped; the stop still
    completes and unloads the job."""
    created(env, tcfg, make_spec())
    env.backend.start(tcfg, make_spec())
    env.tart.ignore_poweroff = True

    def runner(argv, **kwargs):
        if argv[1] == "stop":
            env.tart.set_state("sandbox-x", "stopped")   # the guest finishes powering off now
        return env.tart(argv, **kwargs)

    env.backend.tart = tart_mod.Tart(runner=runner, executable=EXECUTABLE)
    env.backend.stop("sandbox-x")
    assert ["stop", "sandbox-x"] in env.tart.calls
    assert not env.launchctl.loaded


def test_forced_stop_raises_when_the_vm_keeps_running(env, tcfg, fake_clock):
    created(env, tcfg, make_spec())
    env.backend.start(tcfg, make_spec())
    env.tart.ignore_poweroff = True
    env.tart.stub("stop", "sandbox-x", rc=1, stderr="lock busy")
    with pytest.raises(AsbxError, match="tart stop sandbox-x\nlock busy"):
        env.backend.stop("sandbox-x")
    assert "com.asbx.vm.sandbox-x" in env.launchctl.loaded


def test_restart_boots_from_a_fresh_job(env, tcfg):
    created(env, tcfg, make_spec())
    env.backend.start(tcfg, make_spec())
    env.backend.restart(tcfg, make_spec())
    assert len(env.launchctl.bootstrapped) == 2
    assert env.backend.state("sandbox-x") == RUNNING


def test_delete_removes_vm_snapshots_and_host_files_but_not_look_alikes(env, tcfg):
    created(env, tcfg, make_spec())
    env.backend.start(tcfg, make_spec())
    env.backend.snapshot(tcfg, make_spec(), "a")
    env.tart.add_vm("sandbox-x-snap-other")   # another sandbox's VM, not a snapshot
    vm_log_path("sandbox-x").write_text("log")

    env.backend.delete("sandbox-x")

    assert ["delete", "sandbox-x", "sandbox-x-snap-a"] in env.tart.calls
    assert set(env.tart.vms) == {tcfg["image_alias"], "sandbox-x-snap-other"}
    for path in (job_plist_path("sandbox-x"), vm_log_path("sandbox-x"), state_path("sandbox-x"),
                 state_path("sandbox-x-snap-a")):
        assert not path.exists()
    assert not env.launchctl.loaded


def test_delete_absent_is_not_an_error(env):
    env.backend.delete("sandbox-gone")
    assert not any(call[0] == "delete" for call in env.tart.calls)


def test_delete_never_waits_on_the_guest(env, tcfg):
    """The disk is about to go, so `rm` forces the VM off: a guest that
    stalls or ignores a poweroff can't hold it up."""
    created(env, tcfg, make_spec())
    env.backend.start(tcfg, make_spec())
    env.tart.hang("exec")
    before = len(env.tart.calls)
    env.backend.delete("sandbox-x")
    assert not any(call[0] == "exec" for call in env.tart.calls[before:])
    events = env.tart.events
    assert (events.index(["tart", "stop", "sandbox-x"])
            < events.index(["launchctl", "bootout", f"gui/{os.getuid()}/com.asbx.vm.sandbox-x"])
            < events.index(["tart", "delete", "sandbox-x"]))
    assert "sandbox-x" not in env.tart.vms
    assert not env.launchctl.loaded


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------

def test_snapshot_of_a_running_vm_stops_clones_and_starts_it_again(env, tcfg):
    created(env, tcfg, make_spec())
    env.backend.start(tcfg, make_spec())
    env.backend.meta_set("sandbox-x", META_GROUP, "x")
    env.backend.snapshot(tcfg, make_spec(), "a")

    stop = env.tart.index("tart", "exec", "sandbox-x", "systemctl\npoweroff")
    clone = env.tart.index("tart", "clone", "sandbox-x", "sandbox-x-snap-a")
    assert stop < clone < bootstraps(env)[1]
    assert env.backend.state("sandbox-x") == RUNNING
    assert env.backend.snapshots("sandbox-x") == ["a"]
    assert env.backend.meta_get("sandbox-x-snap-a", META_GROUP) == "x"


def test_snapshot_of_a_stopped_vm_does_not_boot_it(env, tcfg):
    created(env, tcfg, make_spec())
    env.backend.snapshot(tcfg, make_spec(), "a")
    assert not env.launchctl.bootstrapped
    assert env.backend.state("sandbox-x") == STOPPED


def test_snapshot_refuses_a_duplicate_label(env, tcfg):
    created(env, tcfg, make_spec())
    env.backend.snapshot(tcfg, make_spec(), "a")
    with pytest.raises(AsbxError, match="already exists"):
        env.backend.snapshot(tcfg, make_spec(), "a")


def test_snapshot_starts_the_vm_again_when_the_clone_fails(env, tcfg):
    created(env, tcfg, make_spec())
    env.backend.start(tcfg, make_spec())
    env.tart.stub("clone", "sandbox-x", "sandbox-x-snap-a", rc=1)
    with pytest.raises(AsbxError, match="command failed: tart clone sandbox-x sandbox-x-snap-a"):
        env.backend.snapshot(tcfg, make_spec(), "a")
    assert len(env.launchctl.bootstrapped) == 2
    assert env.backend.state("sandbox-x") == RUNNING
    assert env.backend.snapshots("sandbox-x") == []


def test_snapshot_marks_its_clone_before_making_it(env, tcfg, monkeypatch):
    """A clone without its `snapshot_of` mark would be hidden from
    `snapshots` and never deleted with the sandbox."""
    created(env, tcfg, make_spec())

    def fail(vm, data):
        raise OSError("disk full")

    monkeypatch.setattr(tartbackend, "_write_state", fail)
    with pytest.raises(OSError, match="disk full"):
        env.backend.snapshot(tcfg, make_spec(), "a")
    assert "sandbox-x-snap-a" not in env.tart.vms


def test_snapshots_ignore_unmarked_look_alikes(env, tcfg):
    created(env, tcfg, make_spec())
    env.tart.add_vm("sandbox-x-snap-y")
    assert env.backend.snapshots("sandbox-x") == []


def test_restore_refuses_an_unknown_label_without_touching_the_sandbox(env, tcfg):
    created(env, tcfg, make_spec())
    env.backend.start(tcfg, make_spec())
    tart_before, launchctl_before = len(env.tart.calls), len(env.launchctl.calls)
    with pytest.raises(AsbxError, match="no snapshot named 'nope'"):
        env.backend.restore(tcfg, make_spec(), "nope")
    assert {call[0] for call in env.tart.calls[tart_before:]} <= {"list"}
    assert {call[0] for call in env.launchctl.calls[launchctl_before:]} <= {"print"}
    assert env.backend.state("sandbox-x") == RUNNING


def test_restore_replaces_the_vm_with_the_snapshot_and_boots_it(env, tcfg):
    created(env, tcfg, make_spec())
    env.backend.start(tcfg, make_spec())
    env.backend.meta_set("sandbox-x", META_GROUP, "then")
    env.backend.snapshot(tcfg, make_spec(), "a")
    env.backend.meta_set("sandbox-x", META_GROUP, "now")

    env.backend.restore(tcfg, make_spec(), "a")

    events = env.tart.events
    clone = events.index(["tart", "clone", "sandbox-x-snap-a", "sandbox-x-restoring"])
    stop = events.index(["tart", "stop", "sandbox-x"])
    delete = events.index(["tart", "delete", "sandbox-x"])
    rename = events.index(["tart", "rename", "sandbox-x-restoring", "sandbox-x"])
    assert clone < stop < delete < rename < bootstraps(env)[-1]
    # Its state is being discarded, so the sandbox is forced off without asking the guest.
    assert not any("poweroff" in event for event in events[clone:rename])
    assert env.backend.state("sandbox-x") == RUNNING
    assert env.backend.meta_get("sandbox-x", META_GROUP) == "then"
    assert env.backend.meta_get("sandbox-x", "snapshot_of") == ""
    assert env.backend.snapshots("sandbox-x") == ["a"]


def test_restore_leaves_the_sandbox_alone_when_the_clone_fails(env, tcfg):
    created(env, tcfg, make_spec())
    env.backend.snapshot(tcfg, make_spec(), "a")
    env.backend.start(tcfg, make_spec())
    env.tart.stub("clone", "sandbox-x-snap-a", "sandbox-x-restoring", rc=1)
    with pytest.raises(AsbxError, match="tart clone sandbox-x-snap-a sandbox-x-restoring"):
        env.backend.restore(tcfg, make_spec(), "a")
    assert env.backend.state("sandbox-x") == RUNNING
    assert "com.asbx.vm.sandbox-x" in env.launchctl.loaded
    assert not any(call[0] in ("stop", "delete") for call in env.tart.calls)


def test_restore_refuses_when_its_staging_name_is_taken(env, tcfg):
    """`<instance>-restoring` may be another sandbox, so it is never deleted."""
    created(env, tcfg, make_spec())
    env.backend.snapshot(tcfg, make_spec(), "a")
    env.tart.add_vm("sandbox-x-restoring")
    before = len(env.tart.calls)
    with pytest.raises(AsbxError, match="tart delete sandbox-x-restoring"):
        env.backend.restore(tcfg, make_spec(), "a")
    assert {call[0] for call in env.tart.calls[before:]} == {"list"}
    assert {"sandbox-x", "sandbox-x-restoring"} <= set(env.tart.vms)


# ---------------------------------------------------------------------------
# Base image
# ---------------------------------------------------------------------------

def test_build_base_hardens_and_verifies_root_before_preparing(env, tcfg):
    env.tart.stub("exec", "id -u", stdout="0\n")
    prepared = []

    def prepare(g):
        prepared.append(g.instance)
        g.exec(["echo", "prepare"], user="root")

    built = env.backend.build_base(tcfg, prepare)

    b = BASE_BUILD_VM
    alias = tcfg["image_alias"]
    clone = env.tart.index("tart", "clone", tcfg["tart"]["base_image_source"], b)
    dropin = env.tart.index("tart", "exec", "-i", b, "sudo\n-n", f'"$tmp" {AGENT_DROPIN_PATH}')
    stop = env.tart.index("tart", "exec", b, "systemctl\npoweroff")
    first_boot, reboot = bootstraps(env)[:2]
    id_check = env.tart.index("tart", "exec", b, "bash\n-c\nid -u")
    userdel = env.tart.index("tart", "exec", b, "bash\n-c\nuserdel -r admin")
    prep = env.tart.index("tart", "exec", b, "bash\n-c\necho prepare")
    assert clone < first_boot < dropin < stop < reboot < id_check < userdel < prep
    # From the reboot on, commands run without sudo: the agent itself has to be root.
    for i in (id_check, userdel, prep):
        assert "sudo" not in env.tart.events[i]
    assert prepared == [b]
    assert env.tart.pushed(AGENT_DROPIN_PATH) == b"[Service]\nUser=root\nGroup=root\n"
    # ssh password logins are the base recipe's to turn off, not the backend's.
    assert not any("PasswordAuthentication" in arg for call in env.tart.calls for arg in call)

    # Every build boot carries the Softnet rules.
    for body in env.launchctl.bootstrapped:
        assert any(arg.startswith("--net-softnet-block=out @host") for arg in plistlib.loads(body)["ProgramArguments"])

    assert ["rename", b, alias] in env.tart.calls
    assert set(env.tart.vms) == {alias}
    assert env.tart.vms[alias]["State"] == "stopped"
    assert not env.launchctl.loaded
    assert not job_plist_path(b).exists()
    assert env.backend.base_image(tcfg) == built


def test_build_base_runs_the_base_recipe_through_the_root_agent(env, tcfg):
    env.tart.stub("exec", "id -u", stdout="0\n")
    env.backend.build_base(tcfg, lambda g: baseimage.prepare_build_vm(g, tcfg, "echo recipe\n"))

    b = BASE_BUILD_VM
    recipe = env.tart.index("tart", "exec", "-t", "-i", b, "bash\n-c\nbash /root/asbx-base.sh")
    assert env.tart.index("tart", "exec", b, "bash\n-c\nuserdel -r admin") < recipe
    assert "sudo" not in env.tart.events[recipe]
    assert env.tart.pushed(baseimage.BASE_RECIPE_PATH) == b"echo recipe\n"
    assert env.tart.index("tart", "exec", b, "cloud-init clean --logs --machine-id") > recipe


def test_build_base_replaces_the_previous_image(env, tcfg):
    env.tart.add_vm(tcfg["image_alias"])
    env.tart.stub("exec", "id -u", stdout="0\n")
    env.backend.build_base(tcfg, lambda g: None)
    assert env.tart.index("tart", "delete", tcfg["image_alias"]) < env.tart.index("tart", "rename")


def test_build_base_refuses_when_root_verification_fails(env, tcfg):
    env.tart.stub("exec", "id -u", stdout="1000\n")
    with pytest.raises(AsbxError, match="uid 1000, not root.*tart delete asbx-base-build"):
        env.backend.build_base(tcfg, lambda g: pytest.fail("prepared before root was verified"))
    assert not any("userdel" in call[-1] for call in env.tart.calls)
    assert env.tart.vms[BASE_BUILD_VM]["State"] == "stopped"
    assert tcfg["image_alias"] not in env.tart.vms
    assert not env.launchctl.loaded


def test_build_base_recipe_failure_stops_the_build_vm(env, tcfg):
    env.tart.stub("exec", "id -u", stdout="0\n")
    env.tart.stub("exec", "bash /root/asbx-base.sh", rc=2)
    with pytest.raises(AsbxError, match="base recipe failed in asbx-base-build.*exit 2.*tart delete asbx-base-build"):
        env.backend.build_base(tcfg, lambda g: baseimage.prepare_build_vm(g, tcfg, "false\n"))
    assert env.tart.vms[BASE_BUILD_VM]["State"] == "stopped"
    assert tcfg["image_alias"] not in env.tart.vms
    assert not env.launchctl.loaded


def test_build_base_refuses_a_leftover_build_vm(env, tcfg):
    env.tart.add_vm(BASE_BUILD_VM)
    with pytest.raises(AsbxError, match="tart delete asbx-base-build"):
        env.backend.build_base(tcfg, lambda g: None)
    assert not any(call[0] == "clone" for call in env.tart.calls)


def test_build_base_refuses_to_publish_a_build_vm_that_had_to_be_forced_off(env, tcfg, fake_clock):
    """Forced off, the guest may not have written back the build's last
    changes, such as the final cleanup."""
    env.tart.stub("exec", "id -u", stdout="0\n")

    def prepare(g):
        env.tart.ignore_poweroff = True

    with pytest.raises(AsbxError, match="did not power off by itself.*tart delete asbx-base-build"):
        env.backend.build_base(tcfg, prepare)
    assert ["stop", BASE_BUILD_VM] in env.tart.calls
    assert not any(call[0] == "rename" for call in env.tart.calls)
    assert tcfg["image_alias"] not in env.tart.vms
    assert env.tart.vms[BASE_BUILD_VM]["State"] == "stopped"
    assert not env.launchctl.loaded


def test_build_base_resize_failure_names_the_cleanup(env, tcfg):
    env.tart.stub("set", BASE_BUILD_VM, rc=1, stderr="bad size")
    with pytest.raises(AsbxError, match="bad size; delete the VM before retrying: tart delete asbx-base-build"):
        env.backend.build_base(tcfg, lambda g: pytest.fail("prepared after a failed resize"))
    assert not env.launchctl.bootstrapped


def test_build_base_refuses_an_unsafe_softnet_before_cloning(env, tcfg):
    env.backend = TartBackend(env.tart.tart(), stat_fn=_stat_of({}))
    with pytest.raises(AsbxError, match=f"{SOFTNET_DIR} does not exist"):
        env.backend.build_base(tcfg, lambda g: None)
    assert not any(call[0] == "clone" for call in env.tart.calls)


def test_base_image_needs_the_vm_and_its_build_record(env, tcfg):
    assert env.backend.base_image(tcfg) is None
    env.tart.add_vm(tcfg["image_alias"])
    assert env.backend.base_image(tcfg) is None           # a VM asbx didn't build
    env.tart.stub("exec", "id -u", stdout="0\n")
    built = env.backend.build_base(tcfg, lambda g: None)
    assert built and env.backend.base_image(tcfg) == built


# ---------------------------------------------------------------------------
# Addressing, metadata, host checks
# ---------------------------------------------------------------------------

def test_ssh_route_proxies_through_asbx_with_its_own_path_and_config_dir(tcfg, monkeypatch):
    """ssh runs the ProxyCommand with the caller's env, which may lack tart
    on PATH or the config dir this asbx uses."""
    monkeypatch.setenv(config.CONFIG_DIR_ENV, "/my cfg")
    route = TartBackend(FakeTart().tart()).ssh_route(tcfg, "sandbox-x")
    assert route.host_key_alias == "sandbox-x"
    assert route.hostname is None
    assert shlex.split(route.proxy_command) == [
        "/usr/bin/env", "PATH=/opt/homebrew/bin:/usr/bin:/bin", "ASBX_CONFIG_DIR=/my cfg",
        sys.executable, str(hostsetup.ENTRY_POINT), "ssh-proxy", "sandbox-x"]


def test_ssh_route_sets_no_config_dir_when_asbx_runs_without_one(tcfg, monkeypatch):
    monkeypatch.delenv(config.CONFIG_DIR_ENV, raising=False)
    route = TartBackend(FakeTart().tart()).ssh_route(tcfg, "sandbox-x")
    assert shlex.split(route.proxy_command) == [
        "/usr/bin/env", "PATH=/opt/homebrew/bin:/usr/bin:/bin",
        sys.executable, str(hostsetup.ENTRY_POINT), "ssh-proxy", "sandbox-x"]


def test_ssh_address_comes_from_the_dhcp_resolver(tcfg):
    fake = FakeTart()
    fake.stub("ip", "good", stdout="192.168.64.3\n")
    fake.stub("ip", "bad", stdout="not-an-ip\n")
    fake.stub("ip", "down", rc=1)
    backend = TartBackend(fake.tart())
    assert backend.ssh_address(tcfg, "good") == "192.168.64.3"
    assert backend.ssh_address(tcfg, "bad") is None
    assert backend.ssh_address(tcfg, "down") is None
    assert all(call[2:4] == ["--resolver", "dhcp"] for call in fake.calls)
    assert backend.pinned_ipv4("good") is None


def test_state_maps_tart_states(env):
    env.tart.add_vm("a", state="running")
    env.tart.add_vm("b", state="suspended")
    assert [env.backend.state(n) for n in ("a", "b", "c")] == [RUNNING, STOPPED, ABSENT]


def test_meta_round_trip_in_the_state_file(env):
    assert env.backend.meta_get("sandbox-x", META_GROUP) == ""
    env.backend.meta_set("sandbox-x", META_GROUP, "x")
    env.backend.meta_set("sandbox-x", META_EGRESS_MODE, "softnet")
    assert env.backend.meta_get("sandbox-x", META_GROUP) == "x"
    assert (env.config_dir / "state" / "sandbox-x.json").exists()


@pytest.fixture
def mac(monkeypatch):
    monkeypatch.setattr(tartbackend.host, "which", lambda name: True)
    monkeypatch.setattr(tartbackend.platform, "mac_ver", lambda: ("26.1", ("", "", ""), "arm64"))
    monkeypatch.setattr(tartbackend.platform, "machine", lambda: "arm64")


def test_preflight_passes_on_a_ready_mac(env, tcfg, mac):
    env.backend.preflight(tcfg)


def test_preflight_refuses_old_macos(env, tcfg, mac, monkeypatch):
    monkeypatch.setattr(tartbackend.platform, "mac_ver", lambda: ("15.6", ("", "", ""), "arm64"))
    with pytest.raises(AsbxError, match="macOS 26 or later"):
        env.backend.preflight(tcfg)


def test_preflight_refuses_missing_tart(env, tcfg, mac, monkeypatch):
    monkeypatch.setattr(tart_mod.shutil, "which", lambda name: None)
    backend = TartBackend(tart_mod.Tart(runner=env.tart), stat_fn=_safe_softnet(SOFTNET_DIR))
    with pytest.raises(AsbxError, match="tart not found on PATH; install it"):
        backend.preflight(tcfg)


def test_preflight_refuses_an_unsafe_softnet(env, tcfg, mac):
    backend = TartBackend(env.tart.tart(), stat_fn=_stat_of({}))
    with pytest.raises(AsbxError, match=f"{SOFTNET_DIR} does not exist"):
        backend.preflight(tcfg)


def test_doctor_checks_without_config_cover_the_host(env, mac):
    env.tart.stub("--version", stdout="2.42.0\n")
    rows = env.backend.doctor_checks(None, fix=False)
    assert rows == [("PASS", "tart 2.42.0 on PATH"),
                    ("PASS", "macOS 26.1 (Softnet needs 26 or later)"),
                    ("PASS", "Apple silicon")]


def test_doctor_checks_report_softnet_and_base_image(env, tcfg, mac):
    softnet_row = f"softnet in {SOFTNET_DIR} is root-owned and setuid"
    rows = dict((msg.split(":")[0], status) for status, msg in env.backend.doctor_checks(tcfg, fix=False))
    assert rows[softnet_row] == "PASS"
    assert rows[f"base image {tcfg['image_alias']} exists"] == "FAIL"

    not_setuid = _stat_of({SOFTNET_DIR: DIR_OK, f"{SOFTNET_DIR}/softnet": _st(stat.S_IFREG | 0o555)})
    rows = TartBackend(env.tart.tart(), stat_fn=not_setuid).doctor_checks(tcfg, fix=False)
    assert next(r for r in rows if r[1].startswith(softnet_row))[0] == "FAIL"
    assert any("is not setuid" in msg for _status, msg in rows)
