"""Pure-logic tests for asbxlib.sshconf. No Incus required."""
from __future__ import annotations

from asbxlib import sshconf

STALE_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"


# ---------------------------------------------------------------------------
# render_ssh_config_block
# ---------------------------------------------------------------------------

def test_render_ssh_config_block_matches_exact_format():
    rendered = sshconf.render_ssh_config_block(
        "sandbox-backend",
        "sandbox-backend.incus",
        "~/.config/agent-sandbox/id_ed25519",
        "~/.config/agent-sandbox/known_hosts",
        "dev",
    )
    expected = (
        "# managed by asbx — do not edit\n"
        "Host sandbox-backend\n"
        "    HostName sandbox-backend.incus\n"
        "    User dev\n"
        "    IdentityFile ~/.config/agent-sandbox/id_ed25519\n"
        "    IdentitiesOnly yes\n"
        "    StrictHostKeyChecking accept-new\n"
        "    UserKnownHostsFile ~/.config/agent-sandbox/known_hosts\n"
    )
    assert rendered == expected


# ---------------------------------------------------------------------------
# write_ssh_config — HostName comes from config, not from anything the guest says
# ---------------------------------------------------------------------------

def test_instance_hostname_is_instance_name_under_the_configured_dns_domain(cfg):
    assert sshconf.instance_hostname("sandbox-demo", cfg) == "sandbox-demo.incus"
    cfg["network"]["dns_domain"] = "lab.internal"
    assert sshconf.instance_hostname("sandbox-demo", cfg) == "sandbox-demo.lab.internal"


def test_write_ssh_config_writes_the_derived_hostname_and_drops_a_stale_host_key(cfg, tmp_path):
    cfg["ssh"]["config_dir"] = str(tmp_path / "confs")
    known_hosts = tmp_path / "known_hosts"
    cfg["ssh"]["known_hosts"] = str(known_hosts)
    known_hosts.write_text(f"sandbox-demo.incus {STALE_KEY}\n")

    hostname = sshconf.write_ssh_config("sandbox-demo", cfg)

    assert hostname == "sandbox-demo.incus"
    body = (tmp_path / "confs" / "sandbox-demo.conf").read_text()
    assert "    HostName sandbox-demo.incus\n" in body
    assert "    User agent\n" in body
    assert "sandbox-demo.incus" not in known_hosts.read_text()


# ---------------------------------------------------------------------------
# render_remote_stanza / cmd_ssh_config
# ---------------------------------------------------------------------------

def test_render_remote_stanza_is_a_literal_host_block_through_the_jump():
    rendered = sshconf.render_remote_stanza(
        "sandbox-demo", "sandbox-demo.incus", "agent", "me@workstation", "~/.ssh/id_ed25519")
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

    sshconf.cmd_ssh_config(cfg, "demo", "me@workstation", "~/.ssh/id_ed25519")

    out = capsys.readouterr().out
    assert "Host sandbox-custom\n" in out
    assert "HostName sandbox-custom.incus" in out
    assert "ProxyJump me@workstation" in out
