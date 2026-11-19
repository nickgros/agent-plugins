"""asbx command-line interface: argument parsing, backend selection and process exit."""

from __future__ import annotations

import argparse
import sys
from typing import Optional

from .backend import SandboxBackend
from .errors import AsbxError
from .incusbackend import IncusBackend
from .tartbackend import TartBackend
from . import config
from . import host
from . import manifest as manifest_mod
from . import auth
from . import awscreds
from . import harnesssync
from . import baseimage
from . import up
from . import lifecycle
from . import doctor
from . import hostsetup
from . import ingress
from . import sshconf

_AUTO_BACKENDS = {"linux": "incus", "darwin": "tart"}


def make_backend(cfg: Optional[dict]) -> SandboxBackend:
    """The backend `cfg["backend"]` names; `auto` (and no config at all)
    picks by host OS: Incus on Linux, Tart on macOS."""
    name = cfg["backend"] if cfg else "auto"
    if name == "auto":
        name = _AUTO_BACKENDS.get(sys.platform)
        if name is None:
            raise AsbxError(f"no sandbox backend for platform {sys.platform}; "
                            "asbx runs on Linux (Incus) and macOS (Tart)")
    if name == "tart":
        return TartBackend()
    return IncusBackend()


def _incus(backend: SandboxBackend, command: str) -> IncusBackend:
    """`backend` as the Incus backend, for commands that exist only there:
    ingress filters the Incus bridge."""
    if not isinstance(backend, IncusBackend):
        raise AsbxError(f"'asbx {command}' is not supported on the {backend.name} backend")
    return backend


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="asbx", description="agent sandbox VMs on Incus (Linux) or Tart (macOS)")
    sub = p.add_subparsers(dest="command", required=True)

    def add(name: str, handler, *, needs_config: bool = True, needs_backend: bool = True, **kwargs):
        """`needs_backend=False` handlers get None for the backend, so they run on
        hosts no backend supports."""
        sp = sub.add_parser(name, **kwargs)
        sp.set_defaults(handler=handler, needs_config=needs_config, needs_backend=needs_backend)
        return sp

    add("init", lambda backend, cfg, args: hostsetup.cmd_init(backend), needs_config=False,
        help="set up host config, ssh key and the backend's host-side state")

    sp = add("doctor", _handle_doctor, needs_config=False, needs_backend=False,
             help="check host readiness")
    sp.add_argument("--fix", action="store_true")

    sp = add("install", lambda backend, cfg, args: hostsetup.cmd_install(args.force),
             needs_config=False, needs_backend=False, help="symlink this script to ~/.local/bin/asbx")
    sp.add_argument("--force", action="store_true")

    add("build-base", lambda backend, cfg, args: baseimage.cmd_build_base(backend, cfg),
        help="bake the golden base image")

    sp = add("up", lambda backend, cfg, args: up.cmd_up(backend, cfg, args.group, args.no_auth, args.no_provision),
             help="create/start a sandbox and provision it")
    sp.add_argument("group")
    sp.add_argument("--no-auth", action="store_true")
    sp.add_argument("--no-provision", action="store_true")

    sp = add("auth", _handle_auth, help="run the auth phase for a group")
    sp.add_argument("group")

    sp = add("aws-refresh",
             lambda backend, cfg, args: awscreds.install_timer() if args.install_timer
             else awscreds.cmd_aws_refresh(backend, cfg, args.group),
             help="push fresh AWS credentials into running sandboxes")
    sp.add_argument("group", nargs="?")
    sp.add_argument("--install-timer", action="store_true",
                    help="install the host job that re-pushes every 10 minutes and when "
                         "'aws sso login' rewrites the token cache (systemd user units on Linux, "
                         "a LaunchAgent on macOS)")

    sp = add("settings-sync",
             lambda backend, cfg, args: harnesssync.cmd_settings_sync(backend, cfg, args.group),
             help="push host harness settings into running sandboxes")
    sp.add_argument("group", nargs="?")

    sp = add("provision", _handle_provision, help="run the provision phase for a group")
    sp.add_argument("group")

    add("list", lambda backend, cfg, args: lifecycle.cmd_list(backend, cfg), help="list sandboxes")

    sp = add("ssh", lambda backend, cfg, args: lifecycle.cmd_ssh(backend, cfg, args.group, args.cmd),
             help="ssh into a sandbox")
    sp.add_argument("group")
    sp.add_argument("cmd", nargs=argparse.REMAINDER)

    sp = add("ssh-config", lambda backend, cfg, args: sshconf.cmd_ssh_config(
                 backend, cfg, args.group, None if args.direct else args.jump, args.identity),
             help="print the ~/.ssh/config stanza for another machine")
    sp.add_argument("group")
    route = sp.add_mutually_exclusive_group(required=True)
    route.add_argument("--jump", metavar="USER@HOST",
                       help="how the other machine reaches this host")
    route.add_argument("--direct", action="store_true",
                       help="connect to the sandbox's pinned IP over an ingress interface (Incus only)")
    sp.add_argument("--identity", default="~/.ssh/id_ed25519",
                    help="private key on the other machine (default: %(default)s)")

    sp = add("ssh-proxy", _handle_ssh_proxy,
             help="relay stdin/stdout to a sandbox's ssh port (the generated ProxyCommand)")
    sp.add_argument("instance")

    sp = add("ingress", lambda backend, cfg, args: ingress.cmd_ingress_setup(
                 _incus(backend, "ingress setup"), cfg, args.create, args.adopt, args.print_only, args.yes),
             help="install the ingress firewall; create or adopt an ingress interface (Incus only)")
    ingress_sub = sp.add_subparsers(dest="ingress_command", required=True)
    setup = ingress_sub.add_parser("setup", help="install or refresh the ingress firewall (sudo)")
    which = setup.add_mutually_exclusive_group()
    which.add_argument("--create", action="store_true",
                       help="enable the asbx-managed WireGuard interface")
    which.add_argument("--adopt", nargs="?", const="", metavar="IFACE",
                       help="treat an existing WireGuard or tun interface as ingress; "
                            "lists candidates when IFACE is omitted")
    setup.add_argument("--print", dest="print_only", action="store_true",
                       help="print the files and commands instead of installing them")
    setup.add_argument("--yes", action="store_true", help="don't ask before changing anything")

    sp = add("client-config", lambda backend, cfg, args: ingress.cmd_client_config(
                 _incus(backend, "client-config"), cfg, args.name),
             help="print a managed-WireGuard client's wg-quick config (Incus only)")
    sp.add_argument("name")

    sp = add("shell", lambda backend, cfg, args: lifecycle.cmd_shell(backend, cfg, args.group, args.root),
             help="host-side rescue shell in a sandbox, bypassing ssh")
    sp.add_argument("group")
    sp.add_argument("--root", action="store_true")

    for name, handler in (("start", lifecycle.cmd_start), ("stop", lifecycle.cmd_stop), ("restart", lifecycle.cmd_restart)):
        sp = add(name, lambda backend, cfg, args, _h=handler: _h(backend, cfg, args.group))
        sp.add_argument("group")

    sp = add("snapshot", lambda backend, cfg, args: lifecycle.cmd_snapshot(backend, cfg, args.group, args.label))
    sp.add_argument("group")
    sp.add_argument("label", nargs="?")

    sp = add("restore", lambda backend, cfg, args: lifecycle.cmd_restore(backend, cfg, args.group, args.label, args.yes))
    sp.add_argument("group")
    sp.add_argument("label")
    sp.add_argument("--yes", action="store_true")

    sp = add("rebuild", lambda backend, cfg, args: lifecycle.cmd_rebuild(backend, cfg, args.group, args.force, args.yes))
    sp.add_argument("group")
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--yes", action="store_true")

    sp = add("rm", lambda backend, cfg, args: lifecycle.cmd_rm(backend, cfg, args.group, args.yes))
    sp.add_argument("group")
    sp.add_argument("--yes", action="store_true")

    return p


