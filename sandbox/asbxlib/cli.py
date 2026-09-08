"""asbx command-line interface: argument parsing and process exit."""

from __future__ import annotations

import argparse
import sys
from typing import Optional

from .errors import AsbxError
from .incus import Incus
from . import config
from . import manifest as manifest_mod
from . import auth
from . import provision
from . import baseimage
from . import up
from . import lifecycle
from . import doctor
from . import hostsetup


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="asbx", description="agent sandbox VMs on Incus")
    sub = p.add_subparsers(dest="command", required=True)

    def add(name: str, handler, *, needs_config: bool = True, **kwargs):
        sp = sub.add_parser(name, **kwargs)
        sp.set_defaults(handler=handler, needs_config=needs_config)
        return sp

    add("init", lambda incus, cfg, args: hostsetup.cmd_init(incus), needs_config=False,
        help="set up host config, ssh key, ACL")

    sp = add("doctor", lambda incus, cfg, args: doctor.cmd_doctor(incus, cfg, args.fix),
             needs_config=False, help="check host readiness")
    sp.add_argument("--fix", action="store_true")

    sp = add("install", lambda incus, cfg, args: hostsetup.cmd_install(args.force), needs_config=False,
             help="symlink this script to ~/.local/bin/asbx")
    sp.add_argument("--force", action="store_true")

    add("build-base", lambda incus, cfg, args: baseimage.cmd_build_base(incus, cfg),
        help="bake the golden base image")

    sp = add("up", lambda incus, cfg, args: up.cmd_up(incus, cfg, args.group, args.no_auth, args.no_provision),
             help="create/start a sandbox and provision it")
    sp.add_argument("group")
    sp.add_argument("--no-auth", action="store_true")
    sp.add_argument("--no-provision", action="store_true")

    sp = add("auth", _handle_auth, help="run the auth phase for a group")
    sp.add_argument("group")

    sp = add("provision", _handle_provision, help="run the provision phase for a group")
    sp.add_argument("group")

    add("list", lambda incus, cfg, args: lifecycle.cmd_list(incus, cfg), help="list sandboxes")

    sp = add("ssh", lambda incus, cfg, args: lifecycle.cmd_ssh(incus, cfg, args.group, args.cmd),
             help="ssh into a sandbox")
    sp.add_argument("group")
    sp.add_argument("cmd", nargs=argparse.REMAINDER)

    sp = add("shell", lambda incus, cfg, args: lifecycle.cmd_shell(incus, cfg, args.group, args.root),
             help="incus exec shell (host-side rescue)")
    sp.add_argument("group")
    sp.add_argument("--root", action="store_true")

    for name, handler in (("start", lifecycle.cmd_start), ("stop", lifecycle.cmd_stop), ("restart", lifecycle.cmd_restart)):
        sp = add(name, lambda incus, cfg, args, _h=handler: _h(incus, cfg, args.group))
        sp.add_argument("group")

    sp = add("snapshot", lambda incus, cfg, args: lifecycle.cmd_snapshot(incus, cfg, args.group, args.label))
    sp.add_argument("group")
    sp.add_argument("label", nargs="?")

    sp = add("restore", lambda incus, cfg, args: lifecycle.cmd_restore(incus, cfg, args.group, args.label, args.yes))
    sp.add_argument("group")
    sp.add_argument("label")
    sp.add_argument("--yes", action="store_true")

    sp = add("rebuild", lambda incus, cfg, args: lifecycle.cmd_rebuild(incus, cfg, args.group, args.force, args.yes))
    sp.add_argument("group")
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--yes", action="store_true")

    sp = add("rm", lambda incus, cfg, args: lifecycle.cmd_rm(incus, cfg, args.group, args.yes, args.keep_node))
    sp.add_argument("group")
    sp.add_argument("--yes", action="store_true")
    sp.add_argument("--keep-node", action="store_true")

    return p


def _handle_auth(incus: Incus, cfg: dict, args) -> None:
    manifest = manifest_mod.load_manifest(args.group, cfg)
    auth.run_auth(incus, manifest.instance, manifest.auth, cfg)


def _handle_provision(incus: Incus, cfg: dict, args) -> None:
    manifest = manifest_mod.load_manifest(args.group, cfg)
    provision.run_provision(incus, manifest.instance, manifest, cfg["guest_user"])


def fail(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    incus = Incus()
    try:
        cfg = None
        if args.needs_config:
            cfg = config.load_config()
        elif args.command == "doctor":
            try:
                cfg = config.load_config()
            except AsbxError:
                pass
        rc = args.handler(incus, cfg, args)
    except AsbxError as e:
        fail(str(e))
        return 1
    return rc or 0
