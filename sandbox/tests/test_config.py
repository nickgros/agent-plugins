from __future__ import annotations

import pytest

from asbxlib import config
from asbxlib.errors import AsbxError


def test_deep_merge_unknown_nested_key_named_with_full_dotted_path():
    with pytest.raises(AsbxError, match=r"unknown key 'network\.bogus'"):
        config.deep_merge(config.DEFAULT_CONFIG, {"network": {"bogus": True}})


def test_deep_merge_nested_override_keeps_siblings_and_leaves_base_untouched():
    base = {"network": {"name": "incusbr0", "acl": "asbx-egress"}, "guest_user": "agent"}
    merged = config.deep_merge(base, {"network": {"name": "br1"}})
    assert merged["network"] == {"name": "br1", "acl": "asbx-egress"}
    assert merged["guest_user"] == "agent"
    assert base["network"] == {"name": "incusbr0", "acl": "asbx-egress"}


def test_deep_merge_accepts_arbitrary_keys_in_open_maps():
    merged = config.deep_merge(config.DEFAULT_CONFIG, {
        "defaults": {"env": {"FOO": "1"}, "remotes": {"upstream": "git@x:y.git"}},
        "harness_env": {"EXTRA": "2"},
    })
    assert merged["defaults"]["env"] == {"FOO": "1"}
    assert merged["defaults"]["remotes"] == {"upstream": "git@x:y.git"}
    assert merged["harness_env"]["EXTRA"] == "2"
    assert merged["harness_env"]["AWS_REGION"] == "us-east-1"   # defaults kept


def test_deep_merge_still_rejects_unknown_keys_beside_open_maps():
    with pytest.raises(AsbxError, match="defaults.bogus"):
        config.deep_merge(config.DEFAULT_CONFIG, {"defaults": {"bogus": 1}})


def test_normalize_env_stringifies_scalars_yaml_style():
    out = config.normalize_env({"A": 1, "B": True, "C": False, "D": "x", "E": 1.5}, "env")
    assert out == {"A": "1", "B": "true", "C": "false", "D": "x", "E": "1.5"}


def test_normalize_env_rejects_non_scalar_values_naming_the_key():
    with pytest.raises(AsbxError, match="env.BAD"):
        config.normalize_env({"BAD": ["a"]}, "env")


def test_deep_merge_rejects_removed_aws_profile_key():
    with pytest.raises(AsbxError, match=r"unknown key 'aws\.profile'"):
        config.deep_merge(config.DEFAULT_CONFIG, {"aws": {"profile": "x"}})


# ---------------------------------------------------------------------------
# validate_harness_list
# ---------------------------------------------------------------------------

def test_harness_credential_item_in_sync_rejected():
    with pytest.raises(AsbxError, match="'omp-mcp' carries credentials"):
        config.validate_harness_list(["omp-mcp"], "harness_settings.sync", credentials=False)


def test_harness_plain_item_in_credentials_rejected():
    with pytest.raises(AsbxError, match="'omp' holds no credentials"):
        config.validate_harness_list(["omp"], "harness_settings.credentials", credentials=True)


def test_harness_unknown_item_rejected():
    with pytest.raises(AsbxError, match="unknown harness item 'nope'"):
        config.validate_harness_list(["nope"], "w", credentials=False)


@pytest.mark.parametrize("value,message", [
    ("omp", "must be a list"),
    ([1], "must be a list"),
    ([{"guest": "~/a"}], "'host' is required"),
    ([{"host": "~/a", "extra": 1}], "unknown key 'extra'"),
    ([{"host": "a"}], "must start with ~/ or /"),
])
def test_harness_malformed_lists_rejected(value, message):
    with pytest.raises(AsbxError, match=message):
        config.validate_harness_list(value, "w", credentials=False)


def test_harness_inline_host_outside_home_requires_guest():
    with pytest.raises(AsbxError, match="guest is required when host is not under ~"):
        config.validate_harness_list([{"host": "/etc/x"}], "w", credentials=False)


@pytest.mark.parametrize("guest,message", [
    ("~/../b", "must not contain '..'"),
    ("~/a/../b", "must not contain '..'"),
    ("/abs", "must start with ~/"),
    ("b", "must start with ~/"),
    ("~/", "not ~ itself"),
    ("~/.", "not ~ itself"),
])
def test_harness_inline_guest_must_stay_under_home(guest, message):
    with pytest.raises(AsbxError, match=message):
        config.validate_harness_list([{"host": "~/a", "guest": guest}], "w", credentials=False)


def test_harness_inline_guest_is_normalized_so_prune_paths_match():
    out = config.validate_harness_list([{"host": "~/a", "guest": "~//x/./y//z"}], "w", credentials=False)

    assert out == [{"host": "~/a", "guest": "~/x/y/z"}]


def test_harness_inline_guest_defaults_to_host():
    assert config.validate_harness_list([{"host": "~/a"}, "omp"], "w", credentials=False) == [
        {"host": "~/a", "guest": "~/a"}, "omp"]


def test_load_config_rejects_credential_item_in_sync(tmp_path, monkeypatch):
    monkeypatch.setenv(config.CONFIG_DIR_ENV, str(tmp_path))
    (tmp_path / "config.yaml").write_text("harness_settings:\n  sync: [omp-mcp]\n")

    with pytest.raises(AsbxError, match="harness_settings.sync: 'omp-mcp' carries credentials"):
        config.load_config()
