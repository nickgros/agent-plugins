"""Tests for asbxlib.up. Only the egress-ACL guard on an already-existing
instance is covered here; every other collaborator is stubbed out."""
from __future__ import annotations

import dataclasses

import pytest

from asbxlib import manifest, up
from asbxlib.errors import AsbxError

from fake_incus import FakeIncus


@pytest.fixture
def up_env(monkeypatch, cfg):
    loaded = manifest.Manifest(
        path="/tmp/x.yaml", name="demo", instance="sandbox-demo", resources={},
        repos=[], mounts=[], compose_path=None, env={}, auth=[],
        setup_path=None, remotes={},
    )
    monkeypatch.setattr(up.manifest, "load_manifest", lambda group, cfg: loaded)
    monkeypatch.setattr(up.netacl, "ensure_acl", lambda incus, cfg: None)
    monkeypatch.setattr(up.ingress, "require_firewall", lambda cfg: None)
    monkeypatch.setattr(up.netacl, "pin_nic", lambda incus, instance, cfg, address=None: "10.210.86.192")
    for name in ("wait_for_agent", "wait_for_cloud_init", "sync_authorized_keys",
                 "write_guest_env", "write_git_identity", "configure_shell"):
        monkeypatch.setattr(up.guest, name, lambda *a, **k: None)
    monkeypatch.setattr(up.guest, "sync_mounts", lambda *a, **k: False)
    monkeypatch.setattr(up.sshconf, "write_ssh_config", lambda *a, **k: "")
    monkeypatch.setattr(up.harnesssync, "sync_instance", lambda *a, **k: None)


def _existing_instance(cfg, egress_mode: str) -> FakeIncus:
    fake = FakeIncus()
    fake.stub("image", "alias", "list", "--format", "json",
              stdout=f'[{{"name": "{cfg["image_alias"]}", "target": "abc123"}}]')
    fake.stub("config", "get", "user.asbx.egress_mode", stdout=egress_mode)
    fake.set_instance(exists=True, state="Running")
    return fake


def test_cmd_up_attaches_the_acl_to_an_existing_instance_that_never_got_one(up_env, cfg):
    fake = _existing_instance(cfg, egress_mode="")

    up.cmd_up(fake, cfg, "demo", no_auth=True, no_provision=True)

    assert any(c[:4] == ["config", "device", "override", "sandbox-demo"] for c in fake.calls)
    assert ["config", "set", "sandbox-demo", "user.asbx.egress_mode", "acl"] in fake.calls


def test_cmd_up_leaves_the_nic_alone_when_the_acl_is_already_attached(up_env, cfg):
    fake = _existing_instance(cfg, egress_mode="acl")

    up.cmd_up(fake, cfg, "demo", no_auth=True, no_provision=True)

    assert not any(c[:2] == ["config", "device"] for c in fake.calls)


def test_cmd_up_refuses_an_instance_built_with_the_removed_guest_firewall_fallback(up_env, cfg):
    fake = _existing_instance(cfg, egress_mode="guest-nft")

    with pytest.raises(AsbxError, match="asbx rebuild demo"):
        up.cmd_up(fake, cfg, "demo", no_auth=True, no_provision=True)

    assert not any(c[:2] == ["config", "device"] for c in fake.calls)


def test_cmd_up_syncs_harness_settings_before_auth_and_survives_a_sync_failure(up_env, cfg, monkeypatch, capsys):
    order: list[str] = []

    def failing_sync(*a, **k):
        order.append("sync")
        raise AsbxError("cannot read x")

    monkeypatch.setattr(up.harnesssync, "sync_instance", failing_sync)
    monkeypatch.setattr(up.auth, "run_auth", lambda *a, **k: order.append("auth"))
    monkeypatch.setattr(up.provision, "run_provision", lambda *a, **k: order.append("provision"))
    fake = _existing_instance(cfg, egress_mode="acl")

    up.cmd_up(fake, cfg, "demo", no_auth=False, no_provision=False)

    assert order == ["sync", "auth", "provision"]
    assert "harness settings not synced: cannot read x" in capsys.readouterr().err


def test_cmd_up_syncs_harness_settings_even_with_no_auth_and_no_provision(up_env, cfg, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(up.harnesssync, "sync_instance", lambda *a, **k: calls.append("sync"))

    up.cmd_up(_existing_instance(cfg, egress_mode="acl"), cfg, "demo", no_auth=True, no_provision=True)

    assert calls == ["sync"]


def test_cmd_up_warns_and_continues_when_shell_setup_fails(up_env, cfg, monkeypatch, capsys):
    def boom(*a, **k):
        raise AsbxError("installing zsh failed")
    monkeypatch.setattr(up.guest, "configure_shell", boom)
    fake = _existing_instance(cfg, egress_mode="acl")

    up.cmd_up(fake, cfg, "demo", no_auth=True, no_provision=True)

    out = capsys.readouterr()
    assert "shell not configured: installing zsh failed" in out.out + out.err


def test_cmd_up_refuses_before_touching_incus_when_the_ingress_firewall_is_not_in_force(up_env, cfg, monkeypatch):
    def no_firewall(cfg):
        raise AsbxError("asbx-firewall.service is not active; run 'asbx ingress setup'")
    monkeypatch.setattr(up.ingress, "require_firewall", no_firewall)
    fake = _existing_instance(cfg, egress_mode="acl")

    with pytest.raises(AsbxError, match="asbx ingress setup"):
        up.cmd_up(fake, cfg, "demo", no_auth=True, no_provision=True)

    assert fake.calls == []


def test_cmd_up_pins_a_rebuilt_sandbox_s_previous_address_before_its_first_start(up_env, cfg, monkeypatch, tmp_path):
    # The guest's first DHCP lease must already be the kept address, or IP filtering would drop it.
    fake = FakeIncus()
    pins: list[tuple] = []
    monkeypatch.setattr(up.netacl, "pin_nic", lambda incus, instance, cfg, address=None: pins.append(
        (address, ["start", "sandbox-demo"] in fake.calls)) or "10.210.86.50")
    monkeypatch.setattr(up.netacl, "apply_nic_acl", lambda *a: None)
    monkeypatch.setattr(up.manifest, "manifest_sha256", lambda path: "0")
    loaded = up.manifest.load_manifest("demo", cfg)
    monkeypatch.setattr(up.manifest, "load_manifest", lambda group, cfg: dataclasses.replace(
        loaded, resources={"cpu": 2, "memory": "4GiB", "disk": "20GiB"}))
    (tmp_path / "key.pub").write_text("ssh-ed25519 AAAA test\n")
    cfg["ssh"]["key"] = str(tmp_path / "key")
    fake.stub("image", "alias", "list", "--format", "json",
              stdout=f'[{{"name": "{cfg["image_alias"]}", "target": "abc123"}}]')
    fake.set_instance(exists=False)

    up.cmd_up(fake, cfg, "demo", no_auth=True, no_provision=True, ipv4="10.210.86.50")

    assert pins[0] == ("10.210.86.50", False)
