from __future__ import annotations

from . import config
from . import ui
from .errors import AsbxError
from .incus import Incus

# ---------------------------------------------------------------------------
# Auth phase — declarative providers
# ---------------------------------------------------------------------------

_SIGNING_SCOPE = "admin:ssh_signing_key"


def _provider_github_gh(incus: Incus, instance: str, cfg: dict) -> None:
    guest_user = cfg["guest_user"]
    check = incus.exec_in(instance, ["gh", "auth", "status"], user=guest_user, check=False)
    authed = check.returncode == 0
    if authed and _SIGNING_SCOPE in f"{check.stdout or ''}{check.stderr or ''}":
        ui.ok(f"{instance}: github-gh already authenticated")
        return
    pubkey_path = f"/home/{guest_user}/.ssh/id_ed25519.pub"
    # (label, argv, tty). Interactive gh steps need the terminal; the key upload
    # runs captured so its stderr can be inspected and surfaced.
    if authed:
        # Logged in, but the token cannot manage signing keys: widen it in place.
        first = ("refresh scope", ["gh", "auth", "refresh", "--hostname", "github.com",
                                    "--scopes", _SIGNING_SCOPE], True)
    else:
        first = ("login", ["gh", "auth", "login", "--hostname", "github.com",
                           "--git-protocol", "https", "--scopes", _SIGNING_SCOPE], True)
    steps = [
        first,
        ("setup-git", ["gh", "auth", "setup-git"], True),
        ("keygen", ["bash", "-c", "test -f ~/.ssh/id_ed25519 || ssh-keygen -t ed25519 -N '' "
                                   f"-C asbx-{instance} -f ~/.ssh/id_ed25519"], True),
        ("upload signing key", ["gh", "ssh-key", "add", pubkey_path, "--type", "signing",
                                "--title", f"asbx {instance}"], False),
        ("git signing config", ["bash", "-c", "git config --global gpg.format ssh && "
                                              "git config --global user.signingkey ~/.ssh/id_ed25519.pub && "
                                              "git config --global commit.gpgsign true"], True),
    ]
    for n, (label, argv, tty) in enumerate(steps, start=1):
        proc = incus.exec_in(instance, argv, user=guest_user, tty=tty, check=False)
        if proc.returncode == 0:
            continue
        stderr = (proc.stderr or "").strip()
        if label == "upload signing key" and "key is already in use" in stderr:
            continue
        detail = f"\n{stderr}" if stderr else ""
        raise AsbxError(f"auth provider github-gh failed on step {n} ({label}){detail}")


def _provider_aws_sso(incus: Incus, instance: str, cfg: dict) -> None:
    guest_user = cfg["guest_user"]
    profile = cfg["aws"]["profile"]
    check = incus.exec_in(instance, ["aws", "sts", "get-caller-identity", "--profile", profile],
                           user=guest_user, check=False)
    if check.returncode == 0:
        ui.ok(f"{instance}: aws-sso already authenticated")
        return
    config_file = config.expand(cfg["aws"]["config_file"])
    if not config_file.exists():
        raise AsbxError(f"auth provider aws-sso failed on step 1: {config_file} not found")
    aws_dir = f"/home/{guest_user}/.aws"
    incus.exec_in(instance, ["mkdir", "-p", aws_dir], user=guest_user)
    guest_uid = incus.uid_of(instance, guest_user)
    incus.file_push(instance, str(config_file), f"{aws_dir}/config", mode="0600",
                     uid=guest_uid, gid=guest_uid)
    proc = incus.exec_in(instance, ["aws", "sso", "login", "--use-device-code", "--profile", profile],
                          user=guest_user, tty=True, check=False)
    if proc.returncode != 0:
        raise AsbxError("auth provider aws-sso failed on step 2")


BUILTIN_AUTH_PROVIDERS = {
    "github-gh": _provider_github_gh,
    "aws-sso": _provider_aws_sso,
}

def _run_command_provider(incus: Incus, instance: str, spec: dict, guest_user: str) -> None:
    check_cmd = spec.get("check")
    if check_cmd:
        check = incus.exec_in(instance, ["bash", "-lc", check_cmd], user=guest_user, check=False)
        if check.returncode == 0:
            ui.ok(f"{instance}: {spec.get('command')} already authenticated")
            return
    proc = incus.exec_in(instance, ["bash", "-lc", spec["command"]], user=guest_user,
                          tty=True, check=False)
    if proc.returncode != 0:
        raise AsbxError(f"auth provider command failed on step 1: {spec['command']}")


def run_auth(incus: Incus, instance: str, providers: list, cfg: dict) -> None:
    for provider in providers:
        if isinstance(provider, dict):
            _run_command_provider(incus, instance, provider, cfg["guest_user"])
            continue
        name = provider
        if name not in BUILTIN_AUTH_PROVIDERS:
            raise AsbxError(f"unknown auth provider '{name}'")
        BUILTIN_AUTH_PROVIDERS[name](incus, instance, cfg)
