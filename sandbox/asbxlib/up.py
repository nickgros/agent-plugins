from __future__ import annotations

from pathlib import Path

from . import auth, config, guest, manifest, netacl, provision, sshconf, tailscale, ui
from .errors import AsbxError
from .incus import Incus


def cmd_up(incus: Incus, cfg: dict, group: str, no_auth: bool, no_provision: bool) -> None:
    manifest_obj = manifest.load_manifest(group, cfg)
    instance = manifest_obj.instance

    try:
        netacl.ensure_acl(incus, cfg)
    except AsbxError as e:
        ui.warn(f"could not refresh network ACL: {e}")

    if not incus.image_alias_exists(cfg["image_alias"]):
        raise AsbxError(f"image alias {cfg['image_alias']} not found; run 'asbx build-base' first")

    created = not incus.instance_exists(instance)
    if created:
        ssh_pub_path = Path(str(config.expand(cfg["ssh"]["key"])) + ".pub")
        if not ssh_pub_path.exists():
            raise AsbxError(f"ssh public key not found at {ssh_pub_path}; run 'asbx init' first")
        cloud_init = guest.render_instance_cloud_init(instance, cfg["guest_user"], ssh_pub_path.read_text().strip())
        resources = manifest_obj.resources
        ui.ok(f"creating {instance}")
        incus.run(
            "create", cfg["image_alias"], instance, "--vm",
            "-c", f"limits.cpu={resources['cpu']}",
            "-c", f"limits.memory={resources['memory']}",
            "-d", f"root,size={resources['disk']}",
            "-c", f"cloud-init.user-data={cloud_init}",
        )
        incus.config_set(instance, "user.asbx.group", group)
        fingerprint = incus.image_alias_exists(cfg["image_alias"])
        incus.config_set(instance, "user.asbx.base_image", fingerprint or "")
        incus.config_set(instance, "user.asbx.manifest_sha256", manifest.manifest_sha256(manifest_obj.path))

        incus.run("start", instance)

        ui.ok(f"waiting for {instance} agent")
        guest.wait_for_agent(incus, instance)
        guest.wait_for_cloud_init(incus, instance)
        netacl.apply_nic_acl(incus, instance, cfg)
    else:
        state = incus.instance_state(instance)
        if state != "Running":
            ui.ok(f"starting {instance}")
            incus.run("start", instance)

        ui.ok(f"waiting for {instance} agent")
        guest.wait_for_agent(incus, instance)
        guest.wait_for_cloud_init(incus, instance)

    # Mounts are synced on every `up`, not just at creation: adding a mount to
    # a manifest has to reach an existing sandbox for `up` to be idempotent.
    if guest.sync_mounts(incus, instance, manifest_obj.mounts):
        ui.ok(f"restarting {instance} to attach mounts")
        incus.run("restart", instance)
        guest.wait_for_agent(incus, instance)
        guest.wait_for_cloud_init(incus, instance)

    ui.ok(f"{instance}: tailscale")
    tailscale.run_tailscale_up(incus, instance, cfg)
    sshconf.write_ssh_config(incus, instance, cfg)

    guest.write_guest_env(incus, instance, cfg, manifest_obj.env)
    guest.write_git_identity(incus, instance, cfg)

    if not no_auth:
        auth.run_auth(incus, instance, manifest_obj.auth, cfg)
    if not no_provision:
        provision.run_provision(incus, instance, manifest_obj, cfg["guest_user"])

    print(f"ssh {instance}  # or: code --remote ssh-remote+{instance} /workspace")
