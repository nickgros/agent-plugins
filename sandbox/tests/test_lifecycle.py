"""Tests for asbxlib.lifecycle. No Incus required."""
from __future__ import annotations

import pytest

from asbxlib import lifecycle
from asbxlib import manifest
from asbxlib.errors import AsbxError

from fake_incus import FakeIncus

# ---------------------------------------------------------------------------
# classify_repo
# ---------------------------------------------------------------------------

REMOTE = "origin\tgit@example.com:x/y.git (fetch)"


def test_classify_repo_no_remote_is_blocking():
    msg = lifecycle.classify_repo("myrepo", "", "", "")
    assert msg is not None
    assert "no remote configured" in msg


def test_classify_repo_clean_and_pushed_is_not_blocking():
    assert lifecycle.classify_repo("myrepo", "", REMOTE, "") is None


def test_classify_repo_dirty_is_blocking():
    msg = lifecycle.classify_repo("myrepo", " M some/file.py", REMOTE, "")
    assert msg is not None
    assert "myrepo" in msg
    assert "dirty" in msg


def test_classify_repo_unpushed_commits_are_blocking():
    msg = lifecycle.classify_repo("myrepo", "", REMOTE, "abc1234 add thing")
    assert msg is not None
    assert "unpushed" in msg


# ---------------------------------------------------------------------------
# cmd_rm / cmd_rebuild / cmd_restore orchestration (via FakeIncus)
# ---------------------------------------------------------------------------

def _fake_manifest(instance: str = "sandbox-demo") -> manifest.Manifest:
    return manifest.Manifest(
        path="/tmp/x.yaml", name="demo", instance=instance, resources={},
        repos=[], mounts=[], compose_path=None, env={}, auth=[],
        setup_path=None, remotes={},
    )


KNOWN_HOSTS_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"


def test_cmd_rm_with_yes_deletes_instance_and_removes_its_ssh_config_and_host_key(monkeypatch, cfg, tmp_path):
    fake_manifest = _fake_manifest()
    monkeypatch.setattr(lifecycle.manifest_mod, "load_manifest", lambda group, cfg: fake_manifest)

    cfg["ssh"]["config_dir"] = str(tmp_path / "ssh-confs")
    known_hosts = tmp_path / "known_hosts"
    cfg["ssh"]["known_hosts"] = str(known_hosts)
    known_hosts.write_text(f"sandbox-demo.incus {KNOWN_HOSTS_KEY}\nsandbox-other.incus {KNOWN_HOSTS_KEY}\n")

    conf_dir = tmp_path / "ssh-confs"
    conf_dir.mkdir(parents=True, exist_ok=True)
    conf_path = conf_dir / "sandbox-demo.conf"
    conf_path.write_text("stub\n")

    fake = FakeIncus()
    fake.set_instance(exists=True)

    lifecycle.cmd_rm(fake, cfg, "demo", yes=True)

    assert ["delete", "sandbox-demo", "--force"] in fake.calls
    assert not conf_path.exists()
    remaining = known_hosts.read_text()
    assert "sandbox-demo.incus" not in remaining
    assert "sandbox-other.incus" in remaining


def test_cmd_rebuild_refuses_when_workspace_repo_is_dirty_and_force_is_false(monkeypatch, cfg):
    fake_manifest = _fake_manifest()
    monkeypatch.setattr(lifecycle.manifest_mod, "load_manifest", lambda group, cfg: fake_manifest)

    fake = FakeIncus()
    fake.set_instance(exists=True)
    fake.stub("ls", "-1", stdout="myrepo\n")
    fake.stub("find", "/workspace", stdout="/workspace/myrepo\n")
    fake.stub("git", "-C", "status", "--porcelain", stdout=" M dirty.py\n")
    # Registered before the "remote" stub: "--remotes" (part of the unpushed-log
    # command) contains "remote" as a substring, so without this ordering the
    # remote stub would shadow the log call too.
    fake.stub("git", "-C", "log", stdout="")
    fake.stub("git", "-C", "remote", stdout=REMOTE + "\n")

    with pytest.raises(AsbxError, match="unsafe repos"):
        lifecycle.cmd_rebuild(fake, cfg, "demo", force=False, yes=True)

    assert not any("delete" in " ".join(c) for c in fake.calls)


