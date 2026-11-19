"""Tests for asbxlib.incusbackend. FakeIncus scripts every `incus` call."""
from __future__ import annotations

import os
import re

import pytest

from asbxlib import baseimage, incusbackend
from asbxlib.backend import ABSENT, META_GROUP, RUNNING, STOPPED, SandboxSpec
from asbxlib.errors import AsbxError
from asbxlib.incusbackend import IncusBackend, IncusGuest
from asbxlib.manifest import Mount

from fake_incus import FakeIncus


def _spec(mounts=(), ipv4=None) -> SandboxSpec:
    return SandboxSpec(instance="sandbox-demo", group="demo", cpu=2, memory="4GiB", disk="20GiB",
                       mounts=list(mounts), ipv4=ipv4)


# ---------------------------------------------------------------------------
# IncusGuest / state / metadata
# ---------------------------------------------------------------------------

def test_incus_guest_runs_non_root_commands_through_runuser_in_its_instance():
    fake = FakeIncus()

    IncusGuest(fake, "sandbox-demo").exec(["true"], user="agent")

    assert fake.calls[-1][:6] == ["exec", "sandbox-demo", "--", "runuser", "-u", "agent"]


@pytest.mark.parametrize("status, state", [("Running", RUNNING), ("Stopped", STOPPED), ("Frozen", STOPPED)])
def test_state_maps_incus_status_onto_the_normalized_states(status, state):
    fake = FakeIncus()
    fake.set_instance(exists=True, state=status)

    assert IncusBackend(fake).state("sandbox-demo") == state


def test_state_of_a_missing_instance_is_absent():
    fake = FakeIncus()
    fake.set_instance(exists=False)

    assert IncusBackend(fake).state("sandbox-demo") == ABSENT


def test_metadata_lives_in_user_asbx_config_keys():
    fake = FakeIncus()
    fake.stub("config", "get", "user.asbx.group", stdout="demo\n")
    backend = IncusBackend(fake)

    backend.meta_set("sandbox-demo", META_GROUP, "demo")

    assert ["config", "set", "sandbox-demo", "user.asbx.group", "demo"] in fake.calls
    assert backend.meta_get("sandbox-demo", META_GROUP) == "demo"


def test_start_refuses_an_instance_marked_with_the_removed_guest_firewall():
    fake = FakeIncus()
    fake.stub("config", "get", "user.asbx.egress_mode", stdout="guest-nft")

    with pytest.raises(AsbxError, match="asbx rebuild demo"):
        IncusBackend(fake).start({}, _spec())

    assert ["start", "sandbox-demo"] not in fake.calls


# ---------------------------------------------------------------------------
# mounts (apply)
# ---------------------------------------------------------------------------

def _mount_backend(tmp_path, source: str) -> tuple[FakeIncus, IncusBackend, list[Mount]]:
    os.chmod(str(tmp_path), 0o755)
    fake = FakeIncus()
    fake.stub("config", "get", "user.asbx.egress_mode", stdout="acl")
    fake.set_devices({"mnt-0": {"type": "disk", "source": source, "path": "/home/agent/x", "readonly": "true"}})
    return fake, IncusBackend(fake), [Mount(host=str(tmp_path), guest="/home/agent/x", mode="ro")]


def test_apply_leaves_matching_mount_devices_alone(tmp_path):
    fake, backend, mounts = _mount_backend(tmp_path, str(tmp_path))

    assert backend.apply({}, _spec(mounts)) is False
    assert not any(c[:3] == ["config", "device", "add"] for c in fake.calls)


def test_apply_readds_a_mount_whose_source_differs_and_asks_for_a_restart(tmp_path):
    fake, backend, mounts = _mount_backend(tmp_path, "/somewhere/else")

    assert backend.apply({}, _spec(mounts)) is True
    assert any(c[:6] == ["config", "device", "add", "sandbox-demo", "mnt-0", "disk"] for c in fake.calls)


def test_apply_names_the_mount_incus_refuses(tmp_path):
    fake, backend, mounts = _mount_backend(tmp_path, "/somewhere/else")
    fake.stub("device", "add", "disk", rc=1, stderr="no space")

    with pytest.raises(AsbxError, match=re.escape("/home/agent/x")):
        backend.apply({}, _spec(mounts))


# ---------------------------------------------------------------------------
# waits
# ---------------------------------------------------------------------------

