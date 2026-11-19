"""Guest-side operations: acting on a live sandbox's OS through a
`backend.Guest` (hostname, `authorized_keys`, env, shell, git identity), plus
the mount readability rule every backend applies."""
from __future__ import annotations

import json
import re
import stat
from pathlib import Path
from typing import Optional

from . import config
from . import host
from . import ui
from .backend import Guest
from .errors import AsbxError


# ---------------------------------------------------------------------------
# Mount readability check (pure, testable without a VM)
# ---------------------------------------------------------------------------

def mount_readable_error(host_path: Path) -> Optional[str]:
    """Checks the mount source directory itself. Ancestor directories are
    NOT walked: virtiofs mounts are served by the VM service running as
    host root, so parent-directory traversal permissions never block the
    guest.

    The guest's uid is its own namespace's, unrelated to any host uid, so
    world read+execute is what the guest actually needs — the same bar
    doctor and `up` both apply."""
    resolved = host_path.expanduser().resolve()
    msg = f"mount {host_path}: not readable by the sandbox (chmod o+rX {host_path})"
    try:
        st = resolved.stat()
    except OSError:
        return msg
    needed = stat.S_IROTH | stat.S_IXOTH
    if (st.st_mode & needed) != needed:
        return msg
    return None


def render_env_file(env: dict[str, str]) -> str:
    lines = []
    for k, v in env.items():
        escaped = str(v).replace("'", "'\\''")
        lines.append(f"export {k}='{escaped}'")
    return "\n".join(lines) + ("\n" if lines else "")


def render_etc_environment(guest_user: str) -> str:
    """PAM-session PATH (sshd via pam_env). Mise shims come first so repo pins
    beat the base-image Node in non-interactive `ssh host cmd` sessions."""
    home = f"/home/{guest_user}"
    return ("# managed by asbx: rewritten on every 'asbx up'\n"
            f'PATH="{home}/.local/share/mise/shims:{home}/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/games"\n')


MISE_SYSTEM_CONFIG = "/etc/mise/config.toml"


def render_mise_config(tools: dict[str, str]) -> str:
    """System-scope mise config: trusts /workspace (the guest user already has
    passwordless root, so this grants nothing new) and carries group-wide tools.
    json.dumps yields valid TOML basic strings, including for names like
    `aqua:apache/maven`."""
    lines = [
        "# managed by asbx: rewritten on every 'asbx up' and 'asbx provision'",
        "[settings]",
        'trusted_config_paths = ["/workspace"]',
    ]
    if tools:
        lines += ["", "[tools]"]
        lines += [f"{json.dumps(k)} = {json.dumps(v)}" for k, v in tools.items()]
    return "\n".join(lines) + "\n"


def write_mise_config(g: Guest, tools: dict[str, str]) -> None:
    """System scope (not ~/.config/mise) so the agent's own `mise use -g` edits
    are never clobbered."""
    g.exec(["mkdir", "-p", "/etc/mise"], user="root")
    g.push_text(MISE_SYSTEM_CONFIG, render_mise_config(tools), mode="0644", uid=0, gid=0)


def wait_for_cloud_init(g: Guest) -> None:
    """Waits for the image's own cloud-init at boot (on a fresh sandbox it
    regenerates the ssh host keys `build-base` removed). asbx passes no
    user-data; this only surfaces image failures."""
    g.exec(["cloud-init", "status", "--wait"], user="root", check=False)
    status = g.exec(["cloud-init", "status"], user="root", check=False)
    if "error" in (status.stdout or ""):
        tail = g.exec(["cloud-init", "status", "--long"], user="root", check=False)
        raise AsbxError(f"cloud-init failed in {g.instance}:\n{tail.stdout}")


def set_hostname(g: Guest) -> None:
    """The guest's hostname is its sandbox name; set on every `up`, since no
    backend passes it in at creation."""
    g.exec(["hostnamectl", "set-hostname", g.instance], user="root")


_PUBKEY_RE = re.compile(r"^(ssh-|ecdsa-|sk-)\S+ \S+( [^\r\n]*)?$")


def validate_pubkey(key: str) -> str:
    """Returns `key` stripped, or raises. Accepts only a bare single-line key
    (type, base64, optional comment): a leading authorized_keys option such as
    `command=` would otherwise ride along into the guest's authorized_keys."""
    stripped = key.strip()
    if not _PUBKEY_RE.match(stripped):
        raise AsbxError(f"ssh.extra_pubkeys: not a single-line public key: {stripped[:40]!r}")
    return stripped


def render_authorized_keys(primary: str, extras: list[str]) -> str:
    keys: list[str] = []
    for key in [primary, *extras]:
        valid = validate_pubkey(key)
        if valid not in keys:
            keys.append(valid)
    return "# managed by asbx: rewritten on every 'asbx up'\n" + "\n".join(keys) + "\n"


def sync_authorized_keys(g: Guest, cfg: dict) -> None:
    """Makes the guest user's authorized_keys the asbx key plus
    `ssh.extra_pubkeys`, on every `up`. This is the only way keys reach a
    sandbox, so a key added to config reaches an existing one too."""
    guest_user = cfg["guest_user"]
    pub_path = Path(str(config.expand(cfg["ssh"]["key"])) + ".pub")
    if not pub_path.exists():
        raise AsbxError(f"ssh public key not found at {pub_path}; run 'asbx init' first")
    body = render_authorized_keys(pub_path.read_text(), cfg["ssh"]["extra_pubkeys"])
    uid = g.uid_of(guest_user)
    ssh_dir = f"/home/{guest_user}/.ssh"
    g.exec(["install", "-d", "-m", "0700", "-o", str(uid), "-g", str(uid), ssh_dir], user="root")
    g.push_text(f"{ssh_dir}/authorized_keys", body, mode="0600", uid=uid, gid=uid)


