"""Pure-logic tests for asbxlib.manifest. No Incus required."""
from __future__ import annotations

import pytest

from asbxlib import manifest
from asbxlib.errors import AsbxError


# ---------------------------------------------------------------------------
# validate_manifest_dict
# ---------------------------------------------------------------------------

def test_manifest_rw_mount_rejected():
    data = {"mounts": [{"host": "~/x", "guest": "/home/agent/x", "mode": "rw"}]}
    with pytest.raises(AsbxError, match="not implemented"):
        manifest.validate_manifest_dict(data, "/tmp/demo.yaml")


def test_manifest_unknown_top_level_key_named():
    with pytest.raises(AsbxError, match="bogus_key"):
        manifest.validate_manifest_dict({"bogus_key": True}, "/tmp/demo.yaml")


def test_manifest_unknown_resources_key_named_with_dotted_path():
    with pytest.raises(AsbxError, match=r"unknown key 'resources\.gpu'"):
        manifest.validate_manifest_dict({"resources": {"gpu": 1}}, "/tmp/demo.yaml")


def test_manifest_unknown_services_key_named_with_dotted_path():
    with pytest.raises(AsbxError, match=r"unknown key 'services\.bogus'"):
        manifest.validate_manifest_dict({"services": {"bogus": "x"}}, "/tmp/demo.yaml")


def test_manifest_non_mapping_resources_rejected():
    with pytest.raises(AsbxError, match="'resources' must be a mapping"):
        manifest.validate_manifest_dict({"resources": ["cpu"]}, "/tmp/demo.yaml")


def test_manifest_duplicate_repo_dir_rejected():
    data = {
        "repos": [
            {"url": "https://example.com/foo.git", "dir": "shared"},
            {"url": "https://example.com/bar.git", "dir": "shared"},
        ],
    }
    with pytest.raises(AsbxError, match="duplicate.*'shared'"):
        manifest.validate_manifest_dict(data, "/tmp/demo.yaml")


def test_manifest_invalid_memory_unit_rejected():
    with pytest.raises(AsbxError, match="memory"):
        manifest.validate_manifest_dict({"resources": {"memory": "8GB"}}, "/tmp/demo.yaml")


def test_manifest_remotes_non_string_value_rejected():
    with pytest.raises(AsbxError, match="remotes"):
        manifest.validate_manifest_dict({"remotes": {"upstream": 123}}, "/tmp/demo.yaml")


def test_manifest_repo_remotes_non_string_value_rejected():
    data = {"repos": [{"url": "https://example.com/foo.git", "remotes": {"fork": ["nope"]}}]}
    with pytest.raises(AsbxError, match=r"repos\[\*\]\.remotes"):
        manifest.validate_manifest_dict(data, "/tmp/demo.yaml")


# A mount without host/guest must be a user-facing AsbxError, never a KeyError
# escaping out of merge_mounts or the device-add path.
@pytest.mark.parametrize("mount,missing", [
    ({"guest": "/home/agent/x"}, "host"),
    ({"host": "~/x"}, "guest"),
    ({"host": "", "guest": "/home/agent/x"}, "host"),
])
def test_manifest_mount_requires_host_and_guest(mount, missing):
    with pytest.raises(AsbxError, match=rf"mounts\[\*\]\.{missing} is required"):
        manifest.validate_manifest_dict({"mounts": [mount]}, "/tmp/demo.yaml")


def test_manifest_setup_inside_manifest_dir_accepted(tmp_path):
    (tmp_path / "setup.sh").write_text("#!/bin/sh\n")
    data = {"setup": "./setup.sh"}
    assert manifest.validate_manifest_dict(data, str(tmp_path / "demo.yaml")) is data


def test_manifest_setup_escaping_manifest_dir_rejected(tmp_path):
    with pytest.raises(AsbxError, match="'setup' escapes manifest directory"):
        manifest.validate_manifest_dict({"setup": "../evil.sh"}, str(tmp_path / "demo.yaml"))


def test_manifest_services_compose_escaping_manifest_dir_rejected(tmp_path):
    data = {"services": {"compose": "../../etc/passwd"}}
    with pytest.raises(AsbxError, match="'services' escapes manifest directory"):
        manifest.validate_manifest_dict(data, str(tmp_path / "demo.yaml"))


def test_manifest_setup_symlinked_out_of_manifest_dir_rejected(tmp_path):
    outside = tmp_path / "outside.sh"
    outside.write_text("#!/bin/sh\n")
    project = tmp_path / "project"
    project.mkdir()
    (project / "setup.sh").symlink_to(outside)
    with pytest.raises(AsbxError, match="'setup' escapes manifest directory"):
        manifest.validate_manifest_dict({"setup": "setup.sh"}, str(project / "demo.yaml"))


