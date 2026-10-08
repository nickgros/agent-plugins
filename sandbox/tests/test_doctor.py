"""Tests for asbxlib.doctor."""
from __future__ import annotations

import pytest

from fake_incus import FakeIncus

from asbxlib import doctor


@pytest.fixture
def doctor_env(monkeypatch):
    """Pins the host-dependent probes doctor makes so a run depends only on
    the FakeIncus script, not on the machine's kvm, keys, or resolver."""
    monkeypatch.setattr(doctor.host, "which", lambda name: True)
    monkeypatch.setattr(doctor.host, "resolves", lambda name: True)
    monkeypatch.setattr(doctor.manifest_mod, "manifest_groups", lambda cfg: [("demo", "sandbox-demo")])
    monkeypatch.setattr(doctor.netacl, "acl_matches_config", lambda incus, cfg: True)


def _incus(cfg, *, state="Running", version="7.0.0", egress_mode="acl"):
    fake = FakeIncus()
    fake.stub("info", stdout=f'driver: qemu\n  server_version: "{version}"\n')
    fake.stub("image", "alias", "list", "--format", "json",
              stdout=f'[{{"name": "{cfg["image_alias"]}", "target": "abc123"}}]')
    fake.stub("config", "get", "user.asbx.egress_mode", stdout=egress_mode)
    fake.set_instance(exists=True, state=state)
    return fake


def test_cmd_doctor_missing_config_returns_1_and_reports_fail(monkeypatch, capsys):
    monkeypatch.setattr(doctor.host, "which", lambda name: True)

    rc = doctor.cmd_doctor(FakeIncus(), None, fix=False)

    assert rc == 1
    out = capsys.readouterr().out
    assert "FAIL config present" in out


def test_cmd_doctor_reports_network_checks_for_running_instance(doctor_env, capsys, cfg):
    fake = _incus(cfg)
    fake.stub("ip", "-4", "route", "show", "default", stdout="default via 10.210.86.1 dev enp5s0\n")
    fake.stub("dev/tcp", rc=0)

    doctor.cmd_doctor(fake, cfg, fix=False)

    out = capsys.readouterr().out
    assert "PASS sandbox-demo: guest has IPv4 default route" in out
    assert "PASS sandbox-demo: guest IPv4 internet egress" in out


def test_cmd_doctor_fails_route_check_and_skips_egress_check_when_no_default_route(doctor_env, capsys, cfg):
    fake = _incus(cfg)
    fake.stub("ip", "-4", "route", "show", "default", stdout="")

    rc = doctor.cmd_doctor(fake, cfg, fix=False)

    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL sandbox-demo: guest has IPv4 default route" in out
    assert "guest IPv4 internet egress" not in out


def test_cmd_doctor_skips_network_checks_for_stopped_instance(doctor_env, capsys, cfg):
    doctor.cmd_doctor(_incus(cfg, state="Stopped"), cfg, fix=False)

    out = capsys.readouterr().out
    assert "guest has IPv4 default route" not in out
    assert "resolves on the host" not in out


def test_cmd_doctor_fails_when_running_instance_name_does_not_resolve(doctor_env, monkeypatch, capsys, cfg):
    monkeypatch.setattr(doctor.host, "resolves", lambda name: False)
    fake = _incus(cfg)
    fake.stub("ip", "-4", "route", "show", "default", stdout="default via 10.210.86.1 dev enp5s0\n")

    rc = doctor.cmd_doctor(fake, cfg, fix=False)

    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL sandbox-demo: sandbox-demo.incus resolves on the host" in out
    assert "resolvectl dns incusbr0" in out


@pytest.mark.parametrize("egress_mode", ["guest-nft", ""])
def test_cmd_doctor_fails_instance_without_an_attached_acl(doctor_env, capsys, cfg, egress_mode):
    rc = doctor.cmd_doctor(_incus(cfg, state="Stopped", egress_mode=egress_mode), cfg, fix=False)

    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL sandbox-demo: egress ACL attached" in out


def test_cmd_doctor_passes_instance_with_an_attached_acl(doctor_env, capsys, cfg):
    doctor.cmd_doctor(_incus(cfg, state="Stopped"), cfg, fix=False)

    assert "PASS sandbox-demo: egress ACL attached" in capsys.readouterr().out


def test_cmd_doctor_fails_when_incus_is_too_old_for_bridged_nic_acls(doctor_env, capsys, cfg):
    rc = doctor.cmd_doctor(_incus(cfg, state="Stopped", version="6.0.0"), cfg, fix=False)

    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL Incus supports bridged-NIC ACLs" in out
    assert "6.0.0" in out
