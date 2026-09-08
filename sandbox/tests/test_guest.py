"""Pure-logic tests for asbxlib.guest. No Incus required."""
from __future__ import annotations

import json
import os
import re
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

def test_render_instance_cloud_init_has_pubkey_for_guest_user_and_no_authkey():
    rendered = guest.render_instance_cloud_init("sandbox-demo", "dev", "ssh-ed25519 AAAAtest")
    assert "hostname: sandbox-demo" in rendered
    assert "- name: dev" in rendered
    assert "ssh-ed25519 AAAAtest" in rendered
    # The tailnet auth key must never reach a per-instance cloud-init.
    assert "authkey" not in rendered.lower()


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
