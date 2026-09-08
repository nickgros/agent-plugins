from __future__ import annotations

import ipaddress
from typing import Optional

import yaml

from .errors import AsbxError
from .incus import Incus
from . import ui


# ---------------------------------------------------------------------------
# Network ACL: block the host LAN, allow the internet (pure computation)
# ---------------------------------------------------------------------------

def compute_acl_rules(bridge_cidr: str, block_cidrs: list[str],
                      bridge_cidr6: Optional[str] = None) -> dict:
    """`bridge_cidr` is the bridge's own IPv4 address/prefixlen, e.g.
    '10.210.86.1/24' (not the network address), and is also the gateway;
    `bridge_cidr6` is the same for IPv6 when the bridge has one.

    Returns {'lan_drops': [cidr,...], 'intra_bridge_drops': [cidr,...],
    'gateway': addr, 'gateway_port_rules': [{'protocol','destination_port'},...]}.

    Incus sorts ACL rules by action (drop, then reject, then allow) and ignores
    list order, so a carve-out cannot be expressed as an allow rule inside a
    dropped range — it has to be a hole in the dropped range itself. IPv4 keeps
    exactly one such hole: the bridge subnet is excluded from the blocked
    ranges, re-dropped except the gateway address, and the gateway is dropped on
    every port but 53, leaving the guest DNS and nothing else on the host.

    IPv6 needs no hole (DNS is served over IPv4), so v6 ranges are dropped
    whole: no address_exclude, no rule explosion."""
    iface = ipaddress.ip_interface(bridge_cidr)
    bridge_subnet = iface.network
    gateway = str(iface.ip)
    subnet6 = ipaddress.ip_interface(bridge_cidr6).network if bridge_cidr6 else None

    lan_drops: list[str] = []
    for cidr in block_cidrs:
        net = ipaddress.ip_network(cidr)
        if net.version != bridge_subnet.version:
            lan_drops.append(str(net))
            continue
        if bridge_subnet.subnet_of(net):
            lan_drops += [str(n) for n in net.address_exclude(bridge_subnet)]
        else:
            lan_drops.append(str(net))

    # The bridge's own IPv6 subnet, unless a blocked range already covers it.
    if subnet6 is not None and not any(
        subnet6.subnet_of(ipaddress.ip_network(c))
        for c in block_cidrs if ipaddress.ip_network(c).version == 6
    ):
        lan_drops.append(str(subnet6))

    host_net = ipaddress.ip_network(f"{gateway}/{bridge_subnet.max_prefixlen}")
    intra_bridge_drops = [str(n) for n in bridge_subnet.address_exclude(host_net)]

    gateway_port_rules = [
        {"protocol": proto, "destination_port": ports}
        for proto in ("tcp", "udp")
        for ports in ("1-52", "54-65535")
    ]

    return {
        "lan_drops": lan_drops,
        "intra_bridge_drops": intra_bridge_drops,
        "gateway": gateway,
        "gateway_port_rules": gateway_port_rules,
    }


def render_acl_yaml(rules: dict) -> str:
    egress = []
    for cidr in rules["lan_drops"]:
        egress.append({
            "action": "drop",
            "destination": cidr,
            "state": "enabled",
            "description": "asbx: block host LAN",
        })
    for cidr in rules["intra_bridge_drops"]:
        egress.append({
            "action": "drop",
            "destination": cidr,
            "state": "enabled",
            "description": "asbx: block intra-bridge traffic",
        })
    for rule in rules["gateway_port_rules"]:
        egress.append({
            "action": "drop",
            "destination": f"{rules['gateway']}/32",
            "protocol": rule["protocol"],
            "destination_port": rule["destination_port"],
            "state": "enabled",
            "description": "asbx: block gateway except DNS",
        })
    return yaml.safe_dump({"egress": egress, "ingress": []}, sort_keys=False)


def acl_rules_match(expected_yaml: str, actual_yaml: str) -> bool:
    """Structural comparison of egress rule sets, ignoring key order and
    extra fields incus adds to `network acl show` output (name/config/used_by)."""
    expected = yaml.safe_load(expected_yaml) or {}
    actual = yaml.safe_load(actual_yaml) or {}
    exp_rules = {frozenset(r.items()) for r in expected.get("egress", [])}
    act_rules = {frozenset(r.items()) for r in actual.get("egress", [])}
    return exp_rules == act_rules


