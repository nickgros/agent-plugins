"""Tests for asbxlib.ingress: interface eligibility, the rendered firewall and
WireGuard files, the fail-closed gate `up` uses, and the setup commands."""
from __future__ import annotations

import copy

import pytest

from asbxlib import config, ingress
from asbxlib.errors import AsbxError
from asbxlib.host import InterfaceFacts

from fake_incus import FakeIncus

HOST_KEY = "aG9zdCBrZXkgZm9yIGFzYnggdGVzdHMgMzIgYnl0ZXM="
CLIENT_KEY = "Y2xpZW50IGtleSBmb3IgYXNieCB0ZXN0cyAzMiBieXQ="

WIREGUARD = InterfaceFacts(devtype="wireguard", tun_flags=None, is_bridge=False)
TUN = InterfaceFacts(devtype="", tun_flags=0x5001, is_bridge=False)
TAP = InterfaceFacts(devtype="", tun_flags=0x7902, is_bridge=False)
ETHERNET = InterfaceFacts(devtype="", tun_flags=None, is_bridge=False)
BRIDGE = InterfaceFacts(devtype="bridge", tun_flags=None, is_bridge=True)


def _cfg(interfaces=(), managed=False, clients=None, endpoint=""):
    cfg = copy.deepcopy(config.DEFAULT_CONFIG)
    ing = cfg["network"]["ingress"]
    ing["interfaces"] = list(interfaces)
    ing["wireguard"]["enabled"] = managed
    ing["wireguard"]["endpoint"] = endpoint
    ing["wireguard"]["clients"] = clients or {}
    cfg["network"]["ingress"] = config.validate_ingress(ing)
    return cfg


@pytest.fixture
def host_ifaces(monkeypatch):
    """Scripts sysfs: name -> InterfaceFacts; enp42s0 carries the default route."""
    facts = {"tailscale0": TUN, "wg1": WIREGUARD, "tap1": TAP, "enp42s0": ETHERNET,
             "enp5s0": ETHERNET, "docker0": BRIDGE}
    monkeypatch.setattr(ingress.host, "interface_facts", lambda name: facts.get(name))
    monkeypatch.setattr(ingress.host, "interface_names", lambda: sorted(facts))
    monkeypatch.setattr(ingress.host, "default_route_interfaces", lambda: {"enp42s0"})
    return facts


# ---------------------------------------------------------------------------
# interface_problem: only cryptographically authenticated interfaces qualify
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,facts,managed,expected", [
    ("wg1", WIREGUARD, False, None),
    ("tailscale0", TUN, False, None),
    ("asbx0", None, True, None),                          # managed: wg-quick creates it later
    ("asbx0", None, False, "does not exist"),
    ("tap1", TAP, False, "is a tap device (VM NICs are taps)"),
    ("enp5s0", ETHERNET, False, "is not a WireGuard or tun interface (ethernet-like)"),
    ("docker0", BRIDGE, False, "is a bridge"),
    ("incusbr0", BRIDGE, False, "is the sandbox bridge"),
    ("tun9", TUN, False, "carries the host's default route"),
])
def test_interface_problem(name, facts, managed, expected):
    assert ingress.interface_problem(name, facts, "incusbr0", {"tun9"}, managed) == expected


def test_eligible_candidates_lists_only_unconfigured_wireguard_and_tun_interfaces(host_ifaces):
    assert ingress.eligible_candidates(_cfg()) == ["tailscale0", "wg1"]
    assert ingress.eligible_candidates(_cfg(interfaces=["wg1"])) == ["tailscale0"]


# ---------------------------------------------------------------------------
# render_firewall
# ---------------------------------------------------------------------------

def _rules(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines()]


def test_render_firewall_denies_every_new_connection_into_the_bridge_without_ingress():
    rules = _rules(ingress.render_firewall(_cfg()))

    assert rules.index('oifname "incusbr0" ct state established,related accept') \
        < rules.index('oifname "incusbr0" drop')
    assert not any("iifname {" in r for r in rules)
    assert "chain input {" not in rules


def test_render_firewall_admits_ipv4_only_from_ingress_and_leaves_adopted_host_input_alone():
    rules = _rules(ingress.render_firewall(_cfg(interfaces=["tailscale0"])))

    accept = 'oifname "incusbr0" iifname { "tailscale0" } meta nfproto ipv4 accept'
    assert rules.index(accept) < rules.index('oifname "incusbr0" drop')
    assert "chain input {" not in rules
    assert not any(r.startswith('iifname "tailscale0"') or r.startswith('oifname "tailscale0"') for r in rules)


