from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone

from . import config, guest, ui
from .errors import AsbxError
from .incus import Incus


# ---------------------------------------------------------------------------
# Node LTS download resolution (host-side, before rendering cloud-init)
# ---------------------------------------------------------------------------

NODE_PINNED_VERSION = "v24.20.0"
NODE_PINNED_SHA256 = "2f2c0da162318f0de47665410c7c8c2ed3d36c8f3105de4bbc61176c70a7cbf2"


def resolve_node_download(fetch_json=None, fetch_text=None) -> tuple[str, str]:
    """Returns (tarball_url, sha256). Falls back to the pinned pair on any
    network failure; never raises."""
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
        tarball_name = f"node-{version}-linux-x64.tar.xz"
        shasums = fetch_text(f"https://nodejs.org/dist/{version}/SHASUMS256.txt")
        sha256 = None
        for line in shasums.splitlines():
            if line.strip().endswith(tarball_name):
                sha256 = line.split()[0]
                break
        if sha256 is None:
            raise ValueError(f"no sha256 for {tarball_name} in SHASUMS256.txt")
        url = f"https://nodejs.org/dist/{version}/{tarball_name}"
        return url, sha256
    except (urllib.error.URLError, OSError, ValueError, KeyError, json.JSONDecodeError):
        ui.warn(f"using pinned Node {NODE_PINNED_VERSION} (nodejs.org unreachable)")
        url = f"https://nodejs.org/dist/{NODE_PINNED_VERSION}/node-{NODE_PINNED_VERSION}-linux-x64.tar.xz"
        return url, NODE_PINNED_SHA256


# ---------------------------------------------------------------------------
# Base image cloud-init render
# ---------------------------------------------------------------------------

def render_base_cloud_init(guest_user: str, node_url: str, node_sha256: str) -> str:
    return f"""#cloud-config
users:
  - default
  - name: {guest_user}
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    groups: [sudo, users, docker]

packages:
  - git
  - curl
  - ca-certificates
  - gnupg
  - build-essential
  - jq
  - tmux
  - ripgrep
  - fd-find
  - python3
  - python3-venv
  - python3-pip
  - unzip
  - xz-utils
  - openssh-server
  - gh
  - less
  - vim
  - rsync
  - nftables

runcmd:
  # 1. Docker official repo
  - install -m 0755 -d /etc/apt/keyrings
  - curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
  - chmod a+r /etc/apt/keyrings/docker.asc
  - >-
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc]
    https://download.docker.com/linux/debian trixie stable" > /etc/apt/sources.list.d/docker.list
  - apt-get update
  - apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
  # 2. Tailscale
  - curl -fsSL https://tailscale.com/install.sh | sh
  - systemctl enable tailscaled
  # 3. Node LTS into /usr/local (never a shell-profile shim)
  - curl -fsSLo /tmp/node.tar.xz {node_url}
  - echo "{node_sha256}  /tmp/node.tar.xz" | sha256sum -c -
  - mkdir -p /usr/local/lib/nodejs
  - tar -xJf /tmp/node.tar.xz -C /usr/local/lib/nodejs
  - bash -c 'mv /usr/local/lib/nodejs/node-* /usr/local/lib/nodejs/node'
  - ln -sf /usr/local/lib/nodejs/node/bin/node /usr/local/bin/node
  - ln -sf /usr/local/lib/nodejs/node/bin/npm /usr/local/bin/npm
  - ln -sf /usr/local/lib/nodejs/node/bin/npx /usr/local/bin/npx
  - npm config set prefix /usr/local --location=global
  # 4. mise (per-project toolchains only)
  - MISE_INSTALL_PATH=/usr/local/bin/mise sh -c "$(curl -fsSL https://mise.run)"
  - echo 'eval "$(/usr/local/bin/mise activate bash)"' > /etc/profile.d/asbx-mise.sh
  # 5. AWS CLI v2
  - curl -fsSLo /tmp/awscliv2.zip https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip
  - unzip -q -d /tmp /tmp/awscliv2.zip
  - /tmp/aws/install
  # 6. Harness CLIs (as root, into /usr/local)
  - npm i -g @anthropic-ai/claude-code @openai/codex @google/gemini-cli
  # 7. opencode (must not run as root)
  - runuser -u {guest_user} -- bash -c 'curl -fsSL https://opencode.ai/install | bash'
  - ln -sf /home/{guest_user}/.opencode/bin/opencode /usr/local/bin/opencode
  # 8. fd-find is installed as fdfind on Debian
  - ln -sf /usr/bin/fdfind /usr/local/bin/fd
  # 9. workspace + skill symlinks
  - mkdir -p /workspace /home/{guest_user}/.asbx /home/{guest_user}/.agents/skills /home/{guest_user}/.claude /home/{guest_user}/.codex /home/{guest_user}/.config
  - ln -sf /home/{guest_user}/.agents/skills /home/{guest_user}/.claude/skills
  - ln -sf /home/{guest_user}/.agents/skills /home/{guest_user}/.codex/skills
  - chown -R {guest_user}:{guest_user} /workspace /home/{guest_user}
  # 10. ssh
  - systemctl enable ssh
  - sed -i 's/^#\\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
"""


