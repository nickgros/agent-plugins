from __future__ import annotations

import ipaddress

import pytest
import yaml

from fake_incus import FakeIncus

from asbxlib import config, netacl
from asbxlib.errors import AsbxError

# ---------------------------------------------------------------------------
# compute_acl_rules
# ---------------------------------------------------------------------------

BLOCK_CIDRS = [
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "169.254.0.0/16",
    "fc00::/7",
    "fe80::/10",
]


def _nets(cidrs):
    return [ipaddress.ip_network(c) for c in cidrs]


def test_compute_acl_rules_excludes_exactly_the_bridge_subnet_from_covering_cidr():
    rules = netacl.compute_acl_rules("10.210.86.1/24", BLOCK_CIDRS)
    lan_nets = _nets(rules["lan_drops"])
    bridge_subnet = ipaddress.ip_network("10.210.86.0/24")
    covering = ipaddress.ip_network("10.0.0.0/8")

    pieces = [n for n in lan_nets if n.version == 4 and n.subnet_of(covering)]
    assert all(not p.overlaps(bridge_subnet) for p in pieces)
    # The hole is the bridge subnet and nothing wider.
    assert (sum(p.num_addresses for p in pieces)
            == covering.num_addresses - bridge_subnet.num_addresses)
    for probe in ("10.210.85.255", "10.210.87.0", "10.0.0.5", "172.20.0.5", "192.168.1.10",
                  "169.254.1.1"):
        addr = ipaddress.ip_address(probe)
        assert any(addr in n for n in lan_nets), f"{probe} not covered by any lan_drop"
    for guest in ("10.210.86.1", "10.210.86.5"):
        addr = ipaddress.ip_address(guest)
        assert not any(addr in n for n in lan_nets), f"{guest} wrongly covered by a lan_drop"


def test_compute_acl_rules_intra_bridge_drops_cover_peers_but_spare_the_gateway():
    rules = netacl.compute_acl_rules("10.210.86.1/24", BLOCK_CIDRS)
    assert rules["gateway"] == "10.210.86.1"
    intra_nets = _nets(rules["intra_bridge_drops"])
    assert not any(ipaddress.ip_address(rules["gateway"]) in n for n in intra_nets)
    assert any(ipaddress.ip_address("10.210.86.5") in n for n in intra_nets)
    # The whole /24 minus the single gateway address.
    assert sum(n.num_addresses for n in intra_nets) == 255


def test_compute_acl_rules_gateway_ports_leave_only_dns_open():
    rules = netacl.compute_acl_rules("10.210.86.1/24", BLOCK_CIDRS)
    covered: dict[str, set[int]] = {}
    for rule in rules["gateway_port_rules"]:
        spec = rule["destination_port"]
        # incus rejects comma-separated port lists in a single rule.
        assert "," not in spec
        low, high = (int(p) for p in spec.split("-"))
        assert not low <= 53 <= high, f"rule {spec} spans the DNS port"
        covered.setdefault(rule["protocol"], set()).update(range(low, high + 1))
    assert set(covered) == {"tcp", "udp"}
    for proto, ports in covered.items():
        assert ports == set(range(1, 65536)) - {53}, f"{proto} leaves more than DNS reachable"


def test_compute_acl_rules_public_bridge_subnet_needs_no_exclusion():
    block_cidrs = ["10.0.0.0/8", "192.168.0.0/16"]
    rules = netacl.compute_acl_rules("203.0.113.1/24", block_cidrs)
    assert rules["lan_drops"] == block_cidrs
    assert rules["gateway"] == "203.0.113.1"
    intra_nets = _nets(rules["intra_bridge_drops"])
    assert sum(n.num_addresses for n in intra_nets) == 255
    assert not any(ipaddress.ip_address("203.0.113.1") in n for n in intra_nets)


def test_compute_acl_rules_ipv6_ranges_dropped_whole_without_exclusion():
    rules = netacl.compute_acl_rules(
        "10.210.86.1/24", BLOCK_CIDRS, bridge_cidr6="fd42:1:2:3::1/64"
    )
    v6 = [c for c in rules["lan_drops"] if ipaddress.ip_network(c).version == 6]
    # fc00::/7 already covers the bridge's ULA subnet: no extra rule, and no
    # address_exclude explosion (which would be ~50 rules for a /64 hole).
    assert v6 == ["fc00::/7", "fe80::/10"]


def test_compute_acl_rules_adds_bridge_ipv6_subnet_when_no_block_cidr_covers_it():
    rules = netacl.compute_acl_rules(
        "10.210.86.1/24", ["10.0.0.0/8", "fe80::/10"], bridge_cidr6="2001:db8:1:2::1/64"
    )
    v6 = [c for c in rules["lan_drops"] if ipaddress.ip_network(c).version == 6]
    assert v6 == ["fe80::/10", "2001:db8:1:2::/64"]


# ---------------------------------------------------------------------------
# render_acl_yaml / acl_rules_match — gates doctor's drift check
# ---------------------------------------------------------------------------

def _acl_show_output(body: str, **extra) -> str:
    """What `incus network acl show` returns: extra top-level fields, and no
    guarantee about rule order or key order within a rule."""
    parsed = yaml.safe_load(body)
    shown = {
        "name": "asbx-egress",
        "config": {},
        "used_by": ["/1.0/instances/sandbox-demo"],
        "ingress": parsed["ingress"],
        "egress": [dict(reversed(list(r.items()))) for r in reversed(parsed["egress"])],
    }
    shown.update(extra)
    return yaml.safe_dump(shown, sort_keys=False)