def _handle_auth(backend: SandboxBackend, cfg: dict, args) -> None:
    manifest = manifest_mod.load_manifest(args.group, cfg)
    auth.run_auth(backend.guest(manifest.instance), manifest, cfg)


def _handle_doctor(backend: None, cfg: Optional[dict], args) -> int:
    """Builds its own backend so a host no backend supports still gets a report."""
    try:
        backend = make_backend(cfg)
    except AsbxError as e:
        print(f"FAIL {e}")
        return 1
    return doctor.cmd_doctor(backend, cfg, args.fix)


def _handle_provision(backend: SandboxBackend, cfg: dict, args) -> None:
    up.run_provision(backend, manifest_mod.load_manifest(args.group, cfg), cfg)


def _handle_ssh_proxy(backend: SandboxBackend, cfg: dict, args) -> None:
    """The address comes from host-side backend data at connect time, which
    is what lets the ssh config stay static when the address changes."""
    address = backend.ssh_address(cfg, args.instance)
    if not address:
        raise AsbxError(f"{args.instance} has no address yet; is it running? ('asbx list')")
    host.relay_tcp(address, 22)


def fail(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = None
        if args.needs_config:
            cfg = config.load_config()
        elif args.command == "doctor":
            try:
                cfg = config.load_config()
            except AsbxError:
                pass
        backend = make_backend(cfg) if args.needs_backend else None
        rc = args.handler(backend, cfg, args)
    except AsbxError as e:
        fail(str(e))
        return 1
    return rc or 0
