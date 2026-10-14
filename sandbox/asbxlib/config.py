from __future__ import annotations

import ipaddress
import os
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from . import host
from .errors import AsbxError

# ---------------------------------------------------------------------------
# Host config: ~/.config/agent-sandbox/config.yaml
# ---------------------------------------------------------------------------

CONFIG_DIR_ENV = "ASBX_CONFIG_DIR"


def config_dir() -> Path:
    override = os.environ.get(CONFIG_DIR_ENV)
    return Path(override) if override else Path.home() / ".config" / "agent-sandbox"


def config_path() -> Path:
    return config_dir() / "config.yaml"


def projects_dir() -> Path:
    return config_dir() / "projects"


def zsh_dir() -> Path:
    return config_dir() / "zsh"


# Login shells a sandbox can use, mapped to their guest path.
SHELL_PATHS: dict[str, str] = {"bash": "/bin/bash", "zsh": "/usr/bin/zsh"}

DEFAULT_CONFIG: dict[str, Any] = {
    "guest_user": "agent",
    "instance_prefix": "sandbox-",
    "image_alias": "agent-sandbox-base",
    "base_image_source": "images:debian/13/cloud",
    "shell": "bash",
    "network": {
        "name": "incusbr0",
        "acl": "asbx-egress",
        # Resolver suffix Incus's managed DNS serves for instances on the
        # bridge (the network's `dns.domain`, `incus` unless changed).
        "dns_domain": "incus",
        "block_cidrs": [
            "10.0.0.0/8",
            "172.16.0.0/12",
            "192.168.0.0/16",
            "169.254.0.0/16",
            # Tailscale's CGNAT range. A host running Tailscale routes it out
            # tailscale0, and bridge masquerade would present the guest as the
            # host's own tailnet identity.
            "100.64.0.0/10",
            "fc00::/7",
            "fe80::/10",
        ],
        "ingress": {
            # Operator-run interfaces (WireGuard or tun, e.g. tailscale0) whose
            # peers may reach any port on sandbox IPs. See docs/security.md.
            "interfaces": [],
            # asbx-managed WireGuard interface, created by `asbx ingress setup --create`.
            "wireguard": {
                "enabled": False,
                "interface": "asbx0",
                # The host's tunnel address; each client gets a /32 inside it.
                "address": "10.99.0.1/24",
                "listen_port": 51820,
                # host:port clients dial. Required by `asbx client-config`.
                "endpoint": "",
                # name -> {public_key, address}; each client generates its own key.
                "clients": {},
            },
        },
    },
    "ssh": {
        "key": "~/.config/agent-sandbox/id_ed25519",
        "config_dir": "~/.ssh/agent-sandbox.d",
        "known_hosts": "~/.config/agent-sandbox/known_hosts",
        # Extra public keys (full authorized_keys lines) installed for the
        # guest user alongside `key`.pub, e.g. a laptop's key.
        "extra_pubkeys": [],
    },
    "defaults": {
        "resources": {"cpu": 4, "memory": "8GiB", "disk": "30GiB"},
        "auth": ["github-gh", "aws-creds"],
        "mounts": [
            {"host": "~/.agents/skills", "guest": "/home/agent/.agents/skills", "mode": "ro"},
        ],
        "copy_binaries": [],
        "env": {},
        "git": {"name": "", "email": ""},
        "remotes": {},
        # mise tool -> version for every sandbox, e.g. {maven: "3.9"}. A manifest's tools wins per tool.
        "tools": {},
    },
    "aws": {
        # Host profiles whose short-lived credentials may be pushed into
        # sandboxes. A manifest's `aws_profiles` adds to this list.
        "profiles": ["sage-bedrock"],
    },
    "harness_env": {
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "AWS_REGION": "us-east-1",
        "AWS_PROFILE": "sage-bedrock",
    },
    "harness_settings": {
        # Host harness files pushed into every sandbox on `up` and
        # `settings-sync`. `sync` takes built-in item names (HARNESS_ITEMS) or
        # {host, guest} entries and must not hold credential items;
        # `credentials` opts in to those. A manifest's `harness_credentials`
        # adds to the latter.
        "sync": ["omp"],
        "credentials": [],
    },
}


# User-keyed maps: any key is valid under these, not just the default ones.
OPEN_MAPS = frozenset({"defaults.env", "defaults.remotes", "defaults.tools", "harness_env",
                       "network.ingress.wireguard.clients"})


