"""Tests for asbxlib.up. Only the egress-ACL guard on an already-existing
instance is covered here; every other collaborator is stubbed out."""
from __future__ import annotations

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
    for name in ("wait_for_agent", "wait_for_cloud_init", "sync_authorized_keys",
                 "write_guest_env", "write_git_identity"):
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