def compute_acl_rules_for_network(incus: Incus, cfg: dict) -> dict:
    """Reads the bridge's addresses and computes the egress drop sets. Both
    `ensure_acl` and doctor's drift check go through here so they can never
    compare against differently-derived rules."""
    net_cfg = cfg["network"]
    ipv4 = incus.network_get(net_cfg["name"], "ipv4.address")
    if not ipv4:
        raise AsbxError(f"network {net_cfg['name']} has no ipv4.address configured")
    ipv6 = incus.network_get(net_cfg["name"], "ipv6.address")
    return compute_acl_rules(ipv4, net_cfg["block_cidrs"], bridge_cidr6=ipv6 or None)


def ensure_acl(incus: Incus, cfg: dict) -> None:
    net_cfg = cfg["network"]
    rules = compute_acl_rules_for_network(incus, cfg)
    body = render_acl_yaml(rules)

    if not incus.network_acl_exists(net_cfg["acl"]):
        incus.network_acl_create(net_cfg["acl"])
    incus.network_acl_edit(net_cfg["acl"], body)
    ui.ok(f"network ACL {net_cfg['acl']} up to date ({len(rules['lan_drops'])} LAN drops, "
       f"{len(rules['intra_bridge_drops'])} intra-bridge drops)")


def render_guest_nft(rules: dict) -> str:
    """The /etc/nftables.d/asbx-lan.nft body: one drop per LAN/intra-bridge
    CIDR (ip vs ip6 by family) plus the gateway's non-DNS port drops."""
    nft_lines = ["#!/usr/sbin/nft -f", "table inet asbx {", "  chain output {", "    type filter hook output priority 0;"]
    for cidr in rules["lan_drops"] + rules["intra_bridge_drops"]:
        family = "ip6" if ipaddress.ip_network(cidr).version == 6 else "ip"
        nft_lines.append(f"    {family} daddr {cidr} drop;")
    for rule in rules["gateway_port_rules"]:
        nft_lines.append(f"    ip daddr {rules['gateway']} {rule['protocol']} "
                         f"dport {rule['destination_port']} drop;")
    nft_lines += ["  }", "}"]
    return "\n".join(nft_lines) + "\n"


def apply_nic_acl(incus: Incus, instance: str, cfg: dict) -> str:
    """Applies the egress ACL to the instance's NIC. Returns 'acl' or
    'guest-nft' depending on which egress_mode was used."""
    net_cfg = cfg["network"]
    incus.device_remove(instance, "eth0")
    proc = incus.device_add(
        instance, "eth0", "nic",
        f"network={net_cfg['name']}",
        f"security.acls={net_cfg['acl']}",
        "security.acls.default.ingress.action=allow",
        "security.acls.default.egress.action=allow",
    )
    if proc.returncode == 0:
        incus.config_set(instance, "user.asbx.egress_mode", "acl")
        return "acl"

    ui.warn("bridge ACLs unavailable; falling back to in-guest nftables (agent can disable it)")
    incus.device_add(instance, "eth0", "nic", f"network={net_cfg['name']}")
    rules = compute_acl_rules_for_network(incus, cfg)
    nft_body = render_guest_nft(rules)
    incus.exec_in(instance, ["mkdir", "-p", "/etc/nftables.d"], user="root", check=False)
    incus.push_text(instance, "/etc/nftables.d/asbx-lan.nft", nft_body, mode="0644", uid=0, gid=0)
    include_line = 'include "/etc/nftables.d/*.nft"'
    incus.exec_in(
        instance,
        ["bash", "-c", f"grep -qxF '{include_line}' /etc/nftables.conf || echo '{include_line}' >> /etc/nftables.conf"],
        user="root", check=False,
    )
    incus.exec_in(instance, ["systemctl", "restart", "nftables"], user="root", check=False)
    incus.exec_in(instance, ["systemctl", "enable", "nftables"], user="root", check=False)
    incus.config_set(instance, "user.asbx.egress_mode", "guest-nft")
    return "guest-nft"


def acl_matches_config(incus: Incus, cfg: dict) -> bool:
    """True when the live ACL's egress rules equal the ones the config computes."""
    net_cfg = cfg["network"]
    expected = render_acl_yaml(compute_acl_rules_for_network(incus, cfg))
    proc = incus.network_acl_show(net_cfg["acl"])
    if proc.returncode != 0:
        return False
    return acl_rules_match(expected, proc.stdout)
