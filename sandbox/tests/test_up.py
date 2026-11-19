"""Tests for asbxlib.up over the Incus backend, plus one `up` over the Tart
backend. Guest-side steps are stubbed out; what is checked is the order of
the host-side steps around them."""
from __future__ import annotations

import os
import plistlib
import stat

import pytest

from asbxlib import incusbackend, launchd, manifest, tartbackend, up
from asbxlib.errors import AsbxError
from asbxlib.incusbackend import IncusBackend
from asbxlib.tartbackend import TartBackend

from fake_incus import FakeIncus
from fake_tart import FakeLaunchctl, FakeTart

LOADED = manifest.Manifest(
    path="/tmp/x.yaml", name="demo", instance="sandbox-demo",
    resources={"cpu": 2, "memory": "4GiB", "disk": "20GiB"},
    repos=[], mounts=[], compose_path=None, env={}, auth=[],
    setup_path=None, remotes={},
)


@pytest.fixture
def up_env(monkeypatch, cfg, tmp_path):
    """Returns the NIC pins as (address, started yet) tuples."""
    monkeypatch.setattr(up.manifest, "load_manifest", lambda group, cfg: LOADED)
    monkeypatch.setattr(up.manifest, "manifest_sha256", lambda path: "sha0")
    monkeypatch.setattr(incusbackend.netacl, "ensure_acl", lambda incus, cfg: None)
    monkeypatch.setattr(incusbackend.ingress, "require_firewall", lambda cfg: None)
    monkeypatch.setattr(incusbackend, "wait_for_agent", lambda *a, **k: None)
    monkeypatch.setattr(incusbackend.guest, "wait_for_cloud_init", lambda g: None)
    pins: list[tuple] = []
    monkeypatch.setattr(incusbackend.netacl, "pin_nic", lambda incus, instance, cfg, address=None: pins.append(
        (address, ["start", instance] in incus.calls)) or "10.210.86.192")
    for name in ("set_hostname", "sync_authorized_keys", "write_guest_env", "write_git_identity",
                 "configure_shell", "start_paseo"):
        monkeypatch.setattr(up.guest, name, lambda *a, **k: None)
    monkeypatch.setattr(up.sshconf, "write_ssh_config", lambda *a, **k: None)
    monkeypatch.setattr(up.harnesssync, "sync_instance", lambda *a, **k: None)
    (tmp_path / "key.pub").write_text("ssh-ed25519 AAAA test\n")
    cfg["ssh"]["key"] = str(tmp_path / "key")
    return pins


def _with_image(cfg, fake: FakeIncus) -> FakeIncus:
    fake.stub("image", "alias", "list", "--format", "json",
              stdout=f'[{{"name": "{cfg["image_alias"]}", "target": "abc123"}}]')
    return fake


def _existing_instance(cfg, egress_mode: str) -> FakeIncus:
    fake = _with_image(cfg, FakeIncus())
    fake.stub("config", "get", "user.asbx.egress_mode", stdout=egress_mode)
    fake.set_instance(exists=True, state="Running")
    return fake


def _new_instance(cfg) -> FakeIncus:
    fake = _with_image(cfg, FakeIncus())
    fake.set_instance(exists=False)
    return fake


def _acl_attached(fake: FakeIncus) -> bool:
    return ["config", "set", "sandbox-demo", "user.asbx.egress_mode", "acl"] in fake.calls


def test_cmd_up_creates_without_user_data_and_attaches_the_acl_before_the_first_start(up_env, cfg):
    fake = _new_instance(cfg)

    up.cmd_up(IncusBackend(fake), cfg, "demo", no_auth=True, no_provision=True)

    [create] = [c for c in fake.calls if c[0] == "create"]
    assert create == ["create", cfg["image_alias"], "sandbox-demo", "--vm",
                      "-c", "limits.cpu=2", "-c", "limits.memory=4GiB", "-d", "root,size=20GiB"]
    start = fake.calls.index(["start", "sandbox-demo"])
    acl = next(i for i, c in enumerate(fake.calls) if c[:4] == ["config", "device", "override", "sandbox-demo"]
               and "security.acls=asbx-egress" in c)
    attached = fake.calls.index(["config", "set", "sandbox-demo", "user.asbx.egress_mode", "acl"])
    assert acl < start and attached < start
    for key, value in (("group", "demo"), ("base_image", "abc123"), ("manifest_sha256", "sha0")):
        assert fake.calls.index(["config", "set", "sandbox-demo", f"user.asbx.{key}", value]) < start


def test_cmd_up_refuses_to_create_without_a_base_image(up_env, cfg):
    fake = FakeIncus()
    fake.set_instance(exists=False)

    with pytest.raises(AsbxError, match="asbx build-base"):
        up.cmd_up(IncusBackend(fake), cfg, "demo", no_auth=True, no_provision=True)

    assert not any(c[0] == "create" for c in fake.calls)