def deep_merge(base: dict, override: dict, path: str = "") -> dict:
    result = dict(base)
    for k, v in override.items():
        key_path = f"{path}.{k}" if path else k
        if k not in base:
            raise AsbxError(f"config {config_path()}: unknown key '{key_path}'")
        if key_path in OPEN_MAPS and isinstance(v, dict):
            result[k] = {**base[k], **v}
        elif isinstance(v, dict) and isinstance(base[k], dict):
            result[k] = deep_merge(base[k], v, key_path)
        else:
            result[k] = v
    return result


def normalize_env(env: dict, where: str) -> dict[str, str]:
    """Env values reach shell command lines, so they must be strings. YAML
    parses `PORT: 8080` as an int and `DEBUG: true` as a bool; render those the
    way a shell user would write them."""
    out: dict[str, str] = {}
    for k, v in env.items():
        if isinstance(v, bool):
            out[str(k)] = "true" if v else "false"
        elif isinstance(v, (str, int, float)):
            out[str(k)] = str(v)
        else:
            raise AsbxError(f"{where}.{k}: env values must be scalars, got {type(v).__name__}")
    return out


def normalize_tools(tools: Any, where: str) -> dict[str, str]:
    """mise tool name -> version. YAML parses `java: 21` as an int, which is
    exact. A float is not (`python: 3.10` loads as 3.1), so versions with a dot
    must be quoted strings."""
    if not isinstance(tools, dict):
        raise AsbxError(f"{where} must be a mapping of mise tool name to version")
    out: dict[str, str] = {}
    for k, v in tools.items():
        if not isinstance(k, str) or not k:
            raise AsbxError(f"{where}: tool names must be non-empty strings")
        if isinstance(v, int) and not isinstance(v, bool):
            out[k] = str(v)
        elif isinstance(v, str) and v.strip():
            out[k] = v
        elif isinstance(v, float):
            raise AsbxError(f"{where}.{k}: quote the version (YAML reads {v!r} as a number and drops trailing zeros)")
        else:
            raise AsbxError(f"{where}.{k}: tool versions must be non-empty strings or integers")
    return out


@dataclass(frozen=True)
class HarnessItem:
    root: str  # `~`-relative host dir; the guest uses the same path under its home
    paths: tuple[str, ...]  # relative to root; files or directories
    credential: bool


# Runtime state and per-install secrets are deliberately absent from every
# item: agent.db*, history.db*, models.db*, sessions/, blobs/, cache/,
# secret-placeholder.key, terminal-sessions/, managed-skills/.
HARNESS_ITEMS: dict[str, HarnessItem] = {
    "omp": HarnessItem(
        root="~/.omp/agent",
        paths=(
            "config.yml", "config.yaml", "models.yml", "models.yaml",
            "AGENTS.md", "RULES.md", "SYSTEM.md", "APPEND_SYSTEM.md", "PERSONALITY.md",
            "SYSTEM_TEMPLATE.md", "TITLE_SYSTEM.md", "WATCHDOG.md",
            "keybindings.yml", "lsp.json",
            "rules", "commands", "agents", "hooks", "tools", "extensions", "themes", "skills",
        ),
        credential=False,
    ),
    "omp-mcp": HarnessItem(root="~/.omp/agent", paths=("mcp.json", ".mcp.json"), credential=True),
    "omp-env": HarnessItem(root="~/.omp/agent", paths=(".env",), credential=True),
    "omp-secrets": HarnessItem(root="~/.omp/agent", paths=("secrets.yml",), credential=True),
}

_HARNESS_LIST_RULE = "must be a list of harness item names or {host, guest} mappings"