def test_manifest_setup_missing_file_rejected(tmp_path):
    with pytest.raises(AsbxError, match="'setup' file not found"):
        manifest.validate_manifest_dict({"setup": "setup.sh"}, str(tmp_path / "demo.yaml"))


# ---------------------------------------------------------------------------
# merge_resources / merge_mounts
# ---------------------------------------------------------------------------

def test_merge_resources_manifest_overrides_only_given_keys():
    result = manifest.merge_resources(
        {"cpu": 4, "memory": "8GiB", "disk": "30GiB"}, {"cpu": 6}
    )
    assert result == {"cpu": 6, "memory": "8GiB", "disk": "30GiB"}


def test_merge_mounts_manifest_replaces_by_guest_path_others_survive():
    defaults = [
        manifest.Mount(host="~/.agents/skills", guest="/home/agent/.agents/skills", mode="ro"),
        manifest.Mount(host="~/other", guest="/home/agent/other", mode="ro"),
    ]
    manifest_mounts = [
        manifest.Mount(host="~/src/reference-docs", guest="/home/agent/.agents/skills", mode="ro"),
    ]
    result = manifest.merge_mounts(defaults, manifest_mounts)
    by_guest = {m.guest: m for m in result}
    assert by_guest["/home/agent/.agents/skills"].host == "~/src/reference-docs"
    assert by_guest["/home/agent/other"].host == "~/other"
    assert len(result) == 2


# ---------------------------------------------------------------------------
# resolve_remote_url / merge_remotes / repo_dir
# ---------------------------------------------------------------------------

def test_resolve_remote_url_substitutes_owner_https():
    url = manifest.resolve_remote_url(
        "https://github.com/nickgros/Synapse-Repository-Services.git", "Sage-Bionetworks"
    )
    assert url == "https://github.com/Sage-Bionetworks/Synapse-Repository-Services.git"


def test_resolve_remote_url_substitutes_owner_ssh():
    url = manifest.resolve_remote_url("git@github.com:nickgros/foo.git", "Sage-Bionetworks")
    assert url == "git@github.com:Sage-Bionetworks/foo.git"


def test_resolve_remote_url_full_url_used_verbatim():
    url = manifest.resolve_remote_url(
        "https://github.com/nickgros/foo.git", "https://gitlab.com/other/bar.git"
    )
    assert url == "https://gitlab.com/other/bar.git"


def test_resolve_remote_url_unparseable_repo_url_raises():
    with pytest.raises(AsbxError, match="cannot derive"):
        manifest.resolve_remote_url("not-a-url", "Sage-Bionetworks")


def test_merge_remotes_disjoint_names_all_survive():
    result = manifest.merge_remotes({"upstream": "Sage-Bionetworks"}, {"fork": "jsmith"})
    assert result == {"upstream": "Sage-Bionetworks", "fork": "jsmith"}


def test_merge_remotes_override_wins_on_name_collision():
    base = {"upstream": "Sage-Bionetworks"}
    result = manifest.merge_remotes(base, {"upstream": "other-org"})
    assert result == {"upstream": "other-org"}
    assert base == {"upstream": "Sage-Bionetworks"}


def test_repo_dir_derived_from_url_unless_given():
    assert manifest.repo_dir({"url": "https://github.com/nickgros/foo.git"}) == "foo"
    assert manifest.repo_dir({"url": "https://github.com/nickgros/foo/"}) == "foo"
    assert manifest.repo_dir({"url": "https://github.com/nickgros/foo.git", "dir": "bar"}) == "bar"


# ---------------------------------------------------------------------------
# load_manifest
# ---------------------------------------------------------------------------

def test_load_manifest_top_level_remote_overrides_repo_remote(config_root, cfg):
    (config_root / "demo.yaml").write_text(
        "remotes:\n"
        "  upstream: Sage-Bionetworks\n"
        "repos:\n"
        "  - url: https://github.com/nickgros/foo.git\n"
        "    remotes:\n"
        "      upstream: nickgros\n"
        "      fork: jsmith\n"
    )
    result = manifest.load_manifest("demo", cfg)

    repo = result.repos[0]
    assert repo.dir == "foo"
    # The group-wide setting is the one the operator states last.
    assert repo.remotes["upstream"] == "Sage-Bionetworks"
    assert repo.remotes["fork"] == "jsmith"
    assert result.remotes == {"upstream": "Sage-Bionetworks"}
    assert result.instance == f"{cfg['instance_prefix']}demo"
    assert result.setup_path is None
    assert result.compose_path is None


def test_load_manifest_missing_manifest_raises(config_root, cfg):
    with pytest.raises(AsbxError, match="no manifest at"):
        manifest.load_manifest("nope", cfg)
