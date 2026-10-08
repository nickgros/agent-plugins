"""Tests for asbxlib.awscreds. The real aws CLI and systemctl are never run."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from asbxlib import awscreds, hostsetup, manifest
from asbxlib.errors import AsbxError
from fake_incus import FakeIncus

NOW = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)


def _exported(expiration: str = "2030-01-01T13:00:00+00:00") -> str:
    return json.dumps({"Version": 1, "AccessKeyId": "A", "SecretAccessKey": "S",
                       "SessionToken": "T", "Expiration": expiration})


def _proc(rc: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


def _manifest(instance: str, profiles: list[str], auth=("aws-creds",)) -> manifest.Manifest:
    return manifest.Manifest(path="/x.yaml", name=instance, instance=instance,
                             auth=list(auth), aws_profiles=profiles)


# ---------------------------------------------------------------------------
# parse_exported
# ---------------------------------------------------------------------------

def test_parse_exported_refuses_long_term_keys():
    body = '{"Version":1,"AccessKeyId":"A","SecretAccessKey":"S"}'
    with pytest.raises(AsbxError, match="long-term"):
        awscreds.parse_exported("p", body)


def test_parse_exported_unreadable_output():
    with pytest.raises(AsbxError, match="unreadable"):
        awscreds.parse_exported("p", "not json")


def test_parse_exported_accepts_temporary_credentials():
    assert awscreds.parse_exported("p", _exported())["SessionToken"] == "T"


# ---------------------------------------------------------------------------
# render_guest_aws_config
# ---------------------------------------------------------------------------

def test_render_guest_aws_config_sections_and_region():
    out = awscreds.render_guest_aws_config("agent", {"default": "", "x": "us-east-1"})

    assert "[default]\ncredential_process = cat /home/agent/.asbx/aws/default.json\n" in out
    assert "[profile x]\ncredential_process = cat /home/agent/.asbx/aws/x.json\nregion = us-east-1\n" in out
    assert out.count("region =") == 1
    assert out.endswith("\n")


# ---------------------------------------------------------------------------
# push
# ---------------------------------------------------------------------------

def _joined_calls(fake: FakeIncus) -> list[str]:
    return [" ".join(c) for c in fake.calls]


def test_push_writes_tmp_then_renames_then_prunes_stale():
    fake = FakeIncus()

    awscreds.push(fake, "sandbox-a", "agent", {"p": ('{"k": 1}', "us-east-1")})

    calls = _joined_calls(fake)
    tmp_i = next(i for i, c in enumerate(calls) if "file push" in c and "/home/agent/.asbx/aws/p.json.tmp" in c)
    mv_i = calls.index("exec sandbox-a -- runuser -u agent -- bash -c cd /home/agent && "
                       "mv -f /home/agent/.asbx/aws/p.json.tmp /home/agent/.asbx/aws/p.json")
    assert tmp_i < mv_i
    find = next(c for c in fake.calls if "find" in " ".join(c))
    script = find[-1]
    for tok in ("'!'", "-name", "p.json", "-delete"):
        assert tok in script
    assert fake.pushed["/home/agent/.asbx/aws/p.json.tmp"] == '{"k": 1}'
    assert "credential_process = cat /home/agent/.asbx/aws/p.json" in fake.pushed["/home/agent/.aws/config"]
    # root creates the directories owned by the guest user
    assert any(c[:4] == ["exec", "sandbox-a", "--", "install"] for c in fake.calls)


# ---------------------------------------------------------------------------
# cmd_aws_refresh
# ---------------------------------------------------------------------------

@pytest.fixture
def refresh_env(monkeypatch):
    """Two groups a and b, instances sandbox-a / sandbox-b; returns (exports, manifests)."""
    exports: list[str] = []
    manifests = {"a": _manifest("sandbox-a", ["shared"]), "b": _manifest("sandbox-b", ["shared"])}

    def export(profile):
        exports.append(profile)
        return _proc(stdout=_exported())

    monkeypatch.setattr(awscreds.host, "aws_export_credentials", export)
    monkeypatch.setattr(awscreds.host, "aws_profile_region", lambda p: "us-east-1")
    monkeypatch.setattr(awscreds.manifest, "manifest_groups",
                        lambda cfg: [(g, m.instance) for g, m in manifests.items()])
    monkeypatch.setattr(awscreds.manifest, "load_manifest", lambda g, cfg: manifests[g])
    return exports, manifests


def _running(fake: FakeIncus, *instances: str, stopped: tuple[str, ...] = ()) -> None:
    for inst in instances:
        fake.stub("info", inst)
        fake.stub("list", inst, "--format", "json", stdout='[{"status": "Running"}]')
    for inst in stopped:
        fake.stub("info", inst)
        fake.stub("list", inst, "--format", "json", stdout='[{"status": "Stopped"}]')


def _pushed_instances(fake: FakeIncus) -> set[str]:
    return {c[3].split("/", 1)[0] for c in fake.calls if c[:2] == ["file", "push"]}


def test_refresh_exports_each_profile_once_per_run(refresh_env):
    exports, _ = refresh_env
    fake = FakeIncus()
    _running(fake, "sandbox-a", "sandbox-b")

    rc = awscreds.cmd_aws_refresh(fake, {"guest_user": "agent"}, None)

    assert rc == 0
    assert exports == ["shared"]
    assert _pushed_instances(fake) == {"sandbox-a", "sandbox-b"}


def test_refresh_partial_failure_returns_1_and_still_pushes_other_instance(refresh_env, monkeypatch):
    exports, manifests = refresh_env
    manifests["a"] = _manifest("sandbox-a", ["bad"])
    monkeypatch.setattr(
        awscreds.host, "aws_export_credentials",
        lambda p: _proc(rc=255, stderr="expired") if p == "bad" else _proc(stdout=_exported()))
    fake = FakeIncus()
    _running(fake, "sandbox-a", "sandbox-b")

    rc = awscreds.cmd_aws_refresh(fake, {"guest_user": "agent"}, None)

    assert rc == 1
    assert _pushed_instances(fake) == {"sandbox-b"}


def test_refresh_failed_export_is_not_retried_for_next_instance(refresh_env, monkeypatch):
    calls: list[str] = []

    def export(p):
        calls.append(p)
        return _proc(rc=1, stderr="nope")

    monkeypatch.setattr(awscreds.host, "aws_export_credentials", export)
    fake = FakeIncus()
    _running(fake, "sandbox-a", "sandbox-b")

    assert awscreds.cmd_aws_refresh(fake, {"guest_user": "agent"}, None) == 1
    assert calls == ["shared"]


def test_refresh_skips_stopped_and_absent_instances(refresh_env):
    fake = FakeIncus()
    _running(fake, stopped=("sandbox-a",))
    fake.stub("info", "sandbox-b", rc=1)

    rc = awscreds.cmd_aws_refresh(fake, {"guest_user": "agent"}, None)

    assert rc == 0
    assert not any(c[:2] == ["file", "push"] for c in fake.calls)


def test_refresh_with_group_pushes_only_that_group(refresh_env):
    exports, _ = refresh_env
    fake = FakeIncus()
    _running(fake, "sandbox-a", "sandbox-b")

    rc = awscreds.cmd_aws_refresh(fake, {"guest_user": "agent"}, "a")

    assert rc == 0
    assert _pushed_instances(fake) == {"sandbox-a"}
    assert exports == ["shared"]


def test_refresh_all_warns_and_continues_when_a_manifest_fails_to_load(refresh_env, monkeypatch, capsys):
    _, manifests = refresh_env

    def load(group, cfg):
        if group == "a":
            raise AsbxError("broken manifest a")
        return manifests[group]

    monkeypatch.setattr(awscreds.manifest, "load_manifest", load)
    fake = FakeIncus()
    _running(fake, "sandbox-a", "sandbox-b")

    rc = awscreds.cmd_aws_refresh(fake, {"guest_user": "agent"}, None)

    captured = capsys.readouterr()
    assert "broken manifest a" in captured.out + captured.err
    assert rc == 0
    assert _pushed_instances(fake) == {"sandbox-b"}


def test_export_without_aws_cli_raises_asbx_error(monkeypatch):
    monkeypatch.setattr(awscreds.host.shutil, "which", lambda name: None)

    with pytest.raises(AsbxError, match="aws CLI not found"):
        awscreds.host.aws_export_credentials("p")


def test_refresh_with_group_rejects_manifest_without_aws_creds(refresh_env):
    _, manifests = refresh_env
    manifests["a"] = _manifest("sandbox-a", ["p"], auth=["github-gh"])

    with pytest.raises(AsbxError, match="does not use the aws-creds"):
        awscreds.cmd_aws_refresh(FakeIncus(), {"guest_user": "agent"}, "a")


def test_refresh_all_ignores_manifests_without_aws_creds(refresh_env):
    _, manifests = refresh_env
    manifests["a"] = _manifest("sandbox-a", ["p"], auth=["github-gh"])
    fake = FakeIncus()
    _running(fake, "sandbox-a", "sandbox-b")

    awscreds.cmd_aws_refresh(fake, {"guest_user": "agent"}, None)

    assert _pushed_instances(fake) == {"sandbox-b"}


# ---------------------------------------------------------------------------
# credential_status
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("delta, expected", [
    (timedelta(minutes=60), ("PASS", "expires in 60 min")),
    (timedelta(minutes=5), ("WARN", "expires in 5 min")),
    (timedelta(minutes=-1), ("FAIL", "expired")),
])
def test_credential_status_by_remaining_time(delta, expected):
    body = _exported((NOW + delta).isoformat())

    assert awscreds.credential_status(body, NOW) == expected


def test_credential_status_unreadable():
    assert awscreds.credential_status("{}", NOW) == ("FAIL", "unreadable")
    assert awscreds.credential_status("garbage", NOW) == ("FAIL", "unreadable")


# ---------------------------------------------------------------------------
# install_timer
# ---------------------------------------------------------------------------

def test_install_timer_writes_units_and_enables_timer(tmp_path, monkeypatch):
    recorded: list[tuple] = []
    monkeypatch.setattr(awscreds.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(awscreds.host, "which", lambda n: True)
    monkeypatch.setattr(awscreds.host, "systemctl_user",
                        lambda *a: recorded.append(a) or _proc())
    monkeypatch.setenv("ASBX_CONFIG_DIR", "/cfg/dir")

    awscreds.install_timer()

    units = tmp_path / ".config/systemd/user"
    service = (units / "asbx-aws-refresh.service").read_text()
    timer = (units / "asbx-aws-refresh.timer").read_text()
    assert f"ExecStart=" in service and str(hostsetup.ENTRY_POINT) in service
    assert " aws-refresh\n" in service
    assert "Environment=ASBX_CONFIG_DIR=/cfg/dir" in service
    assert "OnUnitActiveSec=10min" in timer
    assert recorded == [("daemon-reload",), ("enable", "--now", "asbx-aws-refresh.timer")]


def test_install_timer_requires_aws_cli(monkeypatch):
    monkeypatch.setattr(awscreds.host, "which", lambda n: False)

    with pytest.raises(AsbxError, match="aws CLI not found"):
        awscreds.install_timer()


def test_install_timer_surfaces_systemctl_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(awscreds.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(awscreds.host, "which", lambda n: True)
    monkeypatch.setattr(awscreds.host, "systemctl_user", lambda *a: _proc(rc=1, stderr="no bus"))

    with pytest.raises(AsbxError, match="no bus"):
        awscreds.install_timer()