def test_render_acl_yaml_round_trips_through_acl_rules_match():
    rules = netacl.compute_acl_rules("10.210.86.1/24", BLOCK_CIDRS,
                                      bridge_cidr6="fd42:1:2:3::1/64")
    body = netacl.render_acl_yaml(rules)
    assert netacl.acl_rules_match(body, _acl_show_output(body))


def test_acl_rules_match_false_when_a_rule_differs():
    body = netacl.render_acl_yaml(netacl.compute_acl_rules("10.210.86.1/24", BLOCK_CIDRS))
    drifted = yaml.safe_load(body)
    drifted["egress"][0]["destination"] = "8.8.8.8/32"
    assert not netacl.acl_rules_match(body, yaml.safe_dump(drifted))


def test_acl_rules_match_false_when_a_rule_is_missing():
    body = netacl.render_acl_yaml(netacl.compute_acl_rules("10.210.86.1/24", BLOCK_CIDRS))
    drifted = yaml.safe_load(body)
    drifted["egress"].pop()
    assert not netacl.acl_rules_match(body, yaml.safe_dump(drifted))


def test_render_acl_yaml_targets_the_gateway_for_every_port_rule():
    rules = netacl.compute_acl_rules("10.210.86.1/24", BLOCK_CIDRS)
    egress = yaml.safe_load(netacl.render_acl_yaml(rules))["egress"]
    port_rules = [r for r in egress if "destination_port" in r]
    assert len(port_rules) == len(rules["gateway_port_rules"])
    assert {r["destination"] for r in port_rules} == {f"{rules['gateway']}/32"}
    assert {r["action"] for r in egress} == {"drop"}


def test_default_block_cidrs_drop_the_tailscale_cgnat_range():
    rules = netacl.compute_acl_rules("10.210.86.1/24", config.DEFAULT_CONFIG["network"]["block_cidrs"])
    lan_nets = _nets(rules["lan_drops"])
    assert any(ipaddress.ip_address("100.100.100.100") in n for n in lan_nets)


# ---------------------------------------------------------------------------
# apply_nic_acl — attaches the ACL to the NIC or raises; never weakens
# ---------------------------------------------------------------------------

def _acl_cfg():
    return {"network": {"name": "incusbr0", "acl": "asbx-egress", "block_cidrs": BLOCK_CIDRS}}


ACL_OPTIONS = ["security.acls=asbx-egress",
               "security.acls.default.ingress.action=allow",
               "security.acls.default.egress.action=allow"]


def test_apply_nic_acl_overrides_the_profile_nic_and_records_the_mode():
    fake = FakeIncus()

    netacl.apply_nic_acl(fake, "sandbox-demo", _acl_cfg())

    assert ["config", "device", "override", "sandbox-demo", "eth0", *ACL_OPTIONS] in fake.calls
    assert ["config", "set", "sandbox-demo", "user.asbx.egress_mode", "acl"] in fake.calls
    assert fake.pushed == {}


def test_apply_nic_acl_updates_the_nic_in_place_when_it_is_already_instance_local():
    fake = FakeIncus()
    fake.stub("device", "override", rc=1, stderr="Error: The device already exists")

    netacl.apply_nic_acl(fake, "sandbox-demo", _acl_cfg())

    assert ["config", "device", "set", "sandbox-demo", "eth0", *ACL_OPTIONS] in fake.calls
    assert ["config", "set", "sandbox-demo", "user.asbx.egress_mode", "acl"] in fake.calls


def test_apply_nic_acl_raises_and_records_nothing_when_incus_rejects_the_acl():
    fake = FakeIncus()
    fake.stub("device", "override", rc=1, stderr="Error: Invalid device option")
    fake.stub("device", "set", rc=1, stderr='Error: Invalid device option "security.acls"')

    with pytest.raises(AsbxError, match="Invalid device option"):
        netacl.apply_nic_acl(fake, "sandbox-demo", _acl_cfg())

    assert not any(c[:2] == ["config", "set"] for c in fake.calls)
    assert fake.pushed == {}


# ---------------------------------------------------------------------------
# Incus version gate
# ---------------------------------------------------------------------------

def test_parse_incus_version_reads_server_version_from_incus_info():
    info = 'environment:\n  server: incus\n  server_version: 6.0.0\n  storage: dir\n'
    assert netacl.parse_incus_version(info) == (6, 0, 0)
    assert netacl.parse_incus_version('  server_version: "7.0"\n') == (7, 0, 0)
    assert netacl.parse_incus_version("driver: qemu\n") is None


@pytest.mark.parametrize("version,supported", [
    ((6, 0, 0), False), ((6, 0, 3), False), ((6, 0, 4), True), ((6, 0, 6), True),
    ((6, 8, 0), False), ((6, 9, 0), False), ((6, 10, 0), True), ((6, 23, 0), True),
    ((7, 0, 0), True), ((7, 5, 1), True),
])
def test_bridged_nic_acl_supported_boundaries(version, supported):
    assert netacl.bridged_nic_acl_supported(version) is supported