BASE_BUILD_INSTANCE = "asbx-base-build"


def cmd_build_base(incus: Incus, cfg: dict) -> None:
    if incus.instance_exists(BASE_BUILD_INSTANCE):
        raise AsbxError(f"instance {BASE_BUILD_INSTANCE} already exists; delete it first: incus delete --force {BASE_BUILD_INSTANCE}")

    node_url, node_sha256 = resolve_node_download()
    cloud_init = render_base_cloud_init(cfg["guest_user"], node_url, node_sha256)

    ui.ok(f"launching {BASE_BUILD_INSTANCE} from {cfg['base_image_source']}")
    resources = cfg["defaults"]["resources"]
    incus.run(
        "launch", cfg["base_image_source"], BASE_BUILD_INSTANCE, "--vm",
        "-c", f"limits.cpu={resources['cpu']}",
        "-c", f"limits.memory={resources['memory']}",
        "-d", f"root,size={resources['disk']}",
        "-c", f"cloud-init.user-data={cloud_init}",
    )

    ui.ok("waiting for cloud-init to finish (this installs docker, node, harness CLIs...)")
    guest.wait_for_agent(incus, BASE_BUILD_INSTANCE, attempts=150, interval=2.0)
    guest.wait_for_cloud_init(incus, BASE_BUILD_INSTANCE)

    for local_bin in cfg["defaults"]["copy_binaries"]:
        local_path = config.expand(local_bin)
        if not local_path.exists():
            ui.warn(f"copy_binaries: {local_path} not found on host, skipping")
            continue
        name = local_path.name
        ui.ok(f"copying {local_path} -> /usr/local/bin/{name}")
        incus.file_push(BASE_BUILD_INSTANCE, str(local_path), f"/usr/local/bin/{name}", mode="0755")

    ui.ok("cleaning instance for imaging")
    incus.exec_in(BASE_BUILD_INSTANCE, ["cloud-init", "clean", "--logs", "--machine-id"], user="root")
    incus.exec_in(BASE_BUILD_INSTANCE, ["bash", "-c", "rm -f /etc/ssh/ssh_host_*"], user="root")
    incus.exec_in(BASE_BUILD_INSTANCE, ["truncate", "-s0", f"/home/{cfg['guest_user']}/.bash_history"], user="root", check=False)
    incus.exec_in(BASE_BUILD_INSTANCE, ["apt-get", "clean"], user="root")

    ui.ok(f"stopping and publishing {BASE_BUILD_INSTANCE}")
    incus.run("stop", BASE_BUILD_INSTANCE)
    date_alias = f"{cfg['image_alias']}-{datetime.now(timezone.utc):%Y%m%d}"
    incus.image_alias_delete(date_alias)
    incus.run("publish", BASE_BUILD_INSTANCE, "--alias", date_alias, "--force")

    fingerprint = incus.image_alias_exists(date_alias)
    if not fingerprint:
        raise AsbxError(f"publish succeeded but alias {date_alias} not found")

    incus.image_alias_delete(cfg["image_alias"])
    incus.image_alias_create(cfg["image_alias"], fingerprint)
    incus.run("delete", BASE_BUILD_INSTANCE, "--force")
    ui.ok(f"base image ready: {cfg['image_alias']} -> {date_alias} ({fingerprint[:12]})")
