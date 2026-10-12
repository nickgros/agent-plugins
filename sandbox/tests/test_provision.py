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

    joined = [" ".join(c) for c in fake.calls]
    assert not any("git clone" in c for c in joined)
    assert not any("should-not-run" in c for c in joined)
    assert any("remote add upstream" in c and "someorg" in c for c in joined)


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


def _calls(fake):
    return [" ".join(c) for c in fake.calls]


def test_repo_setup_runs_after_mise_install_in_the_repo(tmp_path):
    repo = Repo(url="https://github.com/x/y.git", dir="y", ref=None, setup="make", remotes={})
    fake = FakeIncus()
    fake.stub("test", "-d", rc=1)

    provision.run_provision(fake, "sandbox-demo", _manifest(tmp_path, repos=[repo]), "agent")

    joined = _calls(fake)
    install = next(i for i, c in enumerate(joined) if "cd /workspace/y" in c and "mise install --yes" in c)
    setup = next(i for i, c in enumerate(joined) if "cd /workspace/y" in c and "bash -lc make" in c)
    assert install < setup


def test_mise_config_is_written_before_any_mise_install(tmp_path):
    repo = Repo(url="https://github.com/x/y.git", dir="y", ref=None, setup=None, remotes={})
    fake = FakeIncus()

    provision.run_provision(fake, "sandbox-demo", _manifest(tmp_path, repos=[repo]), "agent")

    joined = _calls(fake)
    push = next(i for i, c in enumerate(joined) if c.startswith("file push") and "/etc/mise/config.toml" in c)
    first_install = next(i for i, c in enumerate(joined) if "mise install --yes" in c)
    assert push < first_install


def test_workspace_mise_install_runs_only_when_there_are_no_repos(tmp_path):
    fake = FakeIncus()
    provision.run_provision(fake, "sandbox-demo", _manifest(tmp_path), "agent")
    assert sum("cd /workspace &&" in c and "mise install --yes" in c for c in _calls(fake)) == 1

    repo = Repo(url="https://github.com/x/y.git", dir="y", ref=None, setup=None, remotes={})
    fake = FakeIncus()
    provision.run_provision(fake, "sandbox-demo", _manifest(tmp_path, repos=[repo]), "agent")
    assert not any("cd /workspace &&" in c and "mise install --yes" in c for c in _calls(fake))


def test_mise_install_failure_in_a_repo_names_the_repo_and_carries_stderr(tmp_path):
    repo = Repo(url="https://github.com/x/y.git", dir="y", ref=None, setup=None, remotes={})
    fake = FakeIncus()
    fake.stub("mise", "install", rc=1, stderr="boom")

    with pytest.raises(AsbxError, match="repo y: mise install exited 1\nboom"):
        provision.run_provision(fake, "sandbox-demo", _manifest(tmp_path, repos=[repo]), "agent")


def test_mise_install_failure_without_repos_is_labelled_tools(tmp_path):
    fake = FakeIncus()
    fake.stub("mise", "install", rc=1, stderr="boom")

    with pytest.raises(AsbxError, match="tools: mise install exited 1"):
        provision.run_provision(fake, "sandbox-demo", _manifest(tmp_path), "agent")


def test_mise_install_receives_the_manifest_env(tmp_path):
    fake = FakeIncus()

    provision.run_provision(fake, "sandbox-demo", _manifest(tmp_path, env={"GITHUB_TOKEN": "t0k"}), "agent")

    install = next(c for c in _calls(fake) if "mise install --yes" in c)
    assert "GITHUB_TOKEN=t0k" in install


def test_setup_hook_runs_as_a_login_shell(tmp_path):
    hook = tmp_path / "hook.sh"
    hook.write_text("true\n")
    fake = FakeIncus()

    provision.run_provision(fake, "sandbox-demo", _manifest(tmp_path, setup_path=str(hook)), "agent")

    assert any("bash -l /home/agent/.asbx/provision.sh" in c for c in _calls(fake))
