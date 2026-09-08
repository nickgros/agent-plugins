"""Tests for asbxlib.provision: run_provision and configure_repo_remotes."""
from __future__ import annotations

import pytest
from fake_incus import FakeIncus

from asbxlib import provision
from asbxlib.errors import AsbxError
from asbxlib.manifest import Manifest, Repo, resolve_remote_url


def _manifest(tmp_path=None, **overrides) -> Manifest:
    path = str(tmp_path / "demo.yaml") if tmp_path is not None else "/tmp/x.yaml"
    if tmp_path is not None:
        (tmp_path / "demo.yaml").write_text("repos: []\n")
    defaults = dict(
        path=path,
        name="demo",
        instance="sandbox-demo",
        resources={},
        repos=[],
        mounts=[],
        compose_path=None,
        env={},
        auth=[],
        setup_path=None,
        remotes={},
    )
    defaults.update(overrides)
    return Manifest(**defaults)


def test_repo_already_cloned_is_not_reclonced_but_remotes_still_reconciled(tmp_path):
    repo = Repo(url="https://github.com/x/y.git", dir="y", ref=None,
                setup="echo should-not-run", remotes={"upstream": "someorg"})
    fake = FakeIncus()
    fake.stub("test", "-d", rc=0)          # dir already exists
    fake.stub("remote", "get-url", rc=1)   # remote missing -> add branch

    provision.run_provision(fake, "sandbox-demo", _manifest(tmp_path, repos=[repo]), "agent")


def test_clone_failure_with_authentication_in_stderr_raises_asbxerror_pointing_at_auth_command():
    repo = Repo(url="https://github.com/x/y.git", dir="y", ref=None, setup=None, remotes={})
    fake = FakeIncus()
    fake.stub("test", "-d", rc=1)
    fake.stub("git", "clone", rc=1, stderr="remote: Authentication failed")

    with pytest.raises(AsbxError, match="asbx auth demo"):
        provision.run_provision(fake, "sandbox-demo", _manifest(repos=[repo]), "agent")


def test_configure_repo_remotes_add_seturl_and_noop_cases():
    repo = Repo(url="https://github.com/x/y.git", dir="y", ref=None, setup=None,
                remotes={"upstream": "someorg"})

    # (a) missing remote -> add
    fake_add = FakeIncus()
    fake_add.stub("remote", "get-url", rc=1)
    provision.configure_repo_remotes(fake_add, "sandbox-demo", "agent", "/workspace/y", repo)
    assert any("remote add" in " ".join(c) for c in fake_add.calls)

    # (b) drifted URL -> set-url
    fake_seturl = FakeIncus()
    fake_seturl.stub("remote", "get-url", rc=0, stdout="https://old-url/y.git\n")
    provision.configure_repo_remotes(fake_seturl, "sandbox-demo", "agent", "/workspace/y", repo)
    assert any("remote set-url" in " ".join(c) for c in fake_seturl.calls)

    # (c) matching URL -> neither add nor set-url
    expected_url = resolve_remote_url(repo.url, "someorg")
    fake_noop = FakeIncus()
    fake_noop.stub("remote", "get-url", rc=0, stdout=f"{expected_url}\n")
    provision.configure_repo_remotes(fake_noop, "sandbox-demo", "agent", "/workspace/y", repo)
    assert not any("remote add" in " ".join(c) for c in fake_noop.calls)
    assert not any("remote set-url" in " ".join(c) for c in fake_noop.calls)


def test_compose_path_pushes_file_and_runs_docker_compose_up(tmp_path):
    compose_path = tmp_path / "compose.yml"
    compose_path.write_text("services: {}\n")
    fake = FakeIncus()

    provision.run_provision(fake, "sandbox-demo",
                             _manifest(tmp_path, repos=[], compose_path=str(compose_path)), "agent")

    remote_path = "/home/agent/.asbx/services/compose.yml"
    assert fake.pushed[remote_path] == "services: {}\n"
    assert any("docker compose" in " ".join(c) and "up -d" in " ".join(c) for c in fake.calls)


def test_compose_non_zero_exit_raises(tmp_path):
    compose_path = tmp_path / "compose.yml"
    compose_path.write_text("services: {}\n")
    fake = FakeIncus()
    fake.stub("docker", "compose", rc=1)

    with pytest.raises(AsbxError):
        provision.run_provision(fake, "sandbox-demo",
                                 _manifest(repos=[], compose_path=str(compose_path)), "agent")


def test_run_provision_records_manifest_sha256_on_success(tmp_path):
    manifest_path = tmp_path / "demo.yaml"
    manifest_path.write_text("repos: []\n")
    fake = FakeIncus()
    manifest = _manifest(path=str(manifest_path), repos=[])

    provision.run_provision(fake, "sandbox-demo", manifest, "agent")

    assert ["config", "set", "sandbox-demo", "user.asbx.manifest_sha256",
            provision.manifest_sha256(manifest.path)] in fake.calls
