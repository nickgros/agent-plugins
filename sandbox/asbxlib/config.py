from __future__ import annotations

import os
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

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


DEFAULT_CONFIG: dict[str, Any] = {
    "guest_user": "agent",
    "instance_prefix": "sandbox-",
    "image_alias": "agent-sandbox-base",
    "base_image_source": "images:debian/13/cloud",
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
OPEN_MAPS = frozenset({"defaults.env", "defaults.remotes", "harness_env"})


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


def load_config() -> dict[str, Any]:
    if not config_path().exists():
        raise AsbxError(f"no config at {config_path()}; run 'asbx init' first")
    with open(config_path()) as f:
        raw = yaml.safe_load(f) or {}
    cfg = deep_merge(DEFAULT_CONFIG, raw)
    cfg["defaults"]["env"] = normalize_env(cfg["defaults"]["env"], "defaults.env")
    cfg["harness_env"] = normalize_env(cfg["harness_env"], "harness_env")
    if not is_aws_profile_list(cfg["aws"]["profiles"]):
        raise AsbxError(f"config {config_path()}: 'aws.profiles' must be {AWS_PROFILES_RULE}")
    harness = cfg["harness_settings"]
    harness["sync"] = validate_harness_list(harness["sync"], "harness_settings.sync", credentials=False)
    harness["credentials"] = validate_harness_list(
        harness["credentials"], "harness_settings.credentials", credentials=True)
    return cfg


def expand(p: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(p)))