def validate_harness_list(value: Any, where: str, credentials: bool) -> list:
    """Validates a `harness_settings.sync` / `credentials` / `harness_credentials`
    list and returns it normalized: names stay strings, inline entries become
    {host, guest} dicts with both keys present."""
    if not isinstance(value, list):
        raise AsbxError(f"{where}: {_HARNESS_LIST_RULE}")
    out: list = []
    for entry in value:
        if isinstance(entry, str):
            item = HARNESS_ITEMS.get(entry)
            if item is None:
                raise AsbxError(f"{where}: unknown harness item '{entry}'")
            if item.credential and not credentials:
                raise AsbxError(
                    f"{where}: '{entry}' carries credentials; list it under "
                    "harness_settings.credentials or the manifest's harness_credentials")
            if not item.credential and credentials:
                raise AsbxError(f"{where}: '{entry}' holds no credentials; list it under harness_settings.sync")
            out.append(entry)
            continue
        if not isinstance(entry, dict):
            raise AsbxError(f"{where}: {_HARNESS_LIST_RULE}")
        for k in entry:
            if k not in ("host", "guest"):
                raise AsbxError(f"{where}: unknown key '{k}'")
        host_path = entry.get("host")
        if not isinstance(host_path, str) or not host_path:
            raise AsbxError(f"{where}: 'host' is required and must be a string")
        if not (host_path.startswith("~/") or host_path.startswith("/")):
            raise AsbxError(f"{where}: host '{host_path}' must start with ~/ or /")
        guest_path = entry.get("guest")
        if guest_path is None:
            if not host_path.startswith("~/"):
                raise AsbxError(f"{where}: guest is required when host is not under ~")
            guest_path = host_path
        if not isinstance(guest_path, str) or not guest_path.startswith("~/"):
            raise AsbxError(f"{where}: guest '{guest_path}' must start with ~/")
        if ".." in guest_path.split("/"):
            raise AsbxError(f"{where}: guest '{guest_path}' must not contain '..'")
        rest = posixpath.normpath(guest_path[2:].lstrip("/"))
        if rest == ".":
            raise AsbxError(f"{where}: guest '{guest_path}' must name a path under ~, not ~ itself")
        out.append({"host": host_path, "guest": f"~/{rest}"})
    return out


# An AWS profile name becomes a guest file name and an INI section header.
AWS_PROFILE_RE = re.compile(r"[A-Za-z0-9._-]+")
AWS_PROFILES_RULE = "a list of AWS profile names matching [A-Za-z0-9._-]+"


def is_aws_profile_list(value: Any) -> bool:
    return isinstance(value, list) and all(
        isinstance(p, str) and AWS_PROFILE_RE.fullmatch(p) for p in value)


# Linux interface names: at most 15 bytes (IFNAMSIZ - 1). The name lands in an
# nft ruleset and a systemd unit name, so it is restricted further than the kernel's rule.
IFACE_RE = re.compile(r"[A-Za-z0-9_.-]{1,15}")
# A WireGuard key: 32 bytes in base64, so 43 significant characters and one '='.
WG_KEY_RE = re.compile(r"[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=")
CLIENT_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")


def _ingress_error(key: str, rule: str) -> AsbxError:
    return AsbxError(f"config {config_path()}: 'network.ingress.{key}' {rule}")


def validate_ingress(ingress: dict) -> dict:
    """Validates `network.ingress` and returns a new mapping with duplicate
    interfaces dropped and client entries normalized to {public_key, address}
    strings. The input is never mutated: it may be DEFAULT_CONFIG's own dict."""
    interfaces = ingress["interfaces"]
    if not isinstance(interfaces, list) or not all(
            isinstance(i, str) and IFACE_RE.fullmatch(i) for i in interfaces):
        raise _ingress_error("interfaces", "must be a list of interface names matching [A-Za-z0-9_.-]{1,15}")

    wg = ingress["wireguard"]
    if not isinstance(wg["enabled"], bool):
        raise _ingress_error("wireguard.enabled", "must be true or false")
    if not isinstance(wg["interface"], str) or not IFACE_RE.fullmatch(wg["interface"]):
        raise _ingress_error("wireguard.interface", "must be an interface name matching [A-Za-z0-9_.-]{1,15}")
    try:
        host_if = ipaddress.IPv4Interface(str(wg["address"]))
    except ValueError:
        raise _ingress_error("wireguard.address", "must be an IPv4 address with a prefix, e.g. 10.99.0.1/24")
    subnet = host_if.network
    if subnet.prefixlen > 30 or host_if.ip in (subnet.network_address, subnet.broadcast_address):
        raise _ingress_error("wireguard.address", "must be a host address in a subnet of /30 or larger")
    port = wg["listen_port"]
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise _ingress_error("wireguard.listen_port", "must be a port number from 1 to 65535")
    if not isinstance(wg["endpoint"], str):
        raise _ingress_error("wireguard.endpoint", "must be a host:port string")

    clients: dict[str, dict[str, str]] = {}
    seen_keys: set[str] = set()
    seen_addrs: set[ipaddress.IPv4Address] = {host_if.ip}
    for name, entry in (wg["clients"] or {}).items():
        where = f"wireguard.clients.{name}"
        if not isinstance(name, str) or not CLIENT_NAME_RE.fullmatch(name):
            raise _ingress_error(where, "is not a valid client name ([A-Za-z0-9_.-]+)")
        if not isinstance(entry, dict) or set(entry) != {"public_key", "address"}:
            raise _ingress_error(where, "must be a mapping with exactly 'public_key' and 'address'")
        key = entry["public_key"]
        if not isinstance(key, str) or not WG_KEY_RE.fullmatch(key):
            raise _ingress_error(f"{where}.public_key", "must be a WireGuard public key (`wg pubkey` output)")
        try:
            addr = ipaddress.IPv4Address(str(entry["address"]))
        except ValueError:
            raise _ingress_error(f"{where}.address", "must be an IPv4 address")
        if addr not in subnet or addr in (subnet.network_address, subnet.broadcast_address):
            raise _ingress_error(f"{where}.address", f"must be a host address inside {subnet}")
        if addr in seen_addrs:
            raise _ingress_error(f"{where}.address", f"{addr} is already used by the host or another client")
        if key in seen_keys:
            raise _ingress_error(f"{where}.public_key", "is already used by another client")
        seen_keys.add(key)
        seen_addrs.add(addr)
        clients[name] = {"public_key": key, "address": str(addr)}
    return {"interfaces": list(dict.fromkeys(interfaces)),
            "wireguard": {**wg, "clients": clients}}


