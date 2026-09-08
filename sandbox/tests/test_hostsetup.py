from __future__ import annotations

from asbxlib import hostsetup


def test_entry_point_exists_is_a_file_named_asbx():
    # Guards against a future layout move silently making `asbx install`
    # symlink nothing: ENTRY_POINT must keep resolving to the real
    # sandbox/asbx executable shim.
    assert hostsetup.ENTRY_POINT.is_file()
    assert hostsetup.ENTRY_POINT.name == "asbx"
