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
