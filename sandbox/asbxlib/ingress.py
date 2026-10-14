"""Host-enforced ingress (ADR 0005): the always-on `inet asbx` nft table that
denies new connections into the bridge except from ingress interfaces, the
systemd unit that loads it at boot, interface eligibility, the asbx-managed
WireGuard interface, and `asbx ingress setup` / `asbx client-config`."""
from __future__ import annotations

import copy
import ipaddress
import shutil
from pathlib import Path
from typing import Optional

import yaml

from .errors import AsbxError
from .incus import Incus
from . import config
from . import host
from . import ui

FIREWALL_PATH = "/etc/asbx/firewall.nft"
UNIT_NAME = "asbx-firewall.service"
UNIT_PATH = f"/etc/systemd/system/{UNIT_NAME}"
HEADER = "# managed by asbx — do not edit; rewritten by `asbx ingress setup`\n"

IFF_TUN = 0x0001
IFF_TAP = 0x0002


def _key_placeholder() -> str:
    return f"<the private key in {wireguard_key_path()}>"


def managed_interface(cfg: dict) -> Optional[str]:
    wg = cfg["network"]["ingress"]["wireguard"]
    return wg["interface"] if wg["enabled"] else None


def ingress_interfaces(cfg: dict) -> list[str]:
    """Adopted interfaces, then the managed WireGuard one, without duplicates."""
    managed = managed_interface(cfg)
    names = list(cfg["network"]["ingress"]["interfaces"])
    return names + [managed] if managed and managed not in names else names


# ---------------------------------------------------------------------------
# Interface eligibility: only interfaces whose peers are authenticated
# cryptographically may be ingress. Adopting the LAN NIC would make the whole
# LAN "authenticated" (pure computation over sysfs facts)
# ---------------------------------------------------------------------------

def interface_problem(name: str, facts: Optional[host.InterfaceFacts], bridge: str,
                      default_route_ifaces: set[str], managed: bool) -> Optional[str]:
    """Why `name` can't be an ingress interface, or None when it can."""
    if name == bridge:
        return "is the sandbox bridge"
    if facts is None:
        # The managed WireGuard interface only exists once wg-quick brings it up.
        return None if managed else "does not exist"
    if facts.is_bridge or facts.devtype == "bridge":
        return "is a bridge"
    if name in default_route_ifaces:
        return "carries the host's default route"
    if facts.devtype == "wireguard":
        return None
    if facts.tun_flags is not None:
        if facts.tun_flags & IFF_TAP:
            return "is a tap device (VM NICs are taps)"
        if facts.tun_flags & IFF_TUN:
            return None
    return f"is not a WireGuard or tun interface ({facts.devtype or 'ethernet-like'})"


def interface_problems(cfg: dict) -> dict[str, str]:
    """Every configured ingress interface that isn't eligible, with why."""
    bridge = cfg["network"]["name"]
    default_ifaces = host.default_route_interfaces()
    managed = managed_interface(cfg)
    problems = {}
    for name in ingress_interfaces(cfg):
        why = interface_problem(name, host.interface_facts(name), bridge, default_ifaces, name == managed)
        if why:
            problems[name] = why
    return problems


def eligible_candidates(cfg: dict) -> list[str]:
    """Host interfaces `--adopt` could pick: eligible and not yet configured."""
    bridge = cfg["network"]["name"]
    default_ifaces = host.default_route_interfaces()
    configured = set(ingress_interfaces(cfg))
    return [n for n in host.interface_names() if n not in configured
            and interface_problem(n, host.interface_facts(n), bridge, default_ifaces, False) is None]


# ---------------------------------------------------------------------------
# Rendered host files (pure computation)
# ---------------------------------------------------------------------------

def render_firewall(cfg: dict) -> str:
    """The `inet asbx` table. Loading the file replaces the table atomically.
    A drop in any nft table wins over Incus's own accepts, so this works
    alongside them; replies to guest egress stay allowed by the ct rule."""
    bridge = cfg["network"]["name"]
    ingress = ingress_interfaces(cfg)
    managed = managed_interface(cfg)

    forward = [f'oifname "{bridge}" ct state established,related accept']
    if ingress:
        names = ", ".join(f'"{n}"' for n in ingress)
        forward.append(f'oifname "{bridge}" iifname {{ {names} }} meta nfproto ipv4 accept')
    forward.append(f'oifname "{bridge}" drop')
    input_rules = []
    if managed:
        # Nothing else uses the managed interface, so its peers reach the
        # bridge and nothing else: not the host, not the LAN, and nothing may
        # open connections towards them.
        forward += [f'iifname "{managed}" drop',
                    f'oifname "{managed}" ct state established,related accept',
                    f'oifname "{managed}" drop']
        input_rules = [f'iifname "{managed}" ct state established,related accept',
                       f'iifname "{managed}" drop']

    lines = [HEADER.rstrip("\n"), "table inet asbx", "delete table inet asbx", "table inet asbx {",
             "\tchain forward {", "\t\ttype filter hook forward priority filter; policy accept;"]
    lines += [f"\t\t{r}" for r in forward]
    lines.append("\t}")
    if input_rules:
        lines += ["\tchain input {", "\t\ttype filter hook input priority filter; policy accept;"]
        lines += [f"\t\t{r}" for r in input_rules]
        lines.append("\t}")
    lines.append("}")
    return "\n".join(lines) + "\n"


