from __future__ import annotations

from pathlib import Path
from typing import Optional

from . import auth, config, guest, harnesssync, manifest, provision, sshconf, ui
from .backend import (ABSENT, META_BASE_IMAGE, META_GROUP, META_MANIFEST_SHA256, RUNNING,
                      SandboxBackend, SandboxSpec)
from .errors import AsbxError


def spec_for(m: manifest.Manifest, ipv4: Optional[str] = None) -> SandboxSpec:
    resources = m.resources
    return SandboxSpec(instance=m.instance, group=m.name, cpu=resources["cpu"],
                       memory=resources["memory"], disk=resources["disk"],
                       mounts=list(m.mounts), ipv4=ipv4)


def run_provision(backend: SandboxBackend, m: manifest.Manifest, cfg: dict) -> None:
    provision.run_provision(backend.guest(m.instance), m, cfg["guest_user"])
    backend.meta_set(m.instance, META_MANIFEST_SHA256, manifest.manifest_sha256(m.path))


def cmd_up(backend: SandboxBackend, cfg: dict, group: str, no_auth: bool, no_provision: bool,
           ipv4: Optional[str] = None) -> None:
    """`ipv4` is the address to pin on a newly created sandbox (rebuild's previous one)."""
    manifest_obj = manifest.load_manifest(group, cfg)
    instance = manifest_obj.instance
    spec = spec_for(manifest_obj, ipv4)

    backend.preflight(cfg)

    state = backend.state(instance)
    if state == ABSENT:
        base = backend.base_image(cfg)
        if not base:
            raise AsbxError(f"no base image ({cfg['image_alias']}); run 'asbx build-base' first")
        ssh_pub_path = Path(str(config.expand(cfg["ssh"]["key"])) + ".pub")
        if not ssh_pub_path.exists():
            raise AsbxError(f"ssh public key not found at {ssh_pub_path}; run 'asbx init' first")
        backend.create(cfg, spec)
        backend.meta_set(instance, META_GROUP, group)
        backend.meta_set(instance, META_BASE_IMAGE, base)
        backend.meta_set(instance, META_MANIFEST_SHA256, manifest.manifest_sha256(manifest_obj.path))

    # Applied on every `up`, not just at creation: adding a mount to a
    # manifest has to reach an existing sandbox for `up` to be idempotent.
    if backend.apply(cfg, spec) and state == RUNNING:
        ui.ok(f"restarting {instance} to apply its new configuration")
        backend.restart(cfg, spec)
    backend.start(cfg, spec)

    g = backend.guest(instance)
    guest.set_hostname(g)
    guest.sync_authorized_keys(g, cfg)

    ui.ok(f"{instance}: ssh config")
    sshconf.write_ssh_config(cfg, instance, backend.ssh_route(cfg, instance))

    guest.write_guest_env(g, cfg, manifest_obj.env)
    guest.write_git_identity(g, cfg)
    try:
        harnesssync.sync_instance(g, cfg, manifest_obj)
    except AsbxError as e:
        # Settings are a convenience; a bad host file must not block auth and provisioning.
        ui.warn(f"{instance}: harness settings not synced: {e}")

    try:
        guest.configure_shell(g, cfg)
    except AsbxError as e:
        ui.warn(f"{instance}: shell not configured: {e}")

    guest.start_paseo(g)

    if not no_auth:
        auth.run_auth(g, manifest_obj, cfg)
    if not no_provision:
        run_provision(backend, manifest_obj, cfg)

    print(f"ssh {instance}  # or: code --remote ssh-remote+{instance} /workspace")