def test_render_firewall_locks_the_managed_interface_to_the_bridge():
    rules = _rules(ingress.render_firewall(_cfg(interfaces=["tailscale0"], managed=True)))

    assert 'oifname "incusbr0" iifname { "tailscale0", "asbx0" } meta nfproto ipv4 accept' in rules
    assert rules.index('oifname "incusbr0" drop') < rules.index('iifname "asbx0" drop')
    assert 'oifname "asbx0" drop' in rules
    input_chain = rules[rules.index("chain input {"):]
    assert input_chain.index('iifname "asbx0" ct state established,related accept') \
        < input_chain.index('iifname "asbx0" drop')


def test_render_firewall_replaces_the_table_atomically():
    text = ingress.render_firewall(_cfg())
    assert _rules(text)[1:4] == ["table inet asbx", "delete table inet asbx", "table inet asbx {"]


# ---------------------------------------------------------------------------
# require_firewall: `up` runs only when the deny is in force for this config
# ---------------------------------------------------------------------------

@pytest.fixture
def installed(monkeypatch, host_ifaces):
    state = {"file": None, "active": True}
    monkeypatch.setattr(ingress.host, "read_text", lambda path: state["file"])
    monkeypatch.setattr(ingress, "firewall_unit_active", lambda: state["active"])
    return state


def test_require_firewall_passes_when_the_installed_file_matches_and_the_unit_is_active(installed):
    cfg = _cfg(interfaces=["tailscale0"])
    installed["file"] = ingress.render_firewall(cfg)

    ingress.require_firewall(cfg)


def test_require_firewall_refuses_a_file_rendered_for_other_interfaces(installed):
    installed["file"] = ingress.render_firewall(_cfg())

    with pytest.raises(AsbxError, match="out of date"):
        ingress.require_firewall(_cfg(interfaces=["tailscale0"]))


def test_require_firewall_refuses_an_inactive_unit(installed):
    cfg = _cfg()
    installed["file"] = ingress.render_firewall(cfg)
    installed["active"] = False

    with pytest.raises(AsbxError, match="not active"):
        ingress.require_firewall(cfg)


def test_require_firewall_refuses_an_ineligible_interface_even_if_installed(installed):
    cfg = _cfg(interfaces=["enp42s0"])
    installed["file"] = ingress.render_firewall(cfg)

    with pytest.raises(AsbxError, match="enp42s0 carries the host's default route"):
        ingress.require_firewall(cfg)


# ---------------------------------------------------------------------------
# render_unit / plan_install (pure)
# ---------------------------------------------------------------------------

def test_render_unit_loads_the_table_before_incus_and_never_unloads_it():
    unit = ingress.render_unit("/usr/sbin/nft")

    assert "Before=network-pre.target incus.service incus-startup.service" in unit
    assert f"ExecStart=/usr/sbin/nft -f {ingress.FIREWALL_PATH}" in unit
    # Stopping the unit must not lift the default-deny.
    assert "ExecStop" not in unit


def test_plan_install_without_managed_wireguard_installs_only_the_firewall():
    cfg = _cfg(interfaces=["tailscale0"])
    plan = ingress.plan_install(cfg, "/usr/sbin/nft", None)

    assert [f.path for f in plan.files] == [ingress.FIREWALL_PATH, ingress.UNIT_PATH]
    assert plan.files[0].shown == ingress.render_firewall(cfg)
    assert not any("wg-quick" in " ".join(c) for c in plan.commands)


def test_plan_install_keeps_the_host_key_out_of_what_is_shown():
    cfg = _cfg(managed=True, clients={"laptop": {"public_key": CLIENT_KEY, "address": "10.99.0.2"}})
    wg = ingress.plan_install(cfg, "/usr/sbin/nft", HOST_KEY).files[-1]

    assert (wg.path, wg.mode) == ("/etc/wireguard/asbx0.conf", "0600")
    assert HOST_KEY not in wg.shown
    assert f"PrivateKey = {HOST_KEY}" in wg.secret
    assert f"PublicKey = {CLIENT_KEY}\nAllowedIPs = 10.99.0.2/32" in wg.secret
    assert ["systemctl", "restart", "wg-quick@asbx0"] in ingress.plan_install(cfg, "nft", HOST_KEY).commands


