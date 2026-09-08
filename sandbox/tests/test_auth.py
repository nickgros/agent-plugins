from __future__ import annotations

import pytest

from asbxlib import auth
from asbxlib.errors import AsbxError
from fake_incus import FakeIncus

INSTANCE = "sandbox-demo"


def _cfg() -> dict:
    return {
        "guest_user": "agent",
        "aws": {"profile": "x", "config_file": "~/.aws/config"},
    }


def _joined(fake: FakeIncus) -> str:
    return "\n".join(" ".join(c) for c in fake.calls)


def test_run_auth_skips_github_gh_when_already_authenticated():
    fake = FakeIncus()
    fake.stub("gh", "auth", "status", rc=0)

    auth.run_auth(fake, INSTANCE, ["github-gh"], _cfg())

    assert "gh auth login" not in _joined(fake)


def test_run_auth_unknown_provider_name_raises():
    fake = FakeIncus()

    with pytest.raises(AsbxError, match="nope"):
        auth.run_auth(fake, INSTANCE, ["nope"], _cfg())


def test_dict_provider_check_passing_skips_command():
    fake = FakeIncus()
    fake.stub("bash", "-lc", "true", rc=0)
    provider = {"check": "true", "command": "do-the-thing"}

    auth.run_auth(fake, INSTANCE, [provider], _cfg())

    assert "do-the-thing" not in _joined(fake)


def test_dict_provider_check_failing_runs_command():
    fake = FakeIncus()
    fake.stub("bash", "-lc", "true", rc=1)
    provider = {"check": "true", "command": "do-the-thing"}

    auth.run_auth(fake, INSTANCE, [provider], _cfg())

    joined = _joined(fake)
    assert "do-the-thing" in joined
    assert "bash -lc" in joined


def test_github_gh_tolerates_key_already_in_use_and_continues():
    fake = FakeIncus()
    fake.stub("gh", "auth", "status", rc=1)
    fake.stub("ssh-key", "add", rc=1, stderr="key is already in use")

    auth.run_auth(fake, INSTANCE, ["github-gh"], _cfg())

    assert "gpg.format" in _joined(fake)
