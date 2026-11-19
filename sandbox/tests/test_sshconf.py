"""Pure-logic tests for asbxlib.sshconf. No Incus or Tart required."""
from __future__ import annotations

import shlex

import pytest

from asbxlib import sshconf
from asbxlib.backend import SshRoute
from asbxlib.errors import AsbxError
from asbxlib.incusbackend import IncusBackend
from asbxlib.tartbackend import TartBackend

from fake_incus import FakeIncus
from fake_tart import FakeTart

STALE_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"


# ---------------------------------------------------------------------------
# render_ssh_config_block
# ---------------------------------------------------------------------------

def test_render_ssh_config_block_matches_exact_format():
    rendered = sshconf.render_ssh_config_block(
        "sandbox-backend",
        SshRoute(host_key_alias="sandbox-backend.incus", hostname="sandbox-backend.incus"),
        "~/.config/agent-sandbox/id_ed25519",
        "~/.config/agent-sandbox/known_hosts",
        "dev",
    )
    expected = (
        "# managed by asbx — do not edit\n"
        "Host sandbox-backend\n"
        "    HostName sandbox-backend.incus\n"
        "    HostKeyAlias sandbox-backend.incus\n"
        "    User dev\n"
        "    IdentityFile ~/.config/agent-sandbox/id_ed25519\n"
        "    IdentitiesOnly yes\n"
        "    StrictHostKeyChecking accept-new\n"
        "    UserKnownHostsFile ~/.config/agent-sandbox/known_hosts\n"
    )
    assert rendered == expected


def test_render_ssh_config_block_with_a_proxy_route_has_no_hostname():
    rendered = sshconf.render_ssh_config_block(
        "sandbox-demo", SshRoute(host_key_alias="sandbox-demo", proxy_command="/py /asbx ssh-proxy sandbox-demo"),
        "k", "kh", "dev")

    assert "    ProxyCommand /py /asbx ssh-proxy sandbox-demo\n" in rendered
    assert "    HostKeyAlias sandbox-demo\n" in rendered
    assert "HostName" not in rendered


# ---------------------------------------------------------------------------
# write_ssh_config — built from the backend's host-side route, never the guest
# ---------------------------------------------------------------------------

def test_incus_route_is_instance_name_under_the_configured_dns_domain(cfg):
    backend = IncusBackend(FakeIncus())
    assert backend.ssh_route(cfg, "sandbox-demo") == SshRoute("sandbox-demo.incus", hostname="sandbox-demo.incus")
    cfg["network"]["dns_domain"] = "lab.internal"
    assert backend.ssh_address(cfg, "sandbox-demo") == "sandbox-demo.lab.internal"


def test_write_ssh_config_writes_the_route_and_drops_a_stale_host_key(cfg, tmp_path):
    cfg["ssh"]["config_dir"] = str(tmp_path / "confs")
    known_hosts = tmp_path / "known_hosts"
    cfg["ssh"]["known_hosts"] = str(known_hosts)
    known_hosts.write_text(f"sandbox-demo.incus {STALE_KEY}\n")

    sshconf.write_ssh_config(cfg, "sandbox-demo", SshRoute("sandbox-demo.incus", hostname="sandbox-demo.incus"))

    body = (tmp_path / "confs" / "sandbox-demo.conf").read_text()
    assert "    HostName sandbox-demo.incus\n" in body
    assert "    User agent\n" in body
    assert "sandbox-demo.incus" not in known_hosts.read_text()


# ---------------------------------------------------------------------------
# render_remote_stanza / cmd_ssh_config
# ---------------------------------------------------------------------------

def test_render_remote_stanza_is_a_literal_host_block_through_the_jump():
    rendered = sshconf.render_remote_stanza(
        "sandbox-demo", SshRoute(host_key_alias="sandbox-demo.incus", hostname="sandbox-demo.incus"),
        "agent", "me@workstation", "~/.ssh/id_ed25519")
    assert rendered == (
        "Host sandbox-demo\n"
        "    HostName sandbox-demo.incus\n"
        "    User agent\n"
        "    ProxyJump me@workstation\n"
        "    IdentityFile ~/.ssh/id_ed25519\n"
        "    IdentitiesOnly yes\n"
        "    StrictHostKeyChecking no\n"
        "    UserKnownHostsFile /dev/null\n"
        "    LogLevel ERROR\n"
    )


def test_cmd_ssh_config_uses_the_manifest_instance_name(cfg, config_root, capsys):
    (config_root / "demo.yaml").write_text("name: custom\n")
    cfg["instance_prefix"] = "sandbox-"

    sshconf.cmd_ssh_config(IncusBackend(FakeIncus()), cfg, "demo", "me@workstation", "~/.ssh/id_ed25519")

    out = capsys.readouterr().out
    assert "Host sandbox-custom\n" in out
    assert "HostName sandbox-custom.incus" in out
    assert "ProxyJump me@workstation" in out


