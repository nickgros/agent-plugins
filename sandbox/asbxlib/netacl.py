from __future__ import annotations

import ipaddress
import re
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
    ipv6 = (incus.network_get(net_cfg["name"], "ipv6.address") or "").strip()
    if ipv6 == "none":      # Incus's value for "no IPv6 on this bridge"
        ipv6 = ""
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


def apply_nic_acl(incus: Incus, instance: str, cfg: dict) -> None:
    """Attaches the egress ACL to the instance's NIC, or raises. There is no
    weaker fallback: a firewall inside the guest is not a control when the
    agent has root there, so an instance that can't be filtered must not run
    as if it were."""
    net_cfg = cfg["network"]
    options = (
        f"security.acls={net_cfg['acl']}",
        "security.acls.default.ingress.action=allow",
        "security.acls.default.egress.action=allow",
    )
    # eth0 comes from the default profile until it is overridden, after which
    # it is an instance-local device that can only be updated.
    proc = incus.device_override(instance, "eth0", *options)
    if proc.returncode != 0:
        proc = incus.device_set(instance, "eth0", *options)
    if proc.returncode != 0:
        raise AsbxError(
            f"could not attach network ACL {net_cfg['acl']} to {instance}'s NIC "
            f"(bridged-NIC security.acls needs Incus >= 6.0.4 on the 6.0 LTS or >= 6.10; "
            f"run 'asbx doctor'):\n{(proc.stderr or '').strip()}"
        )
    incus.config_set(instance, "user.asbx.egress_mode", "acl")


_SERVER_VERSION_RE = re.compile(r"^\s*server_version:\s*\"?(\d+)\.(\d+)(?:\.(\d+))?", re.MULTILINE)


def parse_incus_version(info: str) -> Optional[tuple[int, int, int]]:
    """The `server_version` from `incus info` output as (major, minor, patch)."""
    m = _SERVER_VERSION_RE.search(info)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)


def bridged_nic_acl_supported(version: tuple[int, int, int]) -> bool:
    """Whether `security.acls` is accepted on a bridged NIC. Read from the
    Incus source (`device/nic_bridged.go`): absent at v6.0.0-v6.0.3, v6.1,
    v6.2, v6.3, v6.5 and v6.8; present at v6.0.4, v6.10, v6.15, v6.20 and
    v7.0. 6.9 was not checked and counts as unsupported; later releases are
    assumed to keep the option."""
    major, minor, patch = version
    if major >= 7:
        return True
    return major == 6 and (minor >= 10 or (minor == 0 and patch >= 4))


def acl_matches_config(incus: Incus, cfg: dict) -> bool:
    """True when the live ACL's egress rules equal the ones the config computes."""
    net_cfg = cfg["network"]
    expected = render_acl_yaml(compute_acl_rules_for_network(incus, cfg))
    proc = incus.network_acl_show(net_cfg["acl"])
    if proc.returncode != 0:
        return False
    return acl_rules_match(expected, proc.stdout)
