"""Tests for asbxlib.provision: run_provision and configure_repo_remotes."""
from __future__ import annotations

import subprocess

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


def _guide_repos():
    return [
        Repo(url="https://github.com/me/api.git", dir="api", ref="develop", setup="pnpm install",
             remotes={"upstream": "Org"}),
        Repo(url="https://github.com/me/web.git", dir="web", ref=None, setup=None, remotes={}),
    ]


def test_provision_creates_a_worktree_dir_per_repo(tmp_path):
    fake = FakeIncus()

    provision.run_provision(fake, "sandbox-demo", _manifest(tmp_path, repos=_guide_repos()), "agent")

    mkdir = next(c for c in _calls(fake) if "mkdir -p /workspace" in c)
    assert "/workspace/worktrees/api" in mkdir and "/workspace/worktrees/web" in mkdir


def test_guide_bases_worktrees_on_upstream_when_the_repo_has_it():
    repo = Repo(url="https://github.com/me/api.git", dir="api", ref="develop", setup=None,
                remotes={"upstream": "Org"})

    guide = provision.render_workspace_guide(_manifest(repos=[repo]), {"api": "develop"}, None)

    assert "| `api` | `/workspace/api` | origin, upstream | `upstream/develop` | `/workspace/worktrees/api/` |" in guide


def test_guide_lists_each_repo_and_the_worktree_command(tmp_path):
    fake = FakeIncus()
    fake.stub("git", "-C", "/workspace/web", "symbolic-ref", stdout="origin/main\n")

    provision.run_provision(fake, "sandbox-demo", _manifest(tmp_path, repos=_guide_repos()), "agent")

    guide = fake.pushed[provision.GUIDE_PATH]
    assert guide.startswith(provision.GUIDE_MARKER + "\n")
    assert ("| `api` | `/workspace/api` | origin, upstream | `upstream/develop` | "
            "`/workspace/worktrees/api/` | `pnpm install` |") in guide
    assert "| `web` | `/workspace/web` | origin | `origin/main` | `/workspace/worktrees/web/` | — |" in guide
    assert ("git -C /workspace/<repo> worktree add --no-track -b <branch> "
            "/workspace/worktrees/<repo>/<slug> <base>") in guide
    assert "## Project notes" not in guide


def test_guide_without_a_ref_uses_the_clone_default_branch_on_upstream(tmp_path):
    repo = Repo(url="https://github.com/me/web.git", dir="web", ref=None, setup=None, remotes={"upstream": "Org"})
    fake = FakeIncus()
    fake.stub("symbolic-ref", stdout="origin/trunk\n")

    provision.run_provision(fake, "sandbox-demo", _manifest(tmp_path, repos=[repo]), "agent")

    assert "`upstream/trunk`" in fake.pushed[provision.GUIDE_PATH]


def test_guide_falls_back_to_a_placeholder_and_warns_when_origin_head_is_unset(tmp_path, capsys):
    fake = FakeIncus()
    fake.stub("symbolic-ref", rc=1)

    provision.run_provision(fake, "sandbox-demo", _manifest(tmp_path, repos=_guide_repos()), "agent")

    assert "`origin/<default branch>`" in fake.pushed[provision.GUIDE_PATH]
    assert "warning: repo web: origin/HEAD is not set" in capsys.readouterr().err


def test_guide_appends_the_manifest_notes_file(tmp_path):
    notes = tmp_path / "notes.md"
    notes.write_text("Open PRs against Org.\n\n")
    fake = FakeIncus()

    provision.run_provision(fake, "sandbox-demo", _manifest(tmp_path, guide_path=str(notes)), "agent")

    assert fake.pushed[provision.GUIDE_PATH].endswith("## Project notes\n\nOpen PRs against Org.\n")


def test_guide_is_not_written_and_provision_warns_when_the_guest_says_it_is_not_ours(tmp_path, capsys):
    fake = FakeIncus()
    fake.stub("guide_is_generated", rc=1)

    provision.run_provision(fake, "sandbox-demo", _manifest(tmp_path, repos=_guide_repos()), "agent")

    assert provision.GUIDE_PATH not in fake.pushed
    assert "warning: /workspace/AGENTS.md was not written by asbx" in capsys.readouterr().err


def _guide_writable(path) -> bool:
    return subprocess.run(["bash", "-c", provision.GUIDE_WRITABLE_SCRIPT, "_", str(path),
                           provision.GUIDE_MARKER]).returncode == 0


def test_guide_writable_script_accepts_a_missing_or_generated_file(tmp_path):
    guide = tmp_path / "AGENTS.md"
    assert _guide_writable(guide)
    guide.write_text(provision.GUIDE_MARKER + "\n# old\n")
    assert _guide_writable(guide)


@pytest.mark.parametrize("body", ["# mine\n" + provision.GUIDE_MARKER + "\n", provision.GUIDE_MARKER + " \n", ""])
def test_guide_writable_script_refuses_a_file_asbx_did_not_write(tmp_path, body):
    guide = tmp_path / "AGENTS.md"
    guide.write_text(body)
    assert not _guide_writable(guide)


def test_guide_writable_script_refuses_a_dangling_symlink(tmp_path):
    guide = tmp_path / "AGENTS.md"
    guide.symlink_to(tmp_path / "elsewhere")
    assert not _guide_writable(guide)


def test_guide_escapes_pipes_in_table_cells():
    repo = Repo(url="https://github.com/me/api.git", dir="api", ref="main", setup="make || true", remotes={})

    guide = provision.render_workspace_guide(_manifest(repos=[repo]), {"api": "main"}, None)

    assert "`make \\|\\| true`" in guide
