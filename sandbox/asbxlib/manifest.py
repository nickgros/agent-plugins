from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import yaml

from . import config
from .errors import AsbxError

# ---------------------------------------------------------------------------
# Manifest schema and loader
# ---------------------------------------------------------------------------

MANIFEST_KEYS = {
    "name", "resources", "repos", "mounts", "services", "env", "auth", "setup", "remotes",
}
REPO_KEYS = {"url", "dir", "ref", "setup", "remotes"}
MOUNT_KEYS = {"host", "guest", "mode"}
RESOURCE_KEYS = {"cpu", "memory", "disk"}
SERVICES_KEYS = {"compose"}
MEMORY_RE = re.compile(r"^\d+(MiB|GiB)$")


@dataclass(frozen=True)
class Mount:
    host: str
    guest: str
    mode: str = "ro"

    @staticmethod
    def from_dict(d: dict) -> "Mount":
        return Mount(host=d["host"], guest=d["guest"], mode=d.get("mode", "ro"))


@dataclass(frozen=True)
class Repo:
    url: str
    dir: str
    ref: Optional[str] = None
    setup: Optional[str] = None
    remotes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Manifest:
    path: str
    name: str
    instance: str
    resources: dict[str, Any]
    repos: list[Repo]
    mounts: list[Mount]
    compose_path: Optional[str]
    env: dict[str, str]
    auth: list[Any]
    setup_path: Optional[str]
    remotes: dict[str, str]


def repo_dir(repo: dict) -> str:
    if "dir" in repo and repo["dir"]:
        return repo["dir"]
    url = repo["url"]
    base = url.rstrip("/").rsplit("/", 1)[-1]
    if base.endswith(".git"):
        base = base[: -len(".git")]
    return base


def validate_manifest_dict(data: dict, manifest_path: str) -> dict:
    for k in data:
        if k not in MANIFEST_KEYS:
            raise AsbxError(f"manifest {manifest_path}: unknown key '{k}'")

    resources = data.get("resources")
    if resources is not None:
        if not isinstance(resources, dict):
            raise AsbxError(f"manifest {manifest_path}: 'resources' must be a mapping")
        for k in resources:
            if k not in RESOURCE_KEYS:
                raise AsbxError(f"manifest {manifest_path}: unknown key 'resources.{k}'")
        if "cpu" in resources and not isinstance(resources["cpu"], int):
            raise AsbxError(f"manifest {manifest_path}: resources.cpu must be an int")
        for field in ("memory", "disk"):
            if field in resources and not MEMORY_RE.match(str(resources[field])):
                raise AsbxError(
                    f"manifest {manifest_path}: resources.{field} '{resources[field]}' "
                    f"must match ^\\d+(MiB|GiB)$"
                )

    repos = data.get("repos") or []
    seen_dirs: set[str] = set()
    for repo in repos:
        if "url" not in repo or not repo["url"]:
            raise AsbxError(f"manifest {manifest_path}: repos[*].url is required")
        for k in repo:
            if k not in REPO_KEYS:
                raise AsbxError(f"manifest {manifest_path}: unknown key 'repos.{k}'")
        _validate_remotes_dict(repo.get("remotes"), manifest_path, "repos[*].remotes")
        d = repo_dir(repo)
        if d in seen_dirs:
            raise AsbxError(f"manifest {manifest_path}: duplicate repos[*].dir '{d}'")
        seen_dirs.add(d)

    _validate_remotes_dict(data.get("remotes"), manifest_path, "remotes")

    mounts = data.get("mounts") or []
    for mount in mounts:
        for k in mount:
            if k not in MOUNT_KEYS:
                raise AsbxError(f"manifest {manifest_path}: unknown key 'mounts.{k}'")
        for required in ("host", "guest"):
            if not mount.get(required):
                raise AsbxError(f"manifest {manifest_path}: mounts[*].{required} is required")
        mode = mount.get("mode", "ro")
        if mode != "ro":
            raise AsbxError(
                f"mount {mount.get('guest')}: only mode 'ro' is supported "
                f"(writable mounts are not implemented)"
            )

    services = data.get("services")
    if services is not None:
        if not isinstance(services, dict):
            raise AsbxError(f"manifest {manifest_path}: 'services' must be a mapping")
        for k in services:
            if k not in SERVICES_KEYS:
                raise AsbxError(f"manifest {manifest_path}: unknown key 'services.{k}'")

    manifest_dir = os.path.dirname(os.path.realpath(manifest_path))
    for field in ("services", "setup"):
        val = data.get(field)
        if field == "services" and isinstance(val, dict):
            val = val.get("compose")
        if val:
            real = os.path.realpath(os.path.join(manifest_dir, val))
            if not real.startswith(manifest_dir + os.sep) and real != manifest_dir:
                raise AsbxError(f"manifest {manifest_path}: '{field}' escapes manifest directory")
            if not os.path.isfile(real):
                raise AsbxError(f"manifest {manifest_path}: '{field}' file not found: {val}")

    return data