def test_wait_for_agent_raises_after_attempts_exhausted_with_last_stderr(monkeypatch):
    fake = FakeIncus()
    fake.stub("exec", "true", rc=1, stderr="agent not ready")
    monkeypatch.setattr(incusbackend.time, "sleep", lambda s: None)

    with pytest.raises(AsbxError, match="agent not ready"):
        incusbackend.wait_for_agent(fake, "sandbox-demo", attempts=3, interval=0)

    assert len([c for c in fake.calls if "true" in c]) == 3


# ---------------------------------------------------------------------------
# snapshots
# ---------------------------------------------------------------------------

def test_snapshots_include_labels_kept_as_fallback_copies():
    fake = FakeIncus()
    fake.set_snapshots(["pre1"])
    fake.stub("list", "sandbox-demo-snap-", "--format", "json",
              stdout='[{"name": "sandbox-demo-snap-pre2"}, {"name": "sandbox-demo-snap-pre1"}]')

    assert IncusBackend(fake).snapshots("sandbox-demo") == ["pre1", "pre2"]


def test_restore_refuses_an_unknown_label_before_stopping_anything():
    fake = FakeIncus()
    fake.set_snapshots([])
    fake.stub("info", rc=1)

    with pytest.raises(AsbxError, match="no snapshot or copy"):
        IncusBackend(fake).restore({}, _spec(), "nope")

    assert not any(c[0] in ("stop", "delete") for c in fake.calls)


# ---------------------------------------------------------------------------
# build_base
# ---------------------------------------------------------------------------

@pytest.fixture
def build_cfg(cfg, monkeypatch):
    monkeypatch.setattr(incusbackend, "wait_for_agent", lambda *a, **k: None)
    monkeypatch.setattr(incusbackend.guest, "wait_for_cloud_init", lambda g: None)
    return cfg


def _build_fake(cfg) -> FakeIncus:
    fake = FakeIncus()
    fake.stub("info", incusbackend.BASE_BUILD_INSTANCE, rc=1)
    fake.stub("image", "alias", "list", stdout=f'[{{"name": "{cfg["image_alias"]}-x", "target": "f00d"}}]')
    return fake


def test_build_base_refuses_when_a_previous_build_vm_is_still_around(build_cfg):
    fake = FakeIncus()

    with pytest.raises(AsbxError, match="already exists"):
        IncusBackend(fake).build_base(build_cfg, lambda g: None)

    assert not any(c[0] == "launch" for c in fake.calls)


def test_build_base_prepares_the_build_vm_then_publishes(build_cfg, monkeypatch):
    fake = _build_fake(build_cfg)
    monkeypatch.setattr(fake, "image_alias_exists", lambda alias: "f00d")
    b = incusbackend.BASE_BUILD_INSTANCE

    fingerprint = IncusBackend(fake).build_base(
        build_cfg, lambda g: baseimage.prepare_build_vm(g, build_cfg, "echo recipe\n"))

    [launch] = [c for c in fake.calls if c[0] == "launch"]
    assert launch[:4] == ["launch", build_cfg["base_image_source"], b, "--vm"]
    assert not any("cloud-init.user-data" in a for a in launch)
    assert fake.pushed[baseimage.BASE_RECIPE_PATH] == "echo recipe\n"
    run = fake.calls.index(["exec", b, "-t", "--", "bash", baseimage.BASE_RECIPE_PATH])
    clean = fake.calls.index(["exec", b, "--", "cloud-init", "clean", "--logs", "--machine-id"])
    publish = next(i for i, c in enumerate(fake.calls) if c[0] == "publish")
    assert run < clean < publish
    assert ["image", "alias", "create", build_cfg["image_alias"], "f00d"] in fake.calls
    assert fingerprint == "f00d"


def test_build_base_stops_when_the_recipe_fails(build_cfg):
    fake = _build_fake(build_cfg)
    fake.stub("exec", "-t", "bash", baseimage.BASE_RECIPE_PATH, rc=3)

    with pytest.raises(AsbxError, match=r"base recipe failed in asbx-base-build \(exit 3\); "
                                        r"inspect it with 'incus shell asbx-base-build'"):
        IncusBackend(fake).build_base(build_cfg, lambda g: baseimage.prepare_build_vm(g, build_cfg, "false\n"))

    assert not any(c[0] == "publish" for c in fake.calls)
    assert not any("cloud-init" in c for c in fake.calls)
