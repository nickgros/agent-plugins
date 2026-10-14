"""Pure-logic tests for asbxlib.guest. No Incus required."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

import pytest

from asbxlib import guest
from asbxlib.errors import AsbxError
from asbxlib.manifest import Mount
from fake_incus import FakeIncus


# ---------------------------------------------------------------------------
# mount_readable_error — the guest uid lives in its own namespace, so world
# read+execute is the only bar that means anything.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", [0o700, 0o750, 0o704, 0o701])
def test_mount_readable_error_rejects_modes_without_world_read_and_execute(tmp_path, mode):
    target = tmp_path / "mount"
    target.mkdir()
    os.chmod(target, mode)
    msg = guest.mount_readable_error(target)
    assert msg is not None
    assert "chmod o+rX" in msg


@pytest.mark.parametrize("mode", [0o755, 0o705, 0o777])
def test_mount_readable_error_passes_world_readable_modes(tmp_path, mode):
    target = tmp_path / "mount"
    target.mkdir()
    os.chmod(target, mode)
    assert guest.mount_readable_error(target) is None


def test_mount_readable_error_reports_a_missing_path(tmp_path):
    msg = guest.mount_readable_error(tmp_path / "absent")
    assert msg is not None
    assert "absent" in msg


def test_mount_readable_error_ignores_unreadable_ancestors(tmp_path):
    """virtiofs is served by the Incus daemon as host root, so a private
    parent directory never blocks the guest."""
    parent = tmp_path / "private"
    parent.mkdir()
    target = parent / "mount"
    target.mkdir()
    os.chmod(target, 0o755)
    os.chmod(parent, 0o700)
    try:
        assert guest.mount_readable_error(target) is None
    finally:
        os.chmod(parent, 0o755)


# ---------------------------------------------------------------------------
# render_instance_cloud_init
# ---------------------------------------------------------------------------

def test_render_instance_cloud_init_has_pubkey_for_guest_user():
    rendered = guest.render_instance_cloud_init("sandbox-demo", "dev", "ssh-ed25519 AAAAtest")
    assert "hostname: sandbox-demo" in rendered
    assert "- name: dev" in rendered
    assert "ssh-ed25519 AAAAtest" in rendered


# ---------------------------------------------------------------------------
# render_env_file
# ---------------------------------------------------------------------------

def test_render_env_file_escapes_single_quote_round_trips_in_bash():
    rendered = guest.render_env_file({"FOO": "it's a test"})
    proc = subprocess.run(
        ["bash", "-c", f"{rendered}\necho \"$FOO\""],
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip() == "it's a test"


def test_render_env_file_empty_env_renders_nothing():
    assert guest.render_env_file({}) == ""


# ---------------------------------------------------------------------------
# sync_mounts
# ---------------------------------------------------------------------------

def test_sync_mounts_no_change_when_devices_already_match(tmp_path):
    os.chmod(str(tmp_path), 0o755)
    mounts = [Mount(host=str(tmp_path), guest="/home/agent/x", mode="ro")]
    fake = FakeIncus()
    fake.set_devices({
        "mnt-0": {
            "type": "disk",
            "source": str(tmp_path),
            "path": "/home/agent/x",
            "readonly": "true",
        }
    })
    assert guest.sync_mounts(fake, "sandbox-demo", mounts) is False
    assert not any(c[:3] == ["config", "device", "add"] for c in fake.calls)


def test_sync_mounts_returns_true_and_readds_when_source_path_differs(tmp_path):
    os.chmod(str(tmp_path), 0o755)
    mounts = [Mount(host=str(tmp_path), guest="/home/agent/x", mode="ro")]
    fake = FakeIncus()
    fake.set_devices({
        "mnt-0": {
            "type": "disk",
            "source": "/somewhere/else",
            "path": "/home/agent/x",
            "readonly": "true",
        }
    })
    assert guest.sync_mounts(fake, "sandbox-demo", mounts) is True
    assert any(
        c[:6] == ["config", "device", "add", "sandbox-demo", "mnt-0", "disk"]
        for c in fake.calls
    )


def test_sync_mounts_raises_asbxerror_naming_the_mount_when_incus_refuses_the_device(tmp_path):
    os.chmod(str(tmp_path), 0o755)
    mounts = [Mount(host=str(tmp_path), guest="/home/agent/x", mode="ro")]
    fake = FakeIncus()
    fake.set_devices({
        "mnt-0": {
            "type": "disk",
            "source": "/somewhere/else",
            "path": "/home/agent/x",
            "readonly": "true",
        }
    })
    fake.stub("device", "add", "disk", rc=1, stderr="no space")
    with pytest.raises(AsbxError, match=re.escape("/home/agent/x")):
        guest.sync_mounts(fake, "sandbox-demo", mounts)


# ---------------------------------------------------------------------------
# wait_for_agent
# ---------------------------------------------------------------------------

def test_wait_for_agent_raises_after_attempts_exhausted_with_last_stderr(monkeypatch):
    fake = FakeIncus()
    fake.stub("exec", "true", rc=1, stderr="agent not ready")
    monkeypatch.setattr(guest.time, "sleep", lambda s: None)
    with pytest.raises(AsbxError, match="agent not ready"):
        guest.wait_for_agent(fake, "sandbox-demo", attempts=3, interval=0)
    tries = [c for c in fake.calls if "true" in " ".join(c)]
    assert len(tries) == 3


# ---------------------------------------------------------------------------
# write_guest_env
# ---------------------------------------------------------------------------

def test_write_guest_env_pushes_profile_script_and_claude_settings_with_manifest_env_precedence():
    cfg = {
        "guest_user": "agent",
        "harness_env": {"FOO": "base", "SHARED": "harness"},
        "defaults": {"env": {}},
    }
    manifest_env = {"SHARED": "manifest-wins"}
    fake = FakeIncus()

    guest.write_guest_env(fake, "sandbox-demo", cfg, manifest_env)

    assert "/etc/profile.d/asbx-env.sh" in fake.pushed
    profile_body = fake.pushed["/etc/profile.d/asbx-env.sh"]
    assert "SHARED='manifest-wins'" in profile_body
    assert "SHARED='harness'" not in profile_body

    settings_path = "/home/agent/.claude/settings.json"
    assert settings_path in fake.pushed
    parsed = json.loads(fake.pushed[settings_path])
    assert parsed["env"]["SHARED"] == "manifest-wins"

    # Shims before ~/.local/bin before /usr/local/bin: repo pins must beat the base Node.
    assert 'PATH="/home/agent/.local/share/mise/shims:/home/agent/.local/bin:/usr/local/bin:' in fake.pushed["/etc/environment"]


# ---------------------------------------------------------------------------
# render_workspace_cd — only interactive ssh logins move to /workspace
# ---------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("script") is None, reason="needs util-linux script(1) for a tty")
@pytest.mark.parametrize("ssh, tty, start_in_home, lands_in_workspace", [
    (True, True, True, True),
    (True, False, True, False),    # `ssh host cmd`, scp, VSCode's bootstrap
    (False, True, True, False),    # `incus exec` shells
    (True, True, False, False),    # a login that already chose its directory
])
def test_workspace_cd_moves_only_interactive_ssh_logins_from_home(
        tmp_path, ssh, tty, start_in_home, lands_in_workspace):
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    script = tmp_path / "asbx-cwd.sh"
    script.write_text(guest.render_workspace_cd(str(workspace)))
    env = {"PATH": os.environ["PATH"], "HOME": str(home)}
    if ssh:
        env["SSH_CONNECTION"] = "10.0.0.2 5000 10.0.0.3 22"
    start = home if start_in_home else tmp_path
    inner = f'cd {start} && PWD={start} && . {script} && pwd'
    argv = ["script", "-qec", f"sh -c '{inner}'", "/dev/null"] if tty else ["sh", "-c", inner]

    out = subprocess.run(argv, env=env, stdin=None if tty else subprocess.DEVNULL,
                         capture_output=True, text=True, check=True).stdout

    assert out.strip().splitlines()[-1] == str(workspace if lands_in_workspace else start)


# ---------------------------------------------------------------------------
# authorized_keys — the asbx key plus ssh.extra_pubkeys
# ---------------------------------------------------------------------------

KEY_A = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA asbx"
KEY_B = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB laptop"


def test_render_authorized_keys_lists_primary_then_extras_and_drops_duplicates():
    body = guest.render_authorized_keys(KEY_A + "\n", [KEY_B, KEY_A])

    keys = [line for line in body.splitlines() if not line.startswith("#")]
    assert keys == [KEY_A, KEY_B]


@pytest.mark.parametrize("bad", [
    'command="touch /pwned" ssh-ed25519 AAAAC3Nza x',
    "",
    "ssh-ed25519 AAAAC3Nza x\nssh-ed25519 BBBBC3Nza y",
    "not-a-key",
])
def test_render_authorized_keys_rejects_anything_but_a_bare_single_line_key(bad):
    with pytest.raises(AsbxError, match="extra_pubkeys"):
        guest.render_authorized_keys(KEY_A, [bad])


def test_sync_authorized_keys_writes_owner_only_file_in_the_guest_users_ssh_dir(cfg, tmp_path):
    key = tmp_path / "id_ed25519"
    (tmp_path / "id_ed25519.pub").write_text(KEY_A + "\n")
    cfg["ssh"]["key"] = str(key)
    cfg["ssh"]["extra_pubkeys"] = [KEY_B]
    fake = FakeIncus()

    guest.sync_authorized_keys(fake, "sandbox-demo", cfg)

    assert ["exec", "sandbox-demo", "--", "install", "-d", "-m", "0700",
            "-o", "1000", "-g", "1000", "/home/agent/.ssh"] in fake.calls
    pushed = fake.pushed["/home/agent/.ssh/authorized_keys"]
    assert KEY_A in pushed and KEY_B in pushed
    push_call = next(c for c in fake.calls if c[:2] == ["file", "push"])
    assert push_call[3:] == ["sandbox-demo/home/agent/.ssh/authorized_keys",
                             "--mode", "0600", "--uid", "1000", "--gid", "1000"]


def test_sync_authorized_keys_names_the_missing_public_key(cfg, tmp_path):
    cfg["ssh"]["key"] = str(tmp_path / "absent")

    with pytest.raises(AsbxError, match="asbx init"):
        guest.sync_authorized_keys(FakeIncus(), "sandbox-demo", cfg)


def test_render_mise_config_is_valid_toml_with_quoted_tool_names():
    import tomllib
    parsed = tomllib.loads(guest.render_mise_config({"java": "21", "aqua:apache/maven": "3.9"}))
    assert parsed["settings"]["trusted_config_paths"] == ["/workspace"]
    assert parsed["tools"] == {"java": "21", "aqua:apache/maven": "3.9"}


def test_render_mise_config_without_tools_has_no_tools_table():
    import tomllib
    assert "tools" not in tomllib.loads(guest.render_mise_config({}))


def test_write_mise_config_creates_system_dir_and_pushes_root_owned_file():
    fake = FakeIncus()

    guest.write_mise_config(fake, "sandbox-demo", {"maven": "3.9"})

    assert any(c[-3:] == ["mkdir", "-p", "/etc/mise"] for c in fake.calls)
    push = next(c for c in fake.calls if c[:2] == ["file", "push"])
    assert push[3] == "sandbox-demo/etc/mise/config.toml"
    assert push[4:] == ["--mode", "0644", "--uid", "0", "--gid", "0"]


def test_render_etc_environment_puts_shims_before_local_bin_before_system_dirs():
    body = guest.render_etc_environment("agent")
    path = next(l for l in body.splitlines() if l.startswith("PATH=")).split('"')[1].split(":")
    assert path[:3] == ["/home/agent/.local/share/mise/shims", "/home/agent/.local/bin", "/usr/local/bin"]


# ---------------------------------------------------------------------------
# configure_shell
# ---------------------------------------------------------------------------

USERMOD_ZSH = ["exec", "sandbox-demo", "--", "usermod", "-s", "/usr/bin/zsh", "agent"]


def _shell_cfg(shell: str) -> dict:
    return {"shell": shell, "guest_user": "agent"}


def test_configure_shell_zsh_installs_when_missing_and_sets_login_shell():
    fake = FakeIncus()
    fake.stub("test", "-x", "/usr/bin/zsh", rc=1)

    guest.configure_shell(fake, "sandbox-demo", _shell_cfg("zsh"))

    install = next(i for i, c in enumerate(fake.calls) if "apt-get install -y -q zsh" in c[-1])
    assert install < fake.calls.index(USERMOD_ZSH)
    assert "mise activate zsh" in fake.pushed["/etc/zsh/zprofile"]
    assert "asbx-env.sh" in fake.pushed["/etc/zsh/zprofile"]


def test_configure_shell_zsh_present_skips_install():
    fake = FakeIncus()

    guest.configure_shell(fake, "sandbox-demo", _shell_cfg("zsh"))

    assert not any("apt-get" in " ".join(c) for c in fake.calls)
    assert USERMOD_ZSH in fake.calls


def test_configure_shell_zsh_install_failure_raises():
    fake = FakeIncus()
    fake.stub("test", "-x", "/usr/bin/zsh", rc=1)
    fake.stub("apt-get", rc=100)

    with pytest.raises(AsbxError, match="installing zsh"):
        guest.configure_shell(fake, "sandbox-demo", _shell_cfg("zsh"))


def test_configure_shell_bash_only_sets_login_shell():
    fake = FakeIncus()

    guest.configure_shell(fake, "sandbox-demo", _shell_cfg("bash"))

    assert ["exec", "sandbox-demo", "--", "usermod", "-s", "/bin/bash", "agent"] in fake.calls
    assert "/etc/zsh/zprofile" not in fake.pushed


def test_configure_shell_zsh_creates_an_empty_zshrc_only_when_absent():
    fake = FakeIncus()

    guest.configure_shell(fake, "sandbox-demo", _shell_cfg("zsh"))

    assert any("test -e ~/.zshrc || : > ~/.zshrc" in c[-1] for c in fake.calls)


def test_configure_shell_zsh_install_failure_leaves_login_shell_alone():
    fake = FakeIncus()
    fake.stub("test", "-x", "/usr/bin/zsh", rc=1)
    fake.stub("apt-get", rc=100)

    with pytest.raises(AsbxError):
        guest.configure_shell(fake, "sandbox-demo", _shell_cfg("zsh"))

    assert not any("usermod" in c for c in fake.calls)