def test_cmd_up_attaches_the_acl_to_an_existing_instance_that_never_got_one(up_env, cfg):
    fake = _existing_instance(cfg, egress_mode="")

    up.cmd_up(IncusBackend(fake), cfg, "demo", no_auth=True, no_provision=True)

    assert any(c[:4] == ["config", "device", "override", "sandbox-demo"] for c in fake.calls)
    assert _acl_attached(fake)


def test_cmd_up_leaves_the_nic_alone_when_the_acl_is_already_attached(up_env, cfg):
    fake = _existing_instance(cfg, egress_mode="acl")

    up.cmd_up(IncusBackend(fake), cfg, "demo", no_auth=True, no_provision=True)

    assert not any(c[:2] == ["config", "device"] for c in fake.calls)
    assert ["start", "sandbox-demo"] not in fake.calls


def test_cmd_up_attaches_the_acl_to_an_instance_built_with_the_removed_guest_firewall_fallback(up_env, cfg, capsys):
    fake = _existing_instance(cfg, egress_mode="guest-nft")

    up.cmd_up(IncusBackend(fake), cfg, "demo", no_auth=True, no_provision=True)

    assert any(c[:4] == ["config", "device", "override", "sandbox-demo"] for c in fake.calls)
    assert _acl_attached(fake)
    assert "asbx rebuild demo" in capsys.readouterr().err


@pytest.mark.parametrize("prefix_match", ["sandbox-demo2", "sandbox-demo-snap-pre1"])
def test_cmd_up_creates_a_sandbox_whose_name_only_prefixes_an_existing_instance(up_env, cfg, prefix_match):
    # A sibling group's sandbox, or a snapshot copy left after `rebuild` deleted
    # the sandbox, must not pass for the sandbox itself.
    fake = _with_image(cfg, FakeIncus())
    fake.set_instance(exists=True, state="Stopped", name=prefix_match)

    up.cmd_up(IncusBackend(fake), cfg, "demo", no_auth=True, no_provision=True)

    assert any(c[:3] == ["create", cfg["image_alias"], "sandbox-demo"] for c in fake.calls)
    assert ["start", "sandbox-demo"] in fake.calls


def test_cmd_up_syncs_harness_settings_before_auth_and_survives_a_sync_failure(up_env, cfg, monkeypatch, capsys):
    order: list[str] = []

    def failing_sync(*a, **k):
        order.append("sync")
        raise AsbxError("cannot read x")

    monkeypatch.setattr(up.harnesssync, "sync_instance", failing_sync)
    monkeypatch.setattr(up.auth, "run_auth", lambda *a, **k: order.append("auth"))
    monkeypatch.setattr(up.provision, "run_provision", lambda *a, **k: order.append("provision"))

    up.cmd_up(IncusBackend(_existing_instance(cfg, egress_mode="acl")), cfg, "demo",
              no_auth=False, no_provision=False)

    assert order == ["sync", "auth", "provision"]
    assert "harness settings not synced: cannot read x" in capsys.readouterr().err