def test_cmd_ssh_config_direct_targets_the_pinned_ip_without_a_jump(cfg, config_root, capsys):
    (config_root / "demo.yaml").write_text("{}\n")
    fake = FakeIncus()
    fake.set_instance(exists=True)
    fake.set_devices({"eth0": {"ipv4.address": "10.210.86.192", "security.ipv4_filtering": "true",
                               "security.mac_filtering": "true"}})

    sshconf.cmd_ssh_config(IncusBackend(fake), cfg, "demo", None, "~/.ssh/id_ed25519")

    out = capsys.readouterr().out
    assert "    HostName 10.210.86.192\n" in out
    assert "ProxyJump" not in out


def test_cmd_ssh_config_direct_refuses_an_address_without_anti_spoofing(cfg, config_root):
    # An unfiltered address could be claimed by a neighbour sandbox, so it is never handed out.
    (config_root / "demo.yaml").write_text("{}\n")
    fake = FakeIncus()
    fake.set_instance(exists=True)
    fake.set_devices({"eth0": {"ipv4.address": "10.210.86.192"}})

    with pytest.raises(AsbxError, match="asbx up demo"):
        sshconf.cmd_ssh_config(IncusBackend(fake), cfg, "demo", None, "~/.ssh/id_ed25519")


def test_cmd_ssh_config_jump_on_tart_runs_the_proxy_command_on_the_jump_host(cfg, config_root, capsys):
    (config_root / "demo.yaml").write_text("{}\n")
    backend = TartBackend(FakeTart().tart())

    sshconf.cmd_ssh_config(backend, cfg, "demo", "me@mac", "~/.ssh/id_ed25519")

    out = capsys.readouterr().out
    [line] = [l for l in out.splitlines() if "ProxyCommand" in l]
    # The local shell hands ssh one command string, which the Mac's shell then
    # splits exactly as the Mac's own ssh config would.
    ssh, jump, remote = shlex.split(line.split("ProxyCommand ", 1)[1])
    assert (ssh, jump) == ("ssh", "me@mac")
    assert shlex.split(remote) == shlex.split(backend.ssh_route(cfg, "sandbox-demo").proxy_command)
    assert "HostName" not in out and "ProxyJump" not in out
    assert "Host sandbox-demo\n" in out and "    User agent\n" in out


def test_cmd_ssh_config_direct_is_refused_on_tart_which_pins_no_addresses(cfg, config_root):
    (config_root / "demo.yaml").write_text("{}\n")
    fake = FakeTart()
    fake.add_vm("sandbox-demo", state="running")

    with pytest.raises(AsbxError, match="no pinned IPv4; --direct needs a backend that pins"):
        sshconf.cmd_ssh_config(TartBackend(fake.tart()), cfg, "demo", None, "~/.ssh/id_ed25519")


# ---------------------------------------------------------------------------
# ensure_ssh_include / include_is_first
# ---------------------------------------------------------------------------

def test_include_line_follows_the_configured_config_dir(cfg):
    cfg["ssh"]["config_dir"] = "~/.ssh/sandboxes"
    assert sshconf.include_line(cfg) == "Include ~/.ssh/sandboxes/*.conf"


def test_ensure_ssh_include_creates_the_file_with_the_line(cfg, tmp_path):
    path = tmp_path / "config"
    sshconf.ensure_ssh_include(cfg, path)
    assert path.read_text() == sshconf.include_line(cfg) + "\n"
    assert path.stat().st_mode & 0o777 == 0o600


def test_ensure_ssh_include_moves_a_lower_line_to_the_top(cfg, tmp_path):
    line = sshconf.include_line(cfg)
    path = tmp_path / "config"
    path.write_text(f"Host a\n    User x\n\n{line}\n\nHost b\n")

    sshconf.ensure_ssh_include(cfg, path)

    text = path.read_text()
    assert text.splitlines()[0] == line
    assert text.count(line) == 1
    assert "Host a\n    User x" in text and "Host b" in text


def test_ensure_ssh_include_leaves_a_correct_file_untouched(cfg, tmp_path):
    line = sshconf.include_line(cfg)
    path = tmp_path / "config"
    original = f"# mine\n{line}\nHost a\n"
    path.write_text(original)

    sshconf.ensure_ssh_include(cfg, path)

    assert path.read_text() == original


def test_include_is_first_ignores_comments_and_blank_lines_but_not_hosts(cfg):
    line = sshconf.include_line(cfg)
    assert sshconf.include_is_first(f"\n# c\n{line}\nHost a\n", line)
    assert not sshconf.include_is_first(f"Host a\n{line}\n", line)
    assert not sshconf.include_is_first("", line)