def merge_resources(defaults: dict, manifest_resources: Optional[dict]) -> dict:
    result = dict(defaults)
    if manifest_resources:
        result.update(manifest_resources)
    return result


_OWNER_REPO_RE = re.compile(r"^(.*[:/])([^/]+)/([^/]+)$")


def _validate_remotes_dict(remotes: Any, manifest_path: str, field: str) -> None:
    if not remotes:
        return
    if not isinstance(remotes, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in remotes.items()
    ):
        raise AsbxError(f"manifest {manifest_path}: '{field}' must be a mapping of name to org/URL strings")


def resolve_remote_url(repo_url: str, spec: str) -> str:
    """Resolves a `remotes` manifest value to a concrete git remote URL.

    `spec` is either a full URL (contains '://' or an `ssh`-style
    `user@host:...` address), used verbatim, or an org/user name substituted
    for the owner segment of `repo_url`, keeping its host, scheme, and repo
    name intact, e.g. `https://github.com/nickgros/foo.git` + `Sage-Bionetworks`
    -> `https://github.com/Sage-Bionetworks/foo.git`.
    """
    if "://" in spec or "@" in spec:
        return spec
    m = _OWNER_REPO_RE.match(repo_url)
    if not m:
        raise AsbxError(f"remotes: cannot derive an org URL from '{repo_url}' for org '{spec}'")
    prefix, _owner, repo = m.groups()
    return f"{prefix}{spec}/{repo}"


def merge_remotes(base_remotes: dict, override_remotes: dict) -> dict:
    """`override_remotes` wins on name collisions."""
    result = dict(base_remotes)
    result.update(override_remotes)
    return result


def merge_mounts(default_mounts: list[Mount], manifest_mounts: list[Mount]) -> list[Mount]:
    by_guest: dict[str, Mount] = {}
    for m in default_mounts:
        by_guest[m.guest] = m
    for m in manifest_mounts:
        by_guest[m.guest] = m
    return list(by_guest.values())


def load_manifest(group: str, cfg: dict) -> Manifest:
    path = config.projects_dir() / f"{group}.yaml"
    if not path.exists():
        raise AsbxError(f"no manifest at {path}")
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    validate_manifest_dict(data, str(path))

    name = data.get("name") or path.stem
    resources = merge_resources(cfg["defaults"]["resources"], data.get("resources"))
    default_mounts = [Mount.from_dict(m) for m in cfg["defaults"]["mounts"]]
    manifest_mounts = [Mount.from_dict(m) for m in (data.get("mounts") or [])]
    mounts = merge_mounts(default_mounts, manifest_mounts)
    auth = data.get("auth", cfg["defaults"]["auth"])
    env = dict(data.get("env") or {})

    # Top-level `remotes` wins over a repo's own entry of the same name: the
    # group-wide setting is the one the operator states last.
    global_remotes = merge_remotes(cfg["defaults"]["remotes"], data.get("remotes") or {})

    repos = []
    for repo in data.get("repos") or []:
        repos.append(Repo(
            url=repo["url"],
            dir=repo_dir(repo),
            ref=repo.get("ref"),
            setup=repo.get("setup"),
            remotes=merge_remotes(repo.get("remotes") or {}, global_remotes),
        ))

    manifest_dir = path.parent
    services = data.get("services") or {}
    compose_path = None
    if services.get("compose"):
        compose_path = str(manifest_dir / services["compose"])
    setup_path = None
    if data.get("setup"):
        setup_path = str(manifest_dir / data["setup"])

    return Manifest(
        path=str(path),
        name=name,
        instance=f"{cfg['instance_prefix']}{name}",
        resources=resources,
        repos=repos,
        mounts=mounts,
        compose_path=compose_path,
        env=env,
        auth=auth,
        setup_path=setup_path,
        remotes=global_remotes,
    )


def manifest_sha256(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


# ---------------------------------------------------------------------------
# Lifecycle helpers
# ---------------------------------------------------------------------------

def manifest_groups(cfg: dict) -> list[tuple[str, str]]:
    """(group, instance) for every manifest on disk."""
    projects_dir = config.projects_dir()
    if not projects_dir.exists():
        return []
    return [(p.stem, f"{cfg['instance_prefix']}{p.stem}")
            for p in sorted(projects_dir.glob("*.yaml"))]