def test_cmd_up_syncs_harness_settings_even_with_no_auth_and_no_provision(up_env, cfg, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(up.harnesssync, "sync_instance", lambda *a, **k: calls.append("sync"))

    up.cmd_up(IncusBackend(_existing_instance(cfg, egress_mode="acl")), cfg, "demo",
              no_auth=True, no_provision=True)

    assert calls == ["sync"]


def test_cmd_up_warns_and_continues_when_shell_setup_fails(up_env, cfg, monkeypatch, capsys):
    def boom(*a, **k):
        raise AsbxError("installing zsh failed")
    monkeypatch.setattr(up.guest, "configure_shell", boom)

    up.cmd_up(IncusBackend(_existing_instance(cfg, egress_mode="acl")), cfg, "demo",
              no_auth=True, no_provision=True)

    out = capsys.readouterr()
    assert "shell not configured: installing zsh failed" in out.out + out.err


def test_cmd_up_refuses_before_touching_incus_when_the_ingress_firewall_is_not_in_force(up_env, cfg, monkeypatch):
    def no_firewall(cfg):
        raise AsbxError("asbx-firewall.service is not active; run 'asbx ingress setup'")
    monkeypatch.setattr(incusbackend.ingress, "require_firewall", no_firewall)
    fake = _existing_instance(cfg, egress_mode="acl")

    with pytest.raises(AsbxError, match="asbx ingress setup"):
        up.cmd_up(IncusBackend(fake), cfg, "demo", no_auth=True, no_provision=True)

    assert fake.calls == []


def test_cmd_up_pins_a_rebuilt_sandbox_s_previous_address_before_its_first_start(up_env, cfg):
    # The guest's first DHCP lease must already be the kept address, or IP filtering would drop it.
    up.cmd_up(IncusBackend(_new_instance(cfg)), cfg, "demo", no_auth=True, no_provision=True,
              ipv4="10.210.86.50")

    assert up_env[0] == ("10.210.86.50", False)


def test_cmd_up_pins_an_existing_sandbox_after_its_acl_is_in_place(up_env, cfg, monkeypatch):
    fake = _existing_instance(cfg, egress_mode="")
    seen: list[bool] = []
    monkeypatch.setattr(incusbackend.netacl, "pin_nic", lambda incus, instance, cfg, address=None: seen.append(
        _acl_attached(fake)) or "10.210.86.192")

    up.cmd_up(IncusBackend(fake), cfg, "demo", no_auth=True, no_provision=True)

    assert seen == [True]


def test_cmd_up_stops_when_the_nic_cannot_be_pinned(up_env, cfg, monkeypatch):
    def refuse(*a, **k):
        raise AsbxError("could not pin")
    monkeypatch.setattr(incusbackend.netacl, "pin_nic", refuse)
    synced: list[str] = []
    monkeypatch.setattr(up.guest, "sync_authorized_keys", lambda *a: synced.append("keys"))

    with pytest.raises(AsbxError, match="could not pin"):
        up.cmd_up(IncusBackend(_existing_instance(cfg, egress_mode="acl")), cfg, "demo",
                  no_auth=True, no_provision=True)
    assert synced == []


def test_cmd_up_restarts_a_running_sandbox_whose_mounts_changed(up_env, cfg, tmp_path, monkeypatch):
    tmp_path.chmod(0o755)
    monkeypatch.setattr(up.manifest, "load_manifest", lambda group, cfg: manifest.Manifest(
        **{**LOADED.__dict__, "mounts": [manifest.Mount(host=str(tmp_path), guest="/mnt/x", mode="ro")]}))
    fake = _existing_instance(cfg, egress_mode="acl")

    up.cmd_up(IncusBackend(fake), cfg, "demo", no_auth=True, no_provision=True)

    added = next(i for i, c in enumerate(fake.calls) if c[:5] == ["config", "device", "add", "sandbox-demo", "mnt-0"])
    assert added < fake.calls.index(["restart", "sandbox-demo"])


def test_run_provision_records_the_manifest_sha256_after_provisioning(monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(up.provision, "run_provision", lambda *a: order.append("provision"))
    monkeypatch.setattr(up.manifest, "manifest_sha256", lambda path: "sha1")
    fake = FakeIncus()
    monkeypatch.setattr(fake, "config_set", lambda inst, key, value: order.append(f"{key}={value}"))

    up.run_provision(IncusBackend(fake), LOADED, {"guest_user": "agent"})

    assert order == ["provision", "user.asbx.manifest_sha256=sha1"]


def test_cmd_up_over_tart_boots_once_through_a_softnet_job_and_proxies_ssh(up_env, cfg, config_root, monkeypatch):
    fake = FakeTart()
    launchctl = FakeLaunchctl(fake)
    monkeypatch.setattr(launchd, "run_launchctl", launchctl)
    monkeypatch.setattr(tartbackend.platform, "mac_ver", lambda: ("26.1", ("", "", ""), "arm64"))
    softnet_dir = cfg["tart"]["softnet_dir"]
    safe = {softnet_dir: os.stat_result((stat.S_IFDIR | 0o755, 0, 0, 1, 0, 0, 0, 0, 0, 0)),
            f"{softnet_dir}/softnet": os.stat_result((stat.S_IFREG | 0o4555, 0, 0, 1, 0, 0, 0, 0, 0, 0))}
    backend = TartBackend(fake.tart(), stat_fn=lambda path: safe[path])
    fake.add_vm(cfg["image_alias"])
    backend.meta_set(cfg["image_alias"], "built_at", "20261118T000000Z")   # what build-base records
    routes = []
    monkeypatch.setattr(up.sshconf, "write_ssh_config", lambda cfg, instance, route: routes.append(route))

    up.cmd_up(backend, cfg, "demo", no_auth=True, no_provision=True)

    [body] = launchctl.bootstrapped
    assert any(arg.startswith("--net-softnet-block=") for arg in plistlib.loads(body)["ProgramArguments"])
    assert backend.state("sandbox-demo") == "running"
    assert backend.meta_get("sandbox-demo", "base_image") == "20261118T000000Z"
    [route] = routes
    assert route.hostname is None and route.proxy_command.endswith("ssh-proxy sandbox-demo")
