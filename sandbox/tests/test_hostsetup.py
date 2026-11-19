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

from asbxlib import config, host, incusbackend, ingress
from asbxlib.errors import AsbxError
from asbxlib.incusbackend import IncusBackend
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
    acls: list = []
    monkeypatch.setattr(incusbackend.netacl, "ensure_acl", lambda incus, cfg: acls.append(cfg["network"]["acl"]))
    monkeypatch.setattr(hostsetup.sshconf, "ensure_ssh_include", lambda cfg: None)
    # The ingress firewall is opt-in: init must never install it.
    monkeypatch.setattr(ingress, "install", lambda *a, **k: pytest.fail("init installed the firewall"))

    hostsetup.cmd_init(IncusBackend(FakeIncus()))

    assert acls == ["asbx-egress"]   # the backend readied its host-side state

    path = config.config_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert yaml.safe_load(path.read_text())["guest_user"] == config.DEFAULT_CONFIG["guest_user"]

    path.write_text("guest_user: kept\n")
    hostsetup.cmd_init(IncusBackend(FakeIncus()))
    assert path.read_text() == "guest_user: kept\n"


@pytest.mark.parametrize("acl_fails", [False, True])
def test_cmd_init_refreshes_the_acl_even_when_the_ingress_firewall_check_fails(home, monkeypatch, capsys, acl_fails):
    monkeypatch.setitem(config.DEFAULT_CONFIG["ssh"], "key", str(home / "key"))
    monkeypatch.setitem(config.DEFAULT_CONFIG["ssh"], "config_dir", str(home / "confd"))
    acls: list = []

    def ensure_acl(incus, cfg):
        acls.append(cfg["network"]["acl"])
        if acl_fails:
            raise AsbxError("bridge missing")

    def require_firewall(cfg):
        raise AsbxError("ingress firewall unit inactive")

    monkeypatch.setattr(incusbackend.netacl, "ensure_acl", ensure_acl)
    monkeypatch.setattr(incusbackend.ingress, "require_firewall", require_firewall)
    monkeypatch.setattr(hostsetup.sshconf, "ensure_ssh_include", lambda cfg: None)

    hostsetup.cmd_init(IncusBackend(FakeIncus()))

    assert acls == ["asbx-egress"]
    err = capsys.readouterr().err
    assert "ingress firewall unit inactive" in err
    assert ("could not refresh network ACL: bridge missing" in err) == acl_fails
    assert config.config_path().exists()


def test_cmd_install_refuses_to_replace_an_existing_link_without_force(home):
    hostsetup.cmd_install(force=False)
    target = home / ".local" / "bin" / "asbx"
    assert target.is_symlink() and target.resolve() == hostsetup.ENTRY_POINT

    with pytest.raises(AsbxError, match="--force"):
        hostsetup.cmd_install(force=False)

    hostsetup.cmd_install(force=True)
    assert target.resolve() == hostsetup.ENTRY_POINT
