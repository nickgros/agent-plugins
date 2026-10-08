# asbx: agent sandbox VMs on Incus

`asbx` creates isolated Incus **VMs** you can hand to an LLM agent. Each
project group gets one long-lived VM. Inside it are the group's git repos,
Docker Compose services, a provision hook, and the harness CLIs (`claude`,
`codex`, `gemini`, `opencode`, `omp`). You reach it over SSH, including from
VSCode and JetBrains.

## Security model

The VM is the boundary. Restrictions on guest privileges are not part of it.

- **Network:** the guest can reach the internet but not your LAN, your
  tailnet, or any other port on the host. An Incus ACL on the VM's NIC
  enforces this. If the ACL can't be attached, `up` fails.
- **Files:** the guest can't read host files except declared mounts, and
  those are always read-only.
- **Guest root:** the guest user has passwordless root inside the VM.
  Anything credentials or auth providers put inside the VM, the agent can
  use.

Details: [docs/security.md](docs/security.md). Rationale:
[docs/adr/](docs/adr/).

## Requirements

- Linux host with `/dev/kvm`.
- Incus 7.0 LTS. The minimum is 6.0.4 on the 6.0 LTS, or 6.10. Older
  versions can't attach an ACL to a bridged NIC.
- Python 3 with PyYAML.

## Quick start

```sh
sandbox/asbx install   # symlink to ~/.local/bin/asbx
asbx init              # config, SSH key, network ACL, ~/.ssh/config Include
asbx build-base        # bake the base image (several minutes)
asbx doctor            # check host readiness; --fix repairs what it can
```

Then point the host's resolver at the bridge's DNS so `<instance>.incus`
resolves ([host setup: name resolution](docs/host-setup.md#name-resolution)).

Write a manifest at `~/.config/agent-sandbox/projects/demo.yaml`:

```yaml
repos:
  - url: https://github.com/nickgros/agent-plugins.git
auth: [github-gh]   # the default also runs aws-sso, which needs ~/.aws/config
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
| `auth <group>`                       | Re-run the auth providers that aren't satisfied                 |
| `provision <group>`                  | Re-run clone, compose and setup hook                            |
| `list`                               | List every manifest's sandbox and its state                     |
| `ssh <group> [cmd...]`               | SSH into the sandbox                                            |
| `shell <group> [--root]`             | `incus exec` shell, for when SSH is broken                      |
| `start` / `stop` / `restart <group>` | Incus lifecycle wrappers                                        |
| `snapshot <group> [label]`           | Snapshot the sandbox                                            |
| `restore <group> <label>`            | Restore a snapshot                                              |
| `rebuild <group>`                    | Recreate from the current manifest; refuses if work would be lost |
| `rm <group>`                         | Delete the sandbox and its SSH config                           |

Flags and edge cases: [docs/commands.md](docs/commands.md).

## Documentation

- [Host setup](docs/host-setup.md): `init`, base image, name resolution,
  host firewall, `doctor`.
- [Configuration](docs/configuration.md): `config.yaml`.
- [Manifests](docs/manifest.md): per-group YAML and auth providers.
- [Connecting](docs/connecting.md): IDEs, and access from another machine.
- [Commands](docs/commands.md)
- [Security model](docs/security.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Architecture decisions](docs/adr/)
- [Development](asbxlib/README.md): module map and tests.
