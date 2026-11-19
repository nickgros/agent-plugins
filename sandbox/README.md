# asbx: agent sandbox VMs on Incus or Tart

`asbx` creates isolated **VMs** you can hand to an LLM agent. Each
project group gets one long-lived VM. Inside it are the group's git repos,
Docker Compose services, a provision hook, and the harness CLIs (`claude`,
`codex`, `gemini`, `opencode`, `omp`), with a Paseo daemon (`paseo`) running
at boot. You reach it over SSH, including from VSCode and JetBrains.

One backend per host OS ([ADR 0006](docs/adr/0006-one-backend-per-host-os.md)):
Incus on Linux, Tart with Softnet on macOS. `backend: auto` (the default)
picks by OS.

## Security model

The VM is the boundary. Restrictions on guest privileges are not part of it.

- **Network:** the guest can reach the internet but not your LAN, your
  tailnet, or any other port on the host. Incus: an ACL on the VM's NIC; if
  it can't be attached, `up` fails. Inbound default-deny is opt-in: once
  `asbx ingress setup` installs it, a host firewall admits only traffic on
  ingress interfaces. Each NIC is pinned to one IPv4 address. Tart: Softnet
  rules on `tart run`'s command line, so a VM can't boot without them; IPv6
  is dropped.
- **Files:** the guest can't read host files except declared mounts, and
  those are always read-only.
- **Guest root:** the guest user has passwordless root inside the VM.
  Anything credentials or auth providers put inside the VM, the agent can
  use.

Details: [docs/security.md](docs/security.md). Rationale:
[docs/adr/](docs/adr/).

## Requirements

- Python 3 with PyYAML, and git.
- **Linux:** `/dev/kvm`, and Incus 7.0 LTS. The minimum is 6.0.4 on the
  6.0 LTS, or 6.10. Older versions can't attach an ACL to a bridged NIC.
- **macOS:** 26 or later, Apple silicon, Homebrew. Tart, with Softnet
  installed root-owned and setuid ([why](docs/host-setup.md#softnet)); the
  install script sets both up.

## Install

asbx runs from a clone of this repository. The install script clones it to
`~/.local/share/asbx` (or updates an existing clone), installs PyYAML with
`pip --user` if `python3` lacks it, and links `asbx` into `~/.local/bin`. On
macOS it also runs `brew install openai/tools/tart` and installs Softnet
with `sudo`, after showing the commands and asking. Rerun it to update.

```sh
curl -fsSL https://raw.githubusercontent.com/nickgros/agent-plugins/main/sandbox/install.sh | bash
```

To read the script first, download it, review it, then run it with
`bash install.sh`. It doesn't run until it has downloaded completely, and
`ASBX_HOME` changes where it clones.

By hand:

```sh
git clone https://github.com/nickgros/agent-plugins.git ~/.local/share/asbx
~/.local/share/asbx/sandbox/asbx install   # symlink to ~/.local/bin/asbx
# macOS: brew install openai/tools/tart, then
sudo install -d -o root -g wheel -m 0755 /usr/local/libexec/asbx
sudo install -o root -g wheel -m 4555 "$(brew --prefix softnet)/bin/softnet" /usr/local/libexec/asbx/softnet
```

Update a hand-made clone with `git -C ~/.local/share/asbx pull`. On macOS,
redo the second `sudo install` after `brew upgrade` updates Softnet.

## Quick start

```sh
asbx init              # config, SSH key, network ACL, ~/.ssh/config Include
asbx build-base        # bake the base image (several minutes)
asbx doctor            # check host readiness; --fix repairs what it can
```

Linux only: point the host's resolver at the bridge's DNS so
`<instance>.incus` resolves
([host setup: name resolution](docs/host-setup.md#name-resolution)).

Write a manifest at `~/.config/agent-sandbox/projects/demo.yaml`:

```yaml
repos:
  - url: https://github.com/nickgros/agent-plugins.git
auth: [github-gh]   # the default also runs aws-creds, which needs a valid host `aws sso login`
```

```sh
asbx up demo           # create, start, auth, provision
asbx ssh demo          # or connect to host `sandbox-demo` from your IDE
```

## Commands

| Command                              | Does                                                            |
| ------------------------------------ | --------------------------------------------------------------- |
| `init`                               | One-time host setup                                             |
| `doctor [--fix]`                     | Host and sandbox readiness report                               |
| `install [--force]`                  | Symlink `asbx` into `~/.local/bin`                              |
| `build-base`                         | Bake or refresh the golden base image                           |
| `up <group>`                         | Create or start a sandbox, then auth and provision it           |
| `auth <group>`                       | Re-run the auth providers that aren't satisfied (`aws-creds` always re-pushes) |
| `aws-refresh [group] [--install-timer]` | Push fresh AWS credentials into running sandboxes; install the job that re-pushes every 10 minutes and after SSO login (systemd user units on Linux, a LaunchAgent on macOS) |
| `settings-sync [group]`              | Push host harness settings (`harness_settings`) into running sandboxes; `up` does this too |
| `provision <group>`                  | Re-run clone, compose and setup hook                            |
| `list`                               | List every manifest's sandbox and its state                     |
| `ssh <group> [cmd...]`               | SSH into the sandbox                                            |
| `shell <group> [--root]`             | Host-side shell (`incus exec` / `tart exec`), for when SSH is broken |
| `ssh-config <group> --jump USER@HOST \| --direct` | Print the SSH stanza for another machine (`--direct`: Incus only) |
| `ssh-proxy <instance>`               | Relay stdin/stdout to the sandbox's port 22; the generated `ProxyCommand` on Tart |
| `ingress setup [--create \| --adopt [IFACE]]` | Install the host firewall; set up managed WireGuard or adopt an interface (Incus only) |
| `client-config NAME`                 | Print a WireGuard client config (Incus only)                    |
| `start` / `stop` / `restart <group>` | VM lifecycle wrappers                                           |
| `snapshot <group> [label]`           | Snapshot the sandbox                                            |
| `restore <group> <label>`            | Restore a snapshot                                              |
| `rebuild <group>`                    | Recreate from the current manifest; refuses if work would be lost |
| `rm <group>`                         | Delete the sandbox and its SSH config                           |

Flags and edge cases: [docs/commands.md](docs/commands.md).

## Documentation

- [Host setup](docs/host-setup.md): `init`, base image, macOS, name
  resolution, host firewall, `doctor`.
- [Configuration](docs/configuration.md): `config.yaml`.
- [Manifests](docs/manifest.md): per-group YAML and auth providers.
- [Connecting](docs/connecting.md): IDEs, and access from another machine.
- [Commands](docs/commands.md)
- [Security model](docs/security.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Architecture decisions](docs/adr/)
- [Development](asbxlib/README.md): module map and tests.
