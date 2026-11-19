from __future__ import annotations

import json
import urllib.error
import urllib.request

from . import config, guest, ui
from .backend import Guest, SandboxBackend
from .errors import AsbxError


# ---------------------------------------------------------------------------
# Node LTS download resolution (host-side, before rendering the recipe)
# ---------------------------------------------------------------------------

NODE_PINNED_VERSION = "v24.20.0"
# Tarball arch -> sha256 of node-<NODE_PINNED_VERSION>-linux-<arch>.tar.xz.
NODE_PINNED_SHA256 = {
    "x64": "2f2c0da162318f0de47665410c7c8c2ed3d36c8f3105de4bbc61176c70a7cbf2",
    "arm64": "5f4ddab610c1ab2016b3c227cebdbf6d9495161487e4739c7b90090595f465f7",
}

# `dpkg --print-architecture` in the guest -> Node's tarball arch.
NODE_DPKG_ARCH = {"amd64": "x64", "arm64": "arm64"}


def resolve_node_downloads(fetch_json=None, fetch_text=None) -> dict[str, tuple[str, str]]:
    """Returns {tarball arch: (url, sha256)} for every arch in
    NODE_DPKG_ARCH; the recipe picks one by the guest's own architecture.
    Falls back to the pinned pairs on any network failure."""
    if fetch_json is None:
        def fetch_json():
            with urllib.request.urlopen("https://nodejs.org/dist/index.json", timeout=10) as r:
                return json.loads(r.read())
    if fetch_text is None:
        def fetch_text(url):
            with urllib.request.urlopen(url, timeout=10) as r:
                return r.read().decode()

    try:
        entries = fetch_json()
        version = None
        for entry in entries:
            if entry.get("lts"):
                version = entry["version"]
                break
        if version is None:
            raise ValueError("no LTS entry found in index.json")
        shasums = fetch_text(f"https://nodejs.org/dist/{version}/SHASUMS256.txt")
        downloads = {}
        for arch in NODE_DPKG_ARCH.values():
            tarball_name = f"node-{version}-linux-{arch}.tar.xz"
            sha256 = None
            for line in shasums.splitlines():
                if line.strip().endswith(tarball_name):
                    sha256 = line.split()[0]
                    break
            if sha256 is None:
                raise ValueError(f"no sha256 for {tarball_name} in SHASUMS256.txt")
            downloads[arch] = (f"https://nodejs.org/dist/{version}/{tarball_name}", sha256)
        return downloads
    except (urllib.error.URLError, OSError, ValueError, KeyError, json.JSONDecodeError):
        ui.warn(f"using pinned Node {NODE_PINNED_VERSION} (nodejs.org unreachable)")
        return {arch: (f"https://nodejs.org/dist/{NODE_PINNED_VERSION}/"
                       f"node-{NODE_PINNED_VERSION}-linux-{arch}.tar.xz", sha)
                for arch, sha in NODE_PINNED_SHA256.items()}


# ---------------------------------------------------------------------------
# Base image recipe
# ---------------------------------------------------------------------------

def render_paseo_unit(guest_user: str) -> str:
    """Paseo daemon as the guest user. A login shell gives it the same env,
    PATH and mise setup an SSH session gets. The condition holds it back on a
    new instance's first boot until `up` has written the guest env and starts
    it (guest.start_paseo); later boots start it here."""
    return f"""[Unit]
Description=Paseo daemon (asbx)
Wants=network-online.target
After=network-online.target
ConditionPathExists={guest.ENV_FILE_PATH}

[Service]
User={guest_user}
WorkingDirectory=/home/{guest_user}
ExecStart=/bin/bash -lc 'exec paseo daemon run'
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


BASE_PACKAGES = (
    "git", "curl", "ca-certificates", "gnupg", "build-essential", "jq", "tmux", "ripgrep",
    "fd-find", "python3", "python3-venv", "python3-pip", "unzip", "xz-utils", "openssh-server",
    "less", "vim", "zsh", "rsync", "nftables", "sudo",
)


def render_base_script(guest_user: str, node: dict[str, tuple[str, str]]) -> str:
    """The whole base-image recipe as one bash script, run as root on a fresh
    Debian VM by `prepare_build_vm`. One script rather than a cloud-config so
    every backend can run it the same way. It is the only place that turns
    ssh password logins off (step 12), on every backend. `node` is
    `resolve_node_downloads()`. Every installer download fails the recipe
    when it fails (a file first, or pipefail in the inner shell)."""
    u = guest_user
    home = f"/home/{u}"
    as_user = f"runuser -u {u} -- env HOME={home}"
    node_cases = "\n".join(
        f"  {dpkg}) node_url={node[arch][0]}; node_sha256={node[arch][1]} ;;"
        for dpkg, arch in NODE_DPKG_ARCH.items())
    return f"""#!/bin/bash