WORKSPACE_CD_PATH = "/etc/profile.d/asbx-cwd.sh"
ENV_FILE_PATH = "/etc/profile.d/asbx-env.sh"
PASEO_UNIT = "asbx-paseo.service"


def render_workspace_cd(workspace: str = "/workspace") -> str:
    """Interactive ssh logins start in /workspace instead of $HOME. Gated on a
    tty so `ssh host cmd`, scp and VSCode's server bootstrap are untouched, and
    on SSH_CONNECTION so `asbx shell` and the provision hook are too."""
    return ("# managed by asbx: rewritten on every 'asbx up'\n"
            f'if [ -n "$SSH_CONNECTION" ] && [ -t 0 ] && [ "$PWD" = "$HOME" ] && [ -d {workspace} ]; then\n'
            f"    cd {workspace}\n"
            "fi\n")


def write_guest_env(g: Guest, cfg: dict, manifest_env: dict) -> None:
    guest_user = cfg["guest_user"]
    env = {**cfg["harness_env"], **cfg["defaults"]["env"], **manifest_env}
    g.push_text(ENV_FILE_PATH, render_env_file(env), mode="0644", uid=0, gid=0)
    g.push_text(WORKSPACE_CD_PATH, render_workspace_cd(), mode="0644", uid=0, gid=0)
    g.push_text("/etc/environment", render_etc_environment(guest_user), mode="0644", uid=0, gid=0)

    claude_settings = json.dumps({"env": {**cfg["harness_env"], **manifest_env}}, indent=2) + "\n"
    guest_uid = g.uid_of(guest_user)
    g.exec(["mkdir", "-p", f"/home/{guest_user}/.claude"], user=guest_user)
    g.push_text(f"/home/{guest_user}/.claude/settings.json", claude_settings,
                mode="0644", uid=guest_uid, gid=guest_uid)


def start_paseo(g: Guest) -> None:
    """Starts the base image's Paseo daemon unit once the guest env exists.
    `start` leaves a running daemon, and its agents, alone; env changes reach
    it on the next boot or `systemctl restart`."""
    proc = g.exec(["systemctl", "start", PASEO_UNIT], user="root", check=False)
    if proc.returncode != 0:
        ui.warn(f"{g.instance}: paseo daemon not started; images built before {PASEO_UNIT} lack it "
                f"(asbx build-base, then asbx rebuild):\n{(proc.stderr or '').strip()}")
        return
    ui.ok(f"{g.instance}: paseo daemon running")


ZPROFILE_PATH = "/etc/zsh/zprofile"


def render_zprofile() -> str:
    """Debian's zsh never sources /etc/profile.d; mirror what bash login shells get there."""
    return ("# managed by asbx: rewritten on every 'asbx up'\n"
            f"[[ -r {ENV_FILE_PATH} ]] && emulate sh -c '. {ENV_FILE_PATH}'\n"
            f"[[ -r {WORKSPACE_CD_PATH} ]] && emulate sh -c '. {WORKSPACE_CD_PATH}'\n"
            '[[ -x /usr/local/bin/mise ]] && eval "$(/usr/local/bin/mise activate zsh)"\n')


def configure_shell(g: Guest, cfg: dict) -> None:
    name = cfg["shell"]
    path = config.SHELL_PATHS[name]
    guest_user = cfg["guest_user"]
    if name == "zsh":
        if g.exec(["test", "-x", path], user="root", check=False).returncode != 0:
            ui.ok(f"{g.instance}: installing zsh")
            proc = g.exec(
                ["bash", "-c", "apt-get update -q && DEBIAN_FRONTEND=noninteractive apt-get install -y -q zsh"],
                user="root", check=False)
            if proc.returncode != 0:
                raise AsbxError(f"installing zsh in {g.instance} failed (exit {proc.returncode})"
                                + (f"\n{(proc.stderr or '').strip()}" if proc.stderr else ""))
        g.push_text(ZPROFILE_PATH, render_zprofile(), mode="0644", uid=0, gid=0)
        # Without any rc file, zsh starts its interactive newuser wizard.
        g.exec(["bash", "-c", "test -e ~/.zshrc || : > ~/.zshrc"], user=guest_user)
    g.exec(["usermod", "-s", path, guest_user], user="root")
    ui.ok(f"{g.instance}: login shell {name}")


def write_git_identity(g: Guest, cfg: dict) -> None:
    name = cfg["defaults"]["git"]["name"]
    email = cfg["defaults"]["git"]["email"]
    if not name:
        name = host.git_config_get("user.name")
    if not email:
        email = host.git_config_get("user.email")
    guest_user = cfg["guest_user"]
    if name:
        g.exec(["git", "config", "--global", "user.name", name], user=guest_user)
    if email:
        g.exec(["git", "config", "--global", "user.email", email], user=guest_user)
    g.exec(["git", "config", "--global", "init.defaultBranch", "main"], user=guest_user)
    g.exec(["git", "config", "--global", "push.autoSetupRemote", "true"], user=guest_user)
