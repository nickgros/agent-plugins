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