set -euo pipefail
# The guest user's installers inherit this cwd; it must be one they can enter.
cd /
export DEBIAN_FRONTEND=noninteractive

# 1. Base packages
apt-get update
apt-get install -y {' '.join(BASE_PACKAGES)}

# 2. Docker and GitHub CLI official repos
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian trixie stable" > /etc/apt/sources.list.d/docker.list
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /etc/apt/keyrings/githubcli-archive-keyring.gpg
chmod a+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin gh

# 3. Guest user, once the docker group exists
id -u {u} >/dev/null 2>&1 || useradd -m -s /bin/bash {u}
usermod -aG sudo,users,docker {u}
echo '{u} ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/90-asbx-{u}
chmod 0440 /etc/sudoers.d/90-asbx-{u}

# 4. Paseo daemon unit
cat > /etc/systemd/system/{guest.PASEO_UNIT} <<'ASBX_UNIT'
{render_paseo_unit(u)}ASBX_UNIT
chmod 0644 /etc/systemd/system/{guest.PASEO_UNIT}

# 5. Node LTS into /usr/local (never a shell-profile shim), for the guest's architecture
case "$(dpkg --print-architecture)" in
{node_cases}
  *) echo "unsupported guest architecture $(dpkg --print-architecture)" >&2; exit 1 ;;
esac
curl -fsSLo /tmp/node.tar.xz "$node_url"
echo "$node_sha256  /tmp/node.tar.xz" | sha256sum -c -
rm -rf /usr/local/lib/nodejs
mkdir -p /usr/local/lib/nodejs
tar -xJf /tmp/node.tar.xz -C /usr/local/lib/nodejs
mv /usr/local/lib/nodejs/node-* /usr/local/lib/nodejs/node
rm -f /tmp/node.tar.xz
ln -sf /usr/local/lib/nodejs/node/bin/node /usr/local/bin/node
ln -sf /usr/local/lib/nodejs/node/bin/npm /usr/local/bin/npm
ln -sf /usr/local/lib/nodejs/node/bin/npx /usr/local/bin/npx
npm config set prefix /usr/local --location=global

# 6. mise (per-project toolchains only)
curl -fsSLo /tmp/mise-install.sh https://mise.run
MISE_INSTALL_PATH=/usr/local/bin/mise sh /tmp/mise-install.sh
rm -f /tmp/mise-install.sh
echo 'eval "$(/usr/local/bin/mise activate bash)"' > /etc/profile.d/asbx-mise.sh

# 7. AWS CLI v2 for the guest's architecture
curl -fsSLo /tmp/awscliv2.zip "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip"
unzip -q -o -d /tmp /tmp/awscliv2.zip
/tmp/aws/install --update
rm -rf /tmp/aws /tmp/awscliv2.zip

# 8. Harness CLIs (as root, into /usr/local)
npm i -g @openai/codex @google/gemini-cli @getpaseo/cli

# 9. Per-user installers, as the guest user so their self-updates work:
#    opencode (must not run as root), omp (~/.local/bin), Claude Code (~/.local/bin)
{as_user} bash -c 'set -o pipefail; curl -fsSL https://opencode.ai/install | bash'
ln -sf {home}/.opencode/bin/opencode /usr/local/bin/opencode
{as_user} bash -c 'set -o pipefail; curl -fsSL https://raw.githubusercontent.com/can1357/oh-my-pi/main/scripts/install.sh | sh -s -- --binary'
{as_user} bash -c 'set -o pipefail; curl -fsSL https://claude.ai/install.sh | bash'
# Paseo: no server-side speech models; clients do dictation locally
{as_user} paseo daemon config set features.dictation.enabled false

