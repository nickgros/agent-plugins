from __future__ import annotations

import os
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
        "block_cidrs": [
            "10.0.0.0/8",
            "172.16.0.0/12",
            "192.168.0.0/16",
            "169.254.0.0/16",
            "fc00::/7",
            "fe80::/10",
        ],
    },
    "ssh": {
        "key": "~/.config/agent-sandbox/id_ed25519",
        "config_dir": "~/.ssh/agent-sandbox.d",
        "known_hosts": "~/.config/agent-sandbox/known_hosts",
    },
    "tailscale": {
        "auth_key": "",
        "tags": ["tag:sandbox"],
    },
    "defaults": {
        "resources": {"cpu": 4, "memory": "8GiB", "disk": "30GiB"},
        "auth": ["github-gh", "aws-sso"],
        "mounts": [
            {"host": "~/.agents/skills", "guest": "/home/agent/.agents/skills", "mode": "ro"},
        ],
        "copy_binaries": ["~/.local/bin/omp"],
        "env": {},
        "git": {"name": "", "email": ""},
        "remotes": {},
    },
    "aws": {
        "profile": "sage-bedrock",
        "config_file": "~/.aws/config",
    },
    "harness_env": {
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "AWS_REGION": "us-east-1",
        "AWS_PROFILE": "sage-bedrock",
    },
}


def deep_merge(base: dict, override: dict, path: str = "") -> dict:
    result = dict(base)
    for k, v in override.items():
        key_path = f"{path}.{k}" if path else k
        if k not in base:
            raise AsbxError(f"config {config_path()}: unknown key '{key_path}'")
        if isinstance(v, dict) and isinstance(base[k], dict):
            result[k] = deep_merge(base[k], v, key_path)
        else:
            result[k] = v
    return result


def load_config() -> dict[str, Any]:
    if not config_path().exists():
        raise AsbxError(f"no config at {config_path()}; run 'asbx init' first")
    with open(config_path()) as f:
        raw = yaml.safe_load(f) or {}
    cfg = deep_merge(DEFAULT_CONFIG, raw)
    return cfg


def expand(p: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(p)))