def load_config() -> dict[str, Any]:
    if not config_path().exists():
        raise AsbxError(f"no config at {config_path()}; run 'asbx init' first")
    with open(config_path()) as f:
        raw = yaml.safe_load(f) or {}
    cfg = deep_merge(DEFAULT_CONFIG, raw)
    cfg["defaults"]["env"] = normalize_env(cfg["defaults"]["env"], "defaults.env")
    cfg["defaults"]["tools"] = normalize_tools(cfg["defaults"]["tools"], "defaults.tools")
    cfg["harness_env"] = normalize_env(cfg["harness_env"], "harness_env")
    if not is_aws_profile_list(cfg["aws"]["profiles"]):
        raise AsbxError(f"config {config_path()}: 'aws.profiles' must be {AWS_PROFILES_RULE}")
    harness = cfg["harness_settings"]
    harness["sync"] = validate_harness_list(harness["sync"], "harness_settings.sync", credentials=False)
    harness["credentials"] = validate_harness_list(
        harness["credentials"], "harness_settings.credentials", credentials=True)
    if cfg["shell"] not in SHELL_PATHS:
        raise AsbxError(
            f"config {config_path()}: 'shell' must be one of: {', '.join(SHELL_PATHS)}")
    cfg["network"]["ingress"] = validate_ingress(cfg["network"]["ingress"])
    return cfg


def set_config_value(keys: tuple[str, ...], value: Any) -> None:
    """Sets one user override in config.yaml, e.g. ('network', 'ingress',
    'interfaces'). The file is rewritten from its parsed form, which would drop
    comments, so a file with comments is left alone and the caller is told what
    to add instead."""
    path = config_path()
    text = path.read_text()
    snippet = yaml.safe_dump(nest(keys, value), sort_keys=False)
    if any(line.lstrip().startswith("#") or " #" in line for line in text.splitlines()):
        raise AsbxError(f"{path} has comments, which rewriting it would lose; "
                        f"add this yourself, then re-run:\n{snippet}")
    raw = yaml.safe_load(text) or {}
    set_in(raw, keys, value)
    host.write_private_text(path, yaml.safe_dump(raw, sort_keys=False))


def nest(keys: tuple[str, ...], value: Any) -> dict:
    """`value` under `keys`, e.g. nest(('a', 'b'), 1) == {'a': {'b': 1}}."""
    for k in reversed(keys):
        value = {k: value}
    return value


def set_in(mapping: dict, keys: tuple[str, ...], value: Any) -> None:
    """mapping[k0][k1]... = value, creating (or replacing non-mapping) parents."""
    node = mapping
    for k in keys[:-1]:
        if not isinstance(node.get(k), dict):
            node[k] = {}
        node = node[k]
    node[keys[-1]] = value


def expand(p: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(p)))