# 10. fd-find is installed as fdfind on Debian
ln -sf /usr/bin/fdfind /usr/local/bin/fd

# 11. workspace + skill symlinks
mkdir -p /workspace {home}/.asbx {home}/.agents/skills {home}/.claude {home}/.codex {home}/.config
ln -sfn {home}/.agents/skills {home}/.claude/skills
ln -sfn {home}/.agents/skills {home}/.codex/skills
chown -R {u}:{u} /workspace {home}

# 12. ssh: keys only. sshd takes the first value it reads, and Debian reads
#     sshd_config.d first, so drop any drop-in that turns passwords on.
for f in /etc/ssh/sshd_config.d/*.conf; do
  [ -e "$f" ] || continue
  if grep -qiE '^[[:space:]]*PasswordAuthentication[[:space:]]+yes' "$f"; then rm -f "$f"; fi
done
echo 'PasswordAuthentication no' > /etc/ssh/sshd_config.d/00-asbx.conf
sed -i 's/^#\\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl daemon-reload
systemctl enable ssh {guest.PASEO_UNIT}
"""


def is_elf(path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"\x7fELF"
    except OSError:
        return False


def copy_host_binaries(g: Guest, guest_user: str, binaries: list[str]) -> None:
    """Copy host binaries into the guest user's ~/.local/bin, owned by that
    user. Only Linux (ELF) binaries can run in the guest; a macOS host's
    Mach-O builds are skipped."""
    present = []
    for local_bin in binaries:
        local_path = config.expand(local_bin)
        if not local_path.exists():
            ui.warn(f"copy_binaries: {local_path} not found on host, skipping")
        elif not is_elf(local_path):
            ui.warn(f"copy_binaries: {local_path} is not a Linux (ELF) binary, skipping")
        else:
            present.append(local_path)
    if not present:
        return

    bin_dir = f"/home/{guest_user}/.local/bin"
    uid = g.uid_of(guest_user)
    g.exec(["mkdir", "-p", bin_dir], user=guest_user)
    for local_path in present:
        ui.ok(f"copying {local_path} -> {bin_dir}/{local_path.name}")
        g.push_file(str(local_path), f"{bin_dir}/{local_path.name}", mode="0755", uid=uid, gid=uid)


BASE_RECIPE_PATH = "/root/asbx-base.sh"


def prepare_build_vm(g: Guest, cfg: dict, recipe: str) -> None:
    """Everything `build-base` does inside the build VM on every backend:
    runs `recipe` as root, copies the host binaries in, then cleans the VM
    for imaging, so each sandbox gets its own host keys and machine id."""
    g.push_text(BASE_RECIPE_PATH, recipe, mode="0700", uid=0, gid=0)
    ui.ok("running the base recipe (installs docker, node, harness CLIs...)")
    proc = g.exec(["bash", BASE_RECIPE_PATH], user="root", tty=True, check=False)
    if proc.returncode != 0:
        raise AsbxError(f"base recipe failed in {g.instance} (exit {proc.returncode})")
    g.exec(["rm", "-f", BASE_RECIPE_PATH], user="root")

    guest_user = cfg["guest_user"]
    copy_host_binaries(g, guest_user, cfg["defaults"]["copy_binaries"])

    ui.ok("cleaning the VM for imaging")
    g.exec(["cloud-init", "clean", "--logs", "--machine-id"], user="root")
    g.exec(["bash", "-c", "rm -f /etc/ssh/ssh_host_*"], user="root")
    g.exec(["truncate", "-s0", f"/home/{guest_user}/.bash_history"], user="root", check=False)
    g.exec(["apt-get", "clean"], user="root")


def cmd_build_base(backend: SandboxBackend, cfg: dict) -> None:
    recipe = render_base_script(cfg["guest_user"], resolve_node_downloads())
    backend.build_base(cfg, lambda g: prepare_build_vm(g, cfg, recipe))