# ---------------------------------------------------------------------------
# install (sudo shell)
# ---------------------------------------------------------------------------

@pytest.fixture
def sudo_log(monkeypatch, host_ifaces, config_root):
    log = {"files": {}, "commands": []}
    monkeypatch.setattr(ingress.host, "sudo_install_file",
                        lambda dest, body, mode: log["files"].__setitem__(dest, body))
    monkeypatch.setattr(ingress.host, "sudo", lambda argv: log["commands"].append(argv))
    monkeypatch.setattr(ingress.host, "which", lambda name: True)
    monkeypatch.setattr(ingress.host, "wg_genkey", lambda: HOST_KEY)
    monkeypatch.setattr(ingress.host, "system_binary", lambda name: f"/usr/sbin/{name}")
    return log


def test_install_print_only_changes_nothing_and_creates_no_key(sudo_log, capsys):
    ingress.install(_cfg(managed=True), yes=True, print_only=True)

    assert sudo_log == {"files": {}, "commands": []}
    assert not ingress.wireguard_key_path().exists()
    assert HOST_KEY not in capsys.readouterr().out


def test_install_generates_the_host_key_once_and_installs_it_unprinted(sudo_log, capsys):
    ingress.install(_cfg(managed=True), yes=True, print_only=False)

    assert ingress.wireguard_key_path().read_text() == HOST_KEY + "\n"
    assert f"PrivateKey = {HOST_KEY}" in sudo_log["files"]["/etc/wireguard/asbx0.conf"]
    assert ["systemctl", "restart", ingress.UNIT_NAME] in sudo_log["commands"]
    assert HOST_KEY not in capsys.readouterr().out


def test_install_needs_wireguard_tools_for_managed_wireguard(sudo_log, monkeypatch):
    monkeypatch.setattr(ingress.host, "which", lambda name: name != "wg-quick")

    with pytest.raises(AsbxError, match="wireguard-tools"):
        ingress.install(_cfg(managed=True), yes=True, print_only=False)
    assert sudo_log["files"] == {} and not ingress.wireguard_key_path().exists()


def test_install_refuses_ineligible_interfaces_before_any_sudo(sudo_log):
    with pytest.raises(AsbxError, match="enp5s0"):
        ingress.install(_cfg(interfaces=["enp5s0"]), yes=True, print_only=False)
    assert sudo_log == {"files": {}, "commands": []}


def test_install_declined_installs_nothing(sudo_log, monkeypatch):
    monkeypatch.setattr(ingress.ui, "confirm", lambda q: False)

    with pytest.raises(AsbxError, match="aborted"):
        ingress.install(_cfg(), yes=False, print_only=False)
    assert sudo_log == {"files": {}, "commands": []}


# ---------------------------------------------------------------------------
# cmd_ingress_setup --adopt / --create
# ---------------------------------------------------------------------------

def test_adopt_refuses_the_lan_nic(sudo_log):
    with pytest.raises(AsbxError, match="cannot adopt enp42s0: it carries the host's default route"):
        ingress.cmd_ingress_setup(FakeIncus(), _cfg(), create=False, adopt="enp42s0",
                                  print_only=False, yes=True)
    assert sudo_log == {"files": {}, "commands": []}


def test_adopt_records_the_interface_in_config_then_installs_rules_for_it(sudo_log, config_root):
    config.config_path().write_text("guest_user: agent\n")

    ingress.cmd_ingress_setup(FakeIncus(), _cfg(), create=False, adopt="tailscale0",
                              print_only=False, yes=True)

    assert config.load_config()["network"]["ingress"]["interfaces"] == ["tailscale0"]
    assert '{ "tailscale0" }' in sudo_log["files"][ingress.FIREWALL_PATH]


def test_adopt_print_only_leaves_config_alone_and_prints_rules_for_the_interface(sudo_log, config_root, capsys):
    config.config_path().write_text("guest_user: agent\n")

    ingress.cmd_ingress_setup(FakeIncus(), _cfg(), create=False, adopt="tailscale0",
                              print_only=True, yes=True)

    assert config.config_path().read_text() == "guest_user: agent\n"
    out = capsys.readouterr().out
    assert "interfaces:\n    - tailscale0" in out and '{ "tailscale0" }' in out


