from __future__ import annotations

import pytest

from asbxlib import auth, awscreds
from asbxlib.errors import AsbxError
from asbxlib.manifest import Manifest
from fake_incus import FakeIncus

INSTANCE = "sandbox-demo"


def _cfg() -> dict:
    return {
        "guest_user": "agent",
        "aws": {"profiles": ["p"]},
    }


def _manifest(auth=(), aws_profiles=()) -> Manifest:
    return Manifest(path="/demo.yaml", name="demo", instance=INSTANCE,
                    auth=list(auth), aws_profiles=list(aws_profiles))


def _joined(fake: FakeIncus) -> str:
    return "\n".join(" ".join(c) for c in fake.calls)


def test_run_auth_skips_github_gh_when_already_authenticated():
    fake = FakeIncus()
    fake.stub("gh", "auth", "status", rc=0)

    auth.run_auth(fake, _manifest(auth=["github-gh"]), _cfg())

    assert "gh auth login" not in _joined(fake)


def test_run_auth_unknown_provider_name_raises():
    fake = FakeIncus()

    with pytest.raises(AsbxError, match="nope"):
        auth.run_auth(fake, _manifest(auth=["nope"]), _cfg())


def test_dict_provider_check_passing_skips_command():
    fake = FakeIncus()
    fake.stub("bash", "-lc", "true", rc=0)
    provider = {"check": "true", "command": "do-the-thing"}

    auth.run_auth(fake, _manifest(auth=[provider]), _cfg())

    assert "do-the-thing" not in _joined(fake)


def test_dict_provider_check_failing_runs_command():
    fake = FakeIncus()
    fake.stub("bash", "-lc", "true", rc=1)
    provider = {"check": "true", "command": "do-the-thing"}

    auth.run_auth(fake, _manifest(auth=[provider]), _cfg())

    joined = _joined(fake)
    assert "do-the-thing" in joined
    assert "bash -lc" in joined


def test_github_gh_tolerates_key_already_in_use_and_continues():
    fake = FakeIncus()
    fake.stub("gh", "auth", "status", rc=1)
    fake.stub("ssh-key", "add", rc=1, stderr="key is already in use")

    auth.run_auth(fake, _manifest(auth=["github-gh"]), _cfg())

    assert "gpg.format" in _joined(fake)


def test_github_gh_login_requests_signing_key_scope():
    fake = FakeIncus()
    fake.stub("gh", "auth", "status", rc=1)

    auth.run_auth(fake, _manifest(auth=["github-gh"]), _cfg())

    assert "gh auth login --hostname github.com --git-protocol https --scopes admin:ssh_signing_key" in _joined(fake)


def test_github_gh_refreshes_scope_when_authenticated_without_it():
    fake = FakeIncus()
    fake.stub("gh", "auth", "status", rc=0, stdout="- Token scopes: 'admin:public_key', 'repo'")

    auth.run_auth(fake, _manifest(auth=["github-gh"]), _cfg())

    joined = _joined(fake)
    assert "gh auth refresh" in joined and "admin:ssh_signing_key" in joined
    assert "gh auth login" not in joined
    assert "gpg.format" in joined


def test_github_gh_skips_when_authenticated_with_signing_scope():
    fake = FakeIncus()
    fake.stub("gh", "auth", "status", rc=0, stdout="- Token scopes: 'admin:ssh_signing_key', 'repo'")

    auth.run_auth(fake, _manifest(auth=["github-gh"]), _cfg())

    assert "gh auth refresh" not in _joined(fake)
    assert "ssh-key" not in _joined(fake)


def test_github_gh_failed_key_upload_surfaces_gh_error():
    fake = FakeIncus()
    fake.stub("gh", "auth", "status", rc=1)
    fake.stub("ssh-key", "add", rc=1, stderr="needs admin:ssh_signing_key scope")

    with pytest.raises(AsbxError, match="admin:ssh_signing_key scope"):
        auth.run_auth(fake, _manifest(auth=["github-gh"]), _cfg())


def test_aws_creds_wipes_guest_aws_dir_before_pushing(monkeypatch):
    monkeypatch.setattr(awscreds.host, "aws_export_credentials",
                        lambda p: __import__("subprocess").CompletedProcess(
                            [], 0, stdout='{"Version":1,"AccessKeyId":"A","SecretAccessKey":"S",'
                                         '"SessionToken":"T","Expiration":"2030-01-01T00:00:00+00:00"}', stderr=""))
    monkeypatch.setattr(awscreds.host, "aws_profile_region", lambda p: "us-east-1")
    fake = FakeIncus()

    auth.run_auth(fake, _manifest(auth=["aws-creds"], aws_profiles=["p"]), _cfg())

    calls = [" ".join(c) for c in fake.calls]
    rm_i = next(i for i, c in enumerate(calls) if "rm -rf /home/agent/.aws" in c)
    push_i = next(i for i, c in enumerate(calls) if c.startswith("file push"))
    assert rm_i < push_i
    assert not any("sso login" in c for c in calls)


def test_aws_creds_without_profiles_raises_and_touches_nothing():
    fake = FakeIncus()

    with pytest.raises(AsbxError, match="no profiles"):
        auth.run_auth(fake, _manifest(auth=["aws-creds"]), _cfg())

    assert not any("rm -rf" in " ".join(c) for c in fake.calls)


def test_aws_creds_failed_export_pushes_nothing_and_keeps_existing_credentials(monkeypatch):
    monkeypatch.setattr(awscreds.host, "aws_export_credentials",
                        lambda p: __import__("subprocess").CompletedProcess([], 255, stdout="", stderr="expired"))
    fake = FakeIncus()

    with pytest.raises(AsbxError, match="aws sso login"):
        auth.run_auth(fake, _manifest(auth=["aws-creds"], aws_profiles=["p"]), _cfg())

    assert not any(c[:2] == ["file", "push"] for c in fake.calls)
    # the guest's existing credentials stay intact
    assert not any("rm -rf" in " ".join(c) for c in fake.calls)