def render_unit(nft_path: str) -> str:
    """Loads the table before Incus starts any instance. No ExecStop: stopping
    the unit must not lift the default-deny."""
    return (
        HEADER
        + "[Unit]\n"
        "Description=asbx sandbox ingress firewall (nft table inet asbx)\n"
        "DefaultDependencies=no\n"
        "Wants=network-pre.target\n"
        "Before=network-pre.target incus.service incus-startup.service shutdown.target\n"
        "Conflicts=shutdown.target\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "RemainAfterExit=yes\n"
        f"ExecStart={nft_path} -f {FIREWALL_PATH}\n"
        f"ExecReload={nft_path} -f {FIREWALL_PATH}\n"
        "\n"
        "[Install]\n"
        "WantedBy=sysinit.target\n"
    )


def render_wireguard(cfg: dict, private_key: str) -> str:
    wg = cfg["network"]["ingress"]["wireguard"]
    out = (HEADER + "[Interface]\n"
           f"Address = {wg['address']}\n"
           f"ListenPort = {wg['listen_port']}\n"
           f"PrivateKey = {private_key}\n")
    for name, client in wg["clients"].items():
        out += (f"\n# client: {name}\n[Peer]\n"
                f"PublicKey = {client['public_key']}\n"
                f"AllowedIPs = {client['address']}/32\n")
    return out


def render_client(cfg: dict, name: str, host_public_key: str, bridge_subnet: str) -> str:
    wg = cfg["network"]["ingress"]["wireguard"]
    client = wg["clients"][name]
    return (f"# asbx ingress client '{name}'. Use with wg-quick or the WireGuard app.\n"
            "[Interface]\n"
            f"PrivateKey = <this machine's private key, the one whose public key is "
            f"network.ingress.wireguard.clients.{name}.public_key>\n"
            f"Address = {client['address']}/32\n"
            "\n[Peer]\n"
            f"PublicKey = {host_public_key}\n"
            f"Endpoint = {wg['endpoint']}\n"
            f"AllowedIPs = {bridge_subnet}\n"
            "PersistentKeepalive = 25\n")


def nft_path() -> str:
    return shutil.which("nft") or "/usr/sbin/nft"


# ---------------------------------------------------------------------------
# Status, used by `up` (fail closed) and `doctor`
# ---------------------------------------------------------------------------

def firewall_installed_matches(cfg: dict) -> bool:
    """Unprivileged asbx can't list nftables, so the live table is inferred:
    the installed file equals what config renders and the unit is active."""
    return host.read_text(FIREWALL_PATH) == render_firewall(cfg)


def firewall_unit_active() -> bool:
    return host.systemctl_system("is-active", UNIT_NAME).returncode == 0


def require_firewall(cfg: dict) -> None:
    """Raises unless the ingress firewall is in force for the current config."""
    problems = interface_problems(cfg)
    if problems:
        detail = "; ".join(f"{n} {why}" for n, why in problems.items())
        raise AsbxError(f"ineligible ingress interface: {detail}; fix network.ingress in "
                        f"{config.config_path()} (see docs/security.md)")
    if not firewall_installed_matches(cfg):
        raise AsbxError(f"{FIREWALL_PATH} is missing or out of date for this config; "
                        "run 'asbx ingress setup' to install the ingress firewall")
    if not firewall_unit_active():
        raise AsbxError(f"{UNIT_NAME} is not active; run 'asbx ingress setup'")


# ---------------------------------------------------------------------------
# Install (sudo)
# ---------------------------------------------------------------------------

def wireguard_key_path() -> Path:
    return config.config_dir() / "wireguard.key"


def _require_wireguard_tools() -> None:
    if not (host.which("wg") and host.which("wg-quick")):
        raise AsbxError("managed WireGuard needs wireguard-tools (`wg`, `wg-quick`) on the host")


def _host_private_key(create: bool) -> Optional[str]:
    path = wireguard_key_path()
    if path.exists():
        return path.read_text().strip()
    if not create:
        return None
    key = host.wg_genkey()
    host.write_private_text(path, key + "\n")
    ui.ok(f"generated {path}")
    return key


