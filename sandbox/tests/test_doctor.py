"""Tests for asbxlib.doctor."""
from __future__ import annotations

import re

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


# ---------------------------------------------------------------------------
# aws-creds checks
# ---------------------------------------------------------------------------

def _aws_manifest(auth=("aws-creds",), profiles=("p",)):
    from asbxlib.manifest import Manifest
    return Manifest(path="/demo.yaml", name="demo", instance="sandbox-demo",
                    auth=list(auth), aws_profiles=list(profiles))


def _creds_body(minutes: int) -> str:
    from datetime import datetime, timedelta, timezone
    exp = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
    return '{"Version":1,"AccessKeyId":"A","SecretAccessKey":"S","SessionToken":"T","Expiration":"%s"}' % exp


@pytest.fixture
def aws_env(doctor_env, monkeypatch):
    import subprocess
    monkeypatch.setattr(doctor.manifest_mod, "load_manifest", lambda g, cfg: _aws_manifest())
    monkeypatch.setattr(doctor.host, "systemctl_user",
                        lambda *a: subprocess.CompletedProcess([], 0, stdout="active", stderr=""))


def _aws_incus(cfg, **creds_stub):
    fake = _incus(cfg)
    fake.stub("ip", "-4", "route", "show", "default", stdout="")
    if creds_stub:
        fake.stub("cat", "/home/agent/.asbx/aws/p.json", **creds_stub)
    return fake


def test_cmd_doctor_reports_timer_and_fresh_credentials(aws_env, capsys, cfg):
    cmd = _aws_incus(cfg, stdout=_creds_body(60))

    doctor.cmd_doctor(cmd, cfg, fix=False)

    out = capsys.readouterr().out
    assert "PASS asbx-aws-refresh.timer active" in out
    assert "PASS aws CLI on PATH" in out
    assert re.search(r"PASS sandbox-demo: aws profile p: expires in (59|60) min\n", out)


def test_aws_credential_results_use_the_given_clock_and_report_each_profile():
    from datetime import datetime, timezone
    now = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
    fake = FakeIncus()
    body = '{"Expiration": "2030-01-01T12:05:00+00:00"}'
    fake.stub("cat", "/home/agent/.asbx/aws/soon.json", stdout=body)
    fake.stub("cat", "/home/agent/.asbx/aws/gone.json", rc=1)

    results = doctor.aws_credential_results(
        fake, _aws_manifest(profiles=["soon", "gone"]), "demo", "agent", now)

    assert results[0][0] == "WARN" and "aws profile soon: expires in 5 min" in results[0][1]
    assert results[1][0] == "FAIL" and "aws profile gone: missing" in results[1][1]
    assert all("asbx aws-refresh demo" in msg for _s, msg in results)

def test_cmd_doctor_flags_expired_credentials_with_remedy(aws_env, capsys, cfg):
    cmd = _aws_incus(cfg, stdout=_creds_body(-5))

    rc = doctor.cmd_doctor(cmd, cfg, fix=False)

    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL sandbox-demo: aws profile p: expired" in out
    assert "asbx aws-refresh" in out and "aws sso login" in out


def test_cmd_doctor_flags_missing_credential_file(aws_env, capsys, cfg):
    cmd = _aws_incus(cfg, rc=1, stderr="No such file")

    rc = doctor.cmd_doctor(cmd, cfg, fix=False)

    assert rc == 1
    assert "FAIL sandbox-demo: aws profile p: missing" in capsys.readouterr().out


def test_cmd_doctor_warns_when_timer_inactive(aws_env, monkeypatch, capsys, cfg):
    import subprocess
    monkeypatch.setattr(doctor.host, "systemctl_user",
                        lambda *a: subprocess.CompletedProcess([], 3, stdout="inactive", stderr=""))

    doctor.cmd_doctor(_aws_incus(cfg, stdout=_creds_body(60)), cfg, fix=False)

    assert "WARN asbx-aws-refresh.timer active" in capsys.readouterr().out


def test_cmd_doctor_skips_aws_checks_without_aws_creds_provider(doctor_env, monkeypatch, capsys, cfg):
    monkeypatch.setattr(doctor.manifest_mod, "load_manifest",
                        lambda g, c: _aws_manifest(auth=["github-gh"]))

    doctor.cmd_doctor(_aws_incus(cfg), cfg, fix=False)

    out = capsys.readouterr().out
    assert "aws" not in out.replace("sandbox-demo", "")
