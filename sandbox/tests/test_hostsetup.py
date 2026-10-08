from __future__ import annotations

from asbxlib import hostsetup


def test_entry_point_exists_is_a_file_named_asbx():
    # Guards against a future layout move silently making `asbx install`
    # symlink nothing: ENTRY_POINT must keep resolving to the real
    # sandbox/asbx executable shim.
    assert hostsetup.ENTRY_POINT.is_file()
    assert hostsetup.ENTRY_POINT.name == "asbx"


import stat

import pytest
import yaml

from asbxlib import config, host
from asbxlib.errors import AsbxError
from fake_incus import FakeIncus


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv(config.CONFIG_DIR_ENV, str(tmp_path / "cfg"))
    monkeypatch.setattr(hostsetup.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(host, "generate_ssh_key", lambda p: p.write_text("k") or p.with_name(p.name + ".pub").write_text("p"))
    return tmp_path


def test_cmd_init_writes_a_private_config_and_does_not_overwrite_it(home, monkeypatch):
    # point every path the default config uses at tmp
    monkeypatch.setitem(config.DEFAULT_CONFIG["ssh"], "key", str(home / "key"))
    monkeypatch.setitem(config.DEFAULT_CONFIG["ssh"], "config_dir", str(home / "confd"))
    monkeypatch.setattr(hostsetup.netacl, "ensure_acl", lambda incus, cfg: None)
    monkeypatch.setattr(hostsetup.sshconf, "ensure_ssh_include", lambda cfg: None)

    hostsetup.cmd_init(FakeIncus())

    path = config.config_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert yaml.safe_load(path.read_text())["guest_user"] == config.DEFAULT_CONFIG["guest_user"]

    path.write_text("guest_user: kept\n")
    hostsetup.cmd_init(FakeIncus())
    assert path.read_text() == "guest_user: kept\n"


def test_cmd_install_refuses_to_replace_an_existing_link_without_force(home):
    hostsetup.cmd_install(force=False)
    target = home / ".local" / "bin" / "asbx"
    assert target.is_symlink() and target.resolve() == hostsetup.ENTRY_POINT

    with pytest.raises(AsbxError, match="--force"):
        hostsetup.cmd_install(force=False)

    hostsetup.cmd_install(force=True)
    assert target.resolve() == hostsetup.ENTRY_POINT