def test_create_enables_managed_wireguard_and_installs_its_config(sudo_log, config_root):
    config.config_path().write_text("guest_user: agent\n")
    fake = FakeIncus()
    fake.stub("network", "get", "ipv4.address", stdout="10.210.86.1/24\n")

    ingress.cmd_ingress_setup(fake, _cfg(), create=True, adopt=None, print_only=False, yes=True)

    assert config.load_config()["network"]["ingress"]["wireguard"]["enabled"] is True
    assert "/etc/wireguard/asbx0.conf" in sudo_log["files"]
    assert 'iifname "asbx0" drop' in sudo_log["files"][ingress.FIREWALL_PATH]


def test_adopt_picks_from_the_listed_candidates(sudo_log, config_root, monkeypatch):
    config.config_path().write_text("guest_user: agent\n")
    monkeypatch.setattr(ingress.ui, "choose", lambda q, options: options[-1])

    ingress.cmd_ingress_setup(FakeIncus(), _cfg(), create=False, adopt="", print_only=False, yes=True)

    assert config.load_config()["network"]["ingress"]["interfaces"] == ["wg1"]


def test_create_refuses_a_tunnel_range_that_overlaps_the_bridge(sudo_log, config_root):
    cfg = _cfg()
    cfg["network"]["ingress"]["wireguard"]["address"] = "10.210.86.200/24"
    fake = FakeIncus()
    fake.stub("network", "get", "ipv4.address", stdout="10.210.86.1/24\n")

    with pytest.raises(AsbxError, match="overlaps"):
        ingress.cmd_ingress_setup(fake, cfg, create=True, adopt=None, print_only=False, yes=True)
    assert sudo_log == {"files": {}, "commands": []}


# ---------------------------------------------------------------------------
# cmd_client_config
# ---------------------------------------------------------------------------

def test_client_config_routes_only_the_bridge_subnet_to_the_host(monkeypatch, config_root, capsys):
    ingress.wireguard_key_path().write_text(HOST_KEY + "\n")
    monkeypatch.setattr(ingress.host, "wg_pubkey", lambda key: "HOSTPUB" if key == HOST_KEY else "?")
    fake = FakeIncus()
    fake.stub("network", "get", "ipv4.address", stdout="10.210.86.1/24\n")
    cfg = _cfg(managed=True, endpoint="host.example:51820",
               clients={"laptop": {"public_key": CLIENT_KEY, "address": "10.99.0.2"}})

    ingress.cmd_client_config(fake, cfg, "laptop")

    out = capsys.readouterr().out
    assert "Address = 10.99.0.2/32" in out
    assert "PublicKey = HOSTPUB\nEndpoint = host.example:51820\nAllowedIPs = 10.210.86.0/24" in out
    assert HOST_KEY not in out


def test_client_config_requires_an_endpoint(config_root):
    cfg = _cfg(managed=True, clients={"laptop": {"public_key": CLIENT_KEY, "address": "10.99.0.2"}})

    with pytest.raises(AsbxError, match="endpoint is empty"):
        ingress.cmd_client_config(FakeIncus(), cfg, "laptop")


@pytest.mark.parametrize("managed, client, key, match", [
    (False, "laptop", True, "managed WireGuard is off"),
    (True, "phone", True, "no client 'phone'.*configured: laptop"),
    (True, "laptop", False, "no host WireGuard key"),
])
def test_client_config_refuses(config_root, managed, client, key, match):
    if key:
        ingress.wireguard_key_path().write_text(HOST_KEY + "\n")
    cfg = _cfg(managed=managed, endpoint="host.example:51820",
               clients={"laptop": {"public_key": CLIENT_KEY, "address": "10.99.0.2"}})

    with pytest.raises(AsbxError, match=match):
        ingress.cmd_client_config(FakeIncus(), cfg, client)


def test_install_refuses_a_host_key_file_that_is_not_a_wireguard_key(sudo_log):
    # wg-quick runs its config as root; a crafted key file must not smuggle in PostUp lines.
    ingress.wireguard_key_path().write_text(f"{HOST_KEY}\nPostUp = touch /tmp/pwned\n")

    with pytest.raises(AsbxError, match="not a WireGuard private key"):
        ingress.install(_cfg(managed=True), yes=True, print_only=False)
    assert sudo_log == {"files": {}, "commands": []}
