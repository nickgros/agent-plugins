"""Tests for asbxlib.harnesssync. The fake host home is a tmp dir; incus is never run."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from asbxlib import harnesssync, manifest
from asbxlib.errors import AsbxError
from fake_incus import FakeIncus

AGENT = "/home/agent/.omp/agent"


@pytest.fixture
def host_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    (home / ".omp" / "agent").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    return home


def _write(path: Path, body: str = "x\n", mode: int = 0o644) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(mode)
    return path


def _manifest(instance: str = "sandbox-a", credentials: list | None = None) -> manifest.Manifest:
    return manifest.Manifest(path="/x.yaml", name=instance, instance=instance,
                             harness_credentials=credentials or [])


def _populate(home: Path) -> None:
    agent = home / ".omp" / "agent"
    _write(agent / "config.yml")
    _write(agent / "mcp.json", '{"k": "secret"}')
    _write(agent / ".env", "TOKEN=1\n")
    _write(agent / "agent.db", "db")
    _write(agent / "hooks" / "x.sh", "#!/bin/sh\n", 0o755)


def _modes(files):
    return {f.guest: f.mode for f in files}


def _scripts(fake: FakeIncus, command: str) -> list[str]:
    """The `bash -c` scripts of non-root execs that run `command`."""
    return [c[-1] for c in fake.calls if c[0] == "exec" and f"&& {command} " in c[-1]]


def _rm_scripts(fake: FakeIncus) -> list[str]:
    return _scripts(fake, "rm -f")


# ---------------------------------------------------------------------------
# collect / files_for
# ---------------------------------------------------------------------------

def test_default_sync_excludes_credentials_and_runtime_state(host_home, cfg):
    _populate(host_home)

    files = harnesssync.files_for(cfg, _manifest())

    assert _modes(files) == {f"{AGENT}/config.yml": "0644", f"{AGENT}/hooks/x.sh": "0755"}


def test_credential_opt_in_includes_file_as_0600(host_home, cfg):
    _populate(host_home)

    files = harnesssync.files_for(cfg, _manifest(credentials=["omp-mcp"]))

    assert _modes(files)[f"{AGENT}/mcp.json"] == "0600"
    assert f"{AGENT}/.env" not in _modes(files)


def test_credential_copy_of_same_path_wins(host_home, cfg):
    _populate(host_home)
    entry = {"host": "~/.omp/agent/config.yml", "guest": "~/.omp/agent/config.yml"}

    files = harnesssync.files_for(cfg, _manifest(credentials=[entry]))

    assert _modes(files)[f"{AGENT}/config.yml"] == "0600"


def test_directory_walk_skips_git_node_modules_and_broken_symlinks(host_home, cfg):
    agent = host_home / ".omp" / "agent"
    _write(agent / "skills" / "a" / "SKILL.md")
    _write(agent / "skills" / "a" / ".git" / "HEAD")
    _write(agent / "skills" / "a" / "node_modules" / "m" / "i.js")
    os.symlink(agent / "nowhere", agent / "skills" / "a" / "dangling")
    _write(agent / "skills" / "real.md")
    os.symlink(agent / "skills" / "real.md", agent / "skills" / "a" / "linked.md")

    files = harnesssync.files_for(cfg, _manifest())

    assert sorted(_modes(files)) == [
        f"{AGENT}/skills/a/SKILL.md", f"{AGENT}/skills/a/linked.md", f"{AGENT}/skills/real.md"]


def test_missing_inline_root_warns_and_missing_builtin_is_silent(host_home, cfg, capsys):
    cfg["harness_settings"]["sync"] = [{"host": "~/nope", "guest": "~/nope"}, "omp"]

    assert harnesssync.files_for(cfg, _manifest()) == []

    err = capsys.readouterr()
    assert "nope not found on host" in err.out + err.err
    assert ".omp/agent" not in err.out + err.err


def test_credential_name_with_no_files_warns(host_home, cfg, capsys):
    harnesssync.files_for(cfg, _manifest(credentials=["omp-secrets"]))

    err = capsys.readouterr()
    assert "omp-secrets: no files on host" in err.out + err.err


def test_directory_walk_skips_noise_dirs_at_any_depth(host_home, cfg):
    agent = host_home / ".omp" / "agent"
    _write(agent / "extensions" / "e" / "index.ts")
    _write(agent / "extensions" / "e" / "lib" / "__pycache__" / "m.pyc")
    _write(agent / "extensions" / "e" / ".venv" / "bin" / "python")

    assert sorted(_modes(harnesssync.files_for(cfg, _manifest()))) == [f"{AGENT}/extensions/e/index.ts"]


def test_symlink_inside_root_is_followed_for_files_and_directories(host_home, cfg):
    agent = host_home / ".omp" / "agent"
    _write(agent / "skills" / "real" / "SKILL.md")
    os.symlink(agent / "skills" / "real", agent / "skills" / "alias")
    os.symlink(agent / "skills" / "real" / "SKILL.md", agent / "skills" / "file-alias.md")

    assert sorted(_modes(harnesssync.files_for(cfg, _manifest()))) == [
        f"{AGENT}/skills/alias/SKILL.md", f"{AGENT}/skills/file-alias.md", f"{AGENT}/skills/real/SKILL.md"]


def test_symlink_pointing_outside_root_is_skipped_with_a_warning(host_home, cfg, capsys):
    agent = host_home / ".omp" / "agent"
    secret = _write(host_home / ".ssh" / "id_ed25519", "PRIVATE KEY\n", 0o600)
    _write(host_home / "elsewhere" / "doc.md")
    _write(agent / "skills" / "ok.md")
    os.symlink(secret, agent / "skills" / "key")
    os.symlink(host_home / "elsewhere", agent / "skills" / "dir")

    files = harnesssync.files_for(cfg, _manifest())

    assert sorted(_modes(files)) == [f"{AGENT}/skills/ok.md"]
    out = capsys.readouterr()
    assert "skills/key: symlink points outside" in out.out + out.err
    assert "skills/dir: symlink points outside" in out.out + out.err


def test_symlink_loop_inside_root_terminates(host_home, cfg):
    agent = host_home / ".omp" / "agent"
    _write(agent / "skills" / "a" / "x.md")
    os.symlink(agent / "skills", agent / "skills" / "a" / "back")

    assert f"{AGENT}/skills/a/x.md" in _modes(harnesssync.files_for(cfg, _manifest()))


def test_inline_entry_maps_a_directory_under_the_guest_path(host_home, cfg):
    _write(host_home / "notes" / "sub" / "a.md")
    entry = {"host": "~/notes", "guest": "~/docs/notes"}

    files = harnesssync.collect([entry], "agent", credential=False)

    assert [f.guest for f in files] == ["/home/agent/docs/notes/sub/a.md"]


# ---------------------------------------------------------------------------
# rewrite_home / push
# ---------------------------------------------------------------------------

def _mv_scripts(fake: FakeIncus) -> list[str]:
    return [c[-1] for c in fake.calls if c[0] == "exec" and "mv -f" in c[-1]]


def _push(fake: FakeIncus, cfg, credentials=None) -> int:
    return harnesssync.push(fake, "sandbox-a", "agent",
                            harnesssync.files_for(cfg, _manifest(credentials=credentials)))


def test_rewrite_home_leaves_longer_sibling_paths_alone():
    out = harnesssync.rewrite_home("/h/me/proj /h/mex /h/me.bak /h/me", "/h/me", "/home/agent")

    assert out == "/home/agent/proj /h/mex /h/me.bak /home/agent"


def test_push_rewrites_host_home_in_text_bodies(host_home, cfg):
    _write(host_home / ".omp" / "agent" / "config.yml", f"a: {host_home}/proj\nb: {host_home}x\n")
    fake = FakeIncus()

    _push(fake, cfg)

    assert fake.pushed[f"{AGENT}/config.yml.asbx-tmp"] == "a: /home/agent/proj\nb: " + f"{host_home}x\n"


def test_push_renames_into_place_and_writes_state(host_home, cfg):
    _populate(host_home)
    fake = FakeIncus()

    assert _push(fake, cfg) == 2

    renames = _mv_scripts(fake)
    assert len(renames) == 1
    assert f"{AGENT}/config.yml.asbx-tmp" in renames[0] and f"{AGENT}/hooks/x.sh" in renames[0]
    state = json.loads(fake.pushed["/home/agent/.asbx/harness-settings.json"])
    assert state == {"version": 1, "paths": [f"{AGENT}/config.yml", f"{AGENT}/hooks/x.sh"]}


def test_push_binary_file_goes_through_file_push_unmodified(host_home, cfg, monkeypatch):
    blob = host_home / ".omp" / "agent" / "themes" / "t.bin"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"\xff\xfe\x00binary")
    fake = FakeIncus()
    pushed: list[tuple[str, str]] = []
    monkeypatch.setattr(fake, "file_push", lambda inst, local, remote, **kw: pushed.append((local, remote)))

    _push(fake, cfg)

    assert [p for p in pushed if p[1].endswith(".asbx-tmp")] == [(str(blob), f"{AGENT}/themes/t.bin.asbx-tmp")]


def _stub_guest_copy(fake: FakeIncus, path: str, body: str, mode: str) -> None:
    fake.stub("sha256sum", path, stdout=f"{hashlib.sha256(body.encode()).hexdigest()}  {path}\n")
    fake.stub("stat", "-c", path, stdout=f"{mode} {path}\n")


def test_push_skips_files_the_guest_already_has(host_home, cfg):
    _write(host_home / ".omp" / "agent" / "config.yml", "a: 1\n")
    fake = FakeIncus()
    _stub_guest_copy(fake, f"{AGENT}/config.yml", "a: 1\n", "644")

    assert _push(fake, cfg) == 0

    assert not any(p.endswith(".asbx-tmp") for p in fake.pushed)
    assert _mv_scripts(fake) == []


@pytest.mark.parametrize("body,mode", [("edited in guest\n", "644"), ("a: 1\n", "600")])
def test_push_overwrites_a_guest_copy_whose_content_or_mode_differs(host_home, cfg, body, mode):
    _write(host_home / ".omp" / "agent" / "config.yml", "a: 1\n")
    fake = FakeIncus()
    _stub_guest_copy(fake, f"{AGENT}/config.yml", body, mode)

    assert _push(fake, cfg) == 1

    assert fake.pushed[f"{AGENT}/config.yml.asbx-tmp"] == "a: 1\n"


def test_push_batches_execs_so_no_argument_outgrows_the_limit(host_home, cfg):
    skills = host_home / ".omp" / "agent" / "skills"
    for i in range(1200):
        _write(skills / f"skill-{i:04d}-{'x' * 60}" / "SKILL.md", f"{i}\n")
    fake = FakeIncus()

    assert _push(fake, cfg) == 1200

    assert max(len(a) for c in fake.calls for a in c) < 60_000
    scripts = _mv_scripts(fake)
    assert len(scripts) > 1
    assert sum(s.count("mv -f") for s in scripts) == 1200


def test_push_prunes_stale_paths_but_only_under_guest_home(host_home, cfg):
    _populate(host_home)
    fake = FakeIncus()
    fake.stub("cat", ".asbx/harness-settings.json",
              stdout=f'{{"version":1,"paths":["{AGENT}/old.md","/etc/passwd","/home/agent/../../etc/x"]}}')

    _push(fake, cfg)

    rm = _rm_scripts(fake)
    assert len(rm) == 1 and f"{AGENT}/old.md" in rm[0]
    assert not any("/etc/" in "\n".join(c) for c in fake.calls)


def test_push_removes_credential_file_once_opt_in_is_dropped(host_home, cfg):
    _populate(host_home)
    fake = FakeIncus()
    fake.stub("cat", ".asbx/harness-settings.json", stdout=f'{{"version":1,"paths":["{AGENT}/mcp.json"]}}')

    _push(fake, cfg)

    assert f"{AGENT}/mcp.json" in _rm_scripts(fake)[0]


@pytest.mark.parametrize("state", [
    "not json", '{"version":1,"paths":"x"}', '{"version":1,"paths":[1]}', '["a"]', '{"version":1}', ""])
def test_push_unusable_state_prunes_nothing(host_home, cfg, state):
    _populate(host_home)
    fake = FakeIncus()
    fake.stub("cat", ".asbx/harness-settings.json", stdout=state)

    _push(fake, cfg)

    assert _rm_scripts(fake) == []
    assert "/home/agent/.asbx/harness-settings.json" in fake.pushed


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_push_unreadable_host_file_raises(host_home, cfg):
    f = _write(host_home / ".omp" / "agent" / "config.yml")
    f.chmod(0)

    with pytest.raises(AsbxError, match="cannot read"):
        _push(FakeIncus(), cfg)


# ---------------------------------------------------------------------------
# cmd_settings_sync
# ---------------------------------------------------------------------------

@pytest.fixture
def sync_env(host_home, config_root, cfg):
    _write(host_home / ".omp" / "agent" / "config.yml")
    for g in ("a", "b"):
        (config_root / f"{g}.yaml").write_text("")
    return cfg


def _states(fake: FakeIncus, running=(), stopped=(), absent=()):
    for inst in running:
        fake.stub("info", inst)
        fake.stub("list", inst, "--format", "json", stdout='[{"status": "Running"}]')
    for inst in stopped:
        fake.stub("info", inst)
        fake.stub("list", inst, "--format", "json", stdout='[{"status": "Stopped"}]')
    for inst in absent:
        fake.stub("info", inst, rc=1)


def _synced(fake: FakeIncus) -> set[str]:
    return {c[3].split("/", 1)[0] for c in fake.calls if c[:2] == ["file", "push"]}


def test_settings_sync_skips_stopped_and_absent_instances(sync_env):
    fake = FakeIncus()
    _states(fake, stopped=("sandbox-a",), absent=("sandbox-b",))

    assert harnesssync.cmd_settings_sync(fake, sync_env, None) == 0
    assert _synced(fake) == set()


def test_settings_sync_without_group_syncs_every_running_manifest(sync_env):
    fake = FakeIncus()
    _states(fake, running=("sandbox-a", "sandbox-b"))

    assert harnesssync.cmd_settings_sync(fake, sync_env, None) == 0
    assert _synced(fake) == {"sandbox-a", "sandbox-b"}


def test_settings_sync_with_group_touches_only_that_group(sync_env):
    fake = FakeIncus()
    _states(fake, running=("sandbox-a", "sandbox-b"))

    assert harnesssync.cmd_settings_sync(fake, sync_env, "b") == 0
    assert _synced(fake) == {"sandbox-b"}


def test_settings_sync_unknown_group_raises(sync_env):
    with pytest.raises(AsbxError, match="no manifest at"):
        harnesssync.cmd_settings_sync(FakeIncus(), sync_env, "nope")


def test_settings_sync_warns_and_continues_when_a_manifest_fails_to_load(sync_env, config_root, capsys):
    (config_root / "a.yaml").write_text("bogus_key: 1\n")
    fake = FakeIncus()
    _states(fake, running=("sandbox-b",))

    assert harnesssync.cmd_settings_sync(fake, sync_env, None) == 0

    assert "bogus_key" in capsys.readouterr().err
    assert _synced(fake) == {"sandbox-b"}


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_settings_sync_failed_target_returns_1_and_others_still_sync(sync_env, host_home, config_root):
    _write(host_home / "secret.txt").chmod(0)  # only manifest a asks for it
    (config_root / "a.yaml").write_text("harness_credentials: [{host: ~/secret.txt}]\n")
    fake = FakeIncus()
    _states(fake, running=("sandbox-a", "sandbox-b"))

    assert harnesssync.cmd_settings_sync(fake, sync_env, None) == 1

    assert _synced(fake) == {"sandbox-b"}
