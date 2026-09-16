"""Tests for asbxlib.doctor."""
from __future__ import annotations

from fake_incus import FakeIncus

from asbxlib import doctor


def test_cmd_doctor_missing_config_returns_1_and_reports_fail(monkeypatch, capsys):
    monkeypatch.setattr(doctor.host, "which", lambda name: True)
    monkeypatch.setattr(doctor.host, "tailscale_status", lambda: {"BackendState": "Running"})

    rc = doctor.cmd_doctor(FakeIncus(), None, fix=False)

    assert rc == 1
    out = capsys.readouterr().out
    assert "FAIL config present" in out


def test_cmd_doctor_reports_network_checks_for_running_instance(monkeypatch, capsys, cfg):
    monkeypatch.setattr(doctor.host, "which", lambda name: True)
    monkeypatch.setattr(doctor.host, "tailscale_status", lambda: {"BackendState": "Running"})
    monkeypatch.setattr(doctor.manifest_mod, "manifest_groups", lambda cfg: [("demo", "sandbox-demo")])
    monkeypatch.setattr(doctor.netacl, "acl_matches_config", lambda incus, cfg: True)
    cfg["tailscale"]["auth_key"] = "tskey-test"

    fake = FakeIncus()
    fake.stub("info", stdout="driver: qemu\n")
    fake.stub("image", "alias", "list", "--format", "json",
              stdout=f'[{{"name": "{cfg["image_alias"]}", "target": "abc123"}}]')
    fake.set_instance(exists=True, state="Running")
    fake.stub("ip", "-4", "route", "show", "default", stdout="default via 10.210.86.1 dev enp5s0\n")
    fake.stub("dev/tcp", rc=0)

    rc = doctor.cmd_doctor(fake, cfg, fix=False)

    out = capsys.readouterr().out
    assert rc == 0
    assert "PASS sandbox-demo: guest has IPv4 default route" in out
    assert "PASS sandbox-demo: guest IPv4 internet egress" in out


def test_cmd_doctor_fails_egress_check_but_skips_route_check_when_no_default_route(monkeypatch, capsys, cfg):
    monkeypatch.setattr(doctor.host, "which", lambda name: True)
    monkeypatch.setattr(doctor.host, "tailscale_status", lambda: {"BackendState": "Running"})
    monkeypatch.setattr(doctor.manifest_mod, "manifest_groups", lambda cfg: [("demo", "sandbox-demo")])
    monkeypatch.setattr(doctor.netacl, "acl_matches_config", lambda incus, cfg: True)

    fake = FakeIncus()
    fake.set_instance(exists=True, state="Running")
    fake.stub("ip", "-4", "route", "show", "default", stdout="")

    rc = doctor.cmd_doctor(fake, cfg, fix=False)

    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL sandbox-demo: guest has IPv4 default route" in out
    assert "guest IPv4 internet egress" not in out


def test_cmd_doctor_skips_network_checks_for_stopped_instance(monkeypatch, capsys, cfg):
    monkeypatch.setattr(doctor.host, "which", lambda name: True)
    monkeypatch.setattr(doctor.host, "tailscale_status", lambda: {"BackendState": "Running"})
    monkeypatch.setattr(doctor.manifest_mod, "manifest_groups", lambda cfg: [("demo", "sandbox-demo")])
    monkeypatch.setattr(doctor.netacl, "acl_matches_config", lambda incus, cfg: True)
    cfg["tailscale"]["auth_key"] = "tskey-test"

    fake = FakeIncus()
    fake.stub("info", stdout="driver: qemu\n")
    fake.stub("image", "alias", "list", "--format", "json",
              stdout=f'[{{"name": "{cfg["image_alias"]}", "target": "abc123"}}]')
    fake.set_instance(exists=True, state="Stopped")

    rc = doctor.cmd_doctor(fake, cfg, fix=False)

    out = capsys.readouterr().out
    assert rc == 0
    assert "guest has IPv4 default route" not in out