def install(cfg: dict, yes: bool, print_only: bool) -> None:
    """Installs the firewall table and unit, plus the managed WireGuard config
    when enabled, through sudo after showing them. `print_only` changes nothing."""
    problems = interface_problems(cfg)
    if problems:
        raise AsbxError("refusing ineligible ingress interfaces: "
                        + "; ".join(f"{n} {why}" for n, why in problems.items()))
    managed = managed_interface(cfg)
    files = [(FIREWALL_PATH, render_firewall(cfg), "0644", None),
             (UNIT_PATH, render_unit(nft_path()), "0644", None)]
    commands = [["systemctl", "daemon-reload"],
                ["systemctl", "enable", UNIT_NAME],
                ["systemctl", "restart", UNIT_NAME]]
    if managed:
        if not print_only:
            _require_wireguard_tools()
        key = None if print_only else _host_private_key(create=True)
        files.append((f"/etc/wireguard/{managed}.conf", render_wireguard(cfg, _key_placeholder()), "0600",
                      key and render_wireguard(cfg, key)))
        commands += [["systemctl", "enable", f"wg-quick@{managed}"],
                     ["systemctl", "restart", f"wg-quick@{managed}"]]

    for path, shown, mode, _secret in files:
        print(f"# --- {path} (mode {mode}) ---\n{shown}")
    print("# --- then, as root ---")
    for argv in commands:
        print(" ".join(argv))
    if print_only:
        return
    if not yes and not ui.confirm("install these as root with sudo? [y/N] "):
        raise AsbxError("aborted; 'asbx up' refuses to run until the ingress firewall is installed")
    for path, shown, mode, secret in files:
        host.sudo_install_file(path, secret or shown, mode)
    for argv in commands:
        host.sudo(argv)
    ui.ok(f"ingress firewall active ({', '.join(ingress_interfaces(cfg)) or 'no ingress interfaces'})")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _bridge_subnet(incus: Incus, cfg: dict) -> ipaddress.IPv4Network:
    return ipaddress.IPv4Interface(incus.network_get(cfg["network"]["name"], "ipv4.address")).network


def _set_ingress(cfg: dict, keys: tuple[str, ...], value, print_only: bool) -> dict:
    """Records a config change, or with `print_only` prints the YAML to add,
    and returns the config to install from."""
    full_keys = ("network", "ingress", *keys)
    if not print_only:
        config.set_config_value(full_keys, value)
        ui.ok(f"set network.ingress.{'.'.join(keys)} in {config.config_path()}")
        return config.load_config()
    print(f"# --- add to {config.config_path()} ---\n"
          f"{yaml.safe_dump(config.nest(full_keys, value), sort_keys=False)}")
    cfg = copy.deepcopy(cfg)
    node = cfg
    for k in full_keys[:-1]:
        node = node[k]
    node[full_keys[-1]] = value
    return cfg


def cmd_ingress_setup(incus: Incus, cfg: dict, create: bool, adopt: Optional[str],
                      print_only: bool, yes: bool) -> None:
    if adopt is not None:
        name = adopt
        if not name:
            candidates = eligible_candidates(cfg)
            if not candidates:
                raise AsbxError("no WireGuard or tun interface to adopt on this host")
            try:
                name = ui.choose("interface to adopt: ", candidates)
            except ValueError:
                raise AsbxError("aborted")
        why = interface_problem(name, host.interface_facts(name), cfg["network"]["name"],
                                host.default_route_interfaces(), managed=False)
        if why:
            raise AsbxError(f"cannot adopt {name}: it {why}")
        interfaces = cfg["network"]["ingress"]["interfaces"]
        if name not in interfaces:
            if not yes and not ui.confirm(
                    f"adopt {name}? Every peer that can send traffic on it will reach every port "
                    "on every sandbox. [y/N] "):
                raise AsbxError("aborted")
            cfg = _set_ingress(cfg, ("interfaces",), [*interfaces, name], print_only)

    if create:
        if not print_only:
            _require_wireguard_tools()
        wg = cfg["network"]["ingress"]["wireguard"]
        tunnel = ipaddress.IPv4Interface(wg["address"]).network
        if tunnel.overlaps(_bridge_subnet(incus, cfg)):
            raise AsbxError(f"network.ingress.wireguard.address {wg['address']} overlaps the "
                            f"{cfg['network']['name']} subnet; pick another range")
        if not wg["enabled"]:
            cfg = _set_ingress(cfg, ("wireguard", "enabled"), True, print_only)

    install(cfg, yes=yes, print_only=print_only)


def cmd_client_config(incus: Incus, cfg: dict, name: str) -> None:
    wg = cfg["network"]["ingress"]["wireguard"]
    if not wg["enabled"]:
        raise AsbxError("managed WireGuard is off; run 'asbx ingress setup --create' first")
    if name not in wg["clients"]:
        known = ", ".join(wg["clients"]) or "none"
        raise AsbxError(f"no client '{name}' in network.ingress.wireguard.clients (configured: {known})")
    if not wg["endpoint"]:
        raise AsbxError("network.ingress.wireguard.endpoint is empty; set it to the host:port "
                        "this client reaches the host on")
    key = _host_private_key(create=False)
    if key is None:
        raise AsbxError(f"no host WireGuard key at {wireguard_key_path()}; run 'asbx ingress setup --create'")
    print(render_client(cfg, name, host.wg_pubkey(key), str(_bridge_subnet(incus, cfg))), end="")