def test_cmd_restore_with_unknown_label_raises_before_any_stop_or_delete(monkeypatch, cfg):
    fake_manifest = _fake_manifest()
    monkeypatch.setattr(lifecycle.manifest_mod, "load_manifest", lambda group, cfg: fake_manifest)

    fake = FakeIncus()
    fake.set_snapshots([])
    fake.stub("info", rc=1)  # copy-name existence check: no fallback copy either

    with pytest.raises(AsbxError, match="no snapshot or copy"):
        lifecycle.cmd_restore(fake, cfg, "demo", "nonexistent-label", yes=True)

    assert not any("stop" in " ".join(c) for c in fake.calls)
    assert not any("delete" in " ".join(c) for c in fake.calls)


def test_cmd_restore_from_snapshot_restarts_the_instance_and_rewrites_its_ssh_config(monkeypatch, cfg, tmp_path):
    fake_manifest = _fake_manifest()
    monkeypatch.setattr(lifecycle.manifest_mod, "load_manifest", lambda group, cfg: fake_manifest)
    monkeypatch.setattr(lifecycle.guest, "wait_for_agent", lambda *a, **k: None)
    cfg["ssh"]["config_dir"] = str(tmp_path / "confs")
    cfg["ssh"]["known_hosts"] = str(tmp_path / "known_hosts")

    fake = FakeIncus()
    fake.set_snapshots(["pre1"])

    lifecycle.cmd_restore(fake, cfg, "demo", "pre1", yes=True)

    assert ["start", "sandbox-demo"] in fake.calls
    assert "HostName sandbox-demo.incus" in (tmp_path / "confs" / "sandbox-demo.conf").read_text()


def test_cmd_snapshot_refuses_a_label_that_already_exists(monkeypatch, cfg, config_root):
    (config_root / "demo.yaml").write_text("")
    fake = FakeIncus()
    fake.set_snapshots(["pre1"])

    with pytest.raises(AsbxError, match="already exists"):
        lifecycle.cmd_snapshot(fake, cfg, "demo", "pre1")

    assert not any(c[:2] == ["snapshot", "create"] or c[:1] == ["copy"] for c in fake.calls)


def _clean_pushed_fake(ls: str, found: str) -> FakeIncus:
    fake = FakeIncus()
    fake.set_instance(exists=True)
    fake.stub("ls", "-1", stdout=ls)
    fake.stub("find", "/workspace", stdout=found)
    fake.stub("git", "-C", "log", stdout="")
    fake.stub("git", "-C", "remote", stdout=REMOTE + "\n")
    return fake


def test_find_unsafe_repos_checks_worktrees_inside_a_worktrees_directory():
    fake = _clean_pushed_fake("r.worktrees\n", "/workspace/r.worktrees/feat\n/workspace/r.worktrees/fix\n")
    fake.stub("git", "-C", "/workspace/r.worktrees/fix", "status", "--porcelain", stdout=" M x\n")

    assert lifecycle.find_unsafe_repos(fake, "sandbox-demo", "demo", "agent") == [
        "r.worktrees/fix: dirty (uncommitted changes)"]


def test_find_unsafe_repos_flags_a_directory_that_is_not_a_repo():
    fake = _clean_pushed_fake("scratch\nmyrepo\n", "/workspace/myrepo\n")

    assert lifecycle.find_unsafe_repos(fake, "sandbox-demo", "demo", "agent") == [
        "scratch: not a git repository — contents cannot be recovered"]


def test_cmd_rebuild_refuses_when_workspace_cannot_be_listed(monkeypatch, cfg):
    monkeypatch.setattr(lifecycle.manifest_mod, "load_manifest", lambda group, cfg: _fake_manifest())
    fake = FakeIncus()
    fake.set_instance(exists=True)
    fake.stub("ls", "-1", rc=2)

    with pytest.raises(AsbxError, match="cannot list /workspace"):
        lifecycle.cmd_rebuild(fake, cfg, "demo", force=False, yes=True)

    assert not any("delete" in " ".join(c) for c in fake.calls)
