# Configuration: `config.yaml`

`~/.config/agent-sandbox/config.yaml` is the host-wide configuration. Set
`ASBX_CONFIG_DIR` to use another directory. `asbx init` writes every default
into it.

Merge rules:

- **Unknown keys:** a hard error naming the key.
- **Mappings:** merged key by key over the built-in defaults.
- **Lists:** replace the default list entirely.

Because `init` writes every default, a default list added in a newer `asbx`
does not reach an existing `config.yaml`. Add the new entries yourself.
`network.block_cidrs` is an example.

| Key                         | Default                                     | Meaning                                                                                                     |
| --------------------------- | ------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| `guest_user`                | `agent`                                     | Guest account. Used in `/home/<guest_user>` paths and the SSH `User`. Baked in at `build-base`.             |
| `instance_prefix`           | `sandbox-`                                  | Instance name is `<prefix><manifest name>`                                                                  |
| `image_alias`               | `agent-sandbox-base`                        | Rolling alias `build-base` publishes and `up` creates from                                                  |
| `base_image_source`         | `images:debian/13/cloud`                    | Image `build-base` starts from                                                                              |
| `network.name`              | `incusbr0`                                  | Incus bridge the sandboxes attach to                                                                        |
| `network.acl`               | `asbx-egress`                               | Name of the egress ACL                                                                                      |
| `network.dns_domain`        | `incus`                                     | Bridge `dns.domain`. SSH `HostName` is `<instance>.<dns_domain>`.                                           |
| `network.block_cidrs`       | RFC1918, link-local, `100.64.0.0/10`, ULA   | Egress drop list ([security](security.md#network-egress))                                                   |
| `ssh.key`                   | `~/.config/agent-sandbox/id_ed25519`        | Key used from the host to the guests                                                                        |
| `ssh.config_dir`            | `~/.ssh/agent-sandbox.d`                    | Generated per-instance SSH configs                                                                          |
| `ssh.known_hosts`           | `~/.config/agent-sandbox/known_hosts`       | Dedicated known-hosts file. `rm` and `up` purge the instance's entry.                                       |
| `ssh.extra_pubkeys`         | `[]`                                        | Additional `authorized_keys` lines for the guest user ([connecting](connecting.md#from-another-machine))    |
| `defaults.resources`        | `cpu: 4`, `memory: 8GiB`, `disk: 30GiB`     | Per-sandbox limits, overridable per manifest. `disk` also sizes the base image build.                       |
| `defaults.auth`             | `[github-gh, aws-creds]`                    | Auth providers. A manifest's `auth` replaces this list.                                                     |
| `defaults.mounts`           | `~/.agents/skills` read-only                | Mounts for every sandbox. A manifest entry with the same `guest` path replaces one.                         |
| `defaults.copy_binaries`    | `[]`                                        | Host binaries `build-base` copies into the guest user's `~/.local/bin`. Missing ones are skipped with a warning. omp is installed in the image, not copied. |
| `defaults.env`              | `{}`                                        | Guest environment for every sandbox                                                                         |
| `defaults.git.name`/`email` | empty                                       | Guest git identity. Empty means the host's `git config user.name`/`user.email`.                             |
| `defaults.remotes`          | `{}`                                        | Extra git remotes for every repo ([manifest](manifest.md))                                                  |
| `aws.profiles`              | `[sage-bedrock]`                            | Host AWS profiles whose short-lived credentials `aws-creds` pushes into sandboxes. A manifest's `aws_profiles` adds to it. |
| `harness_env`               | Bedrock settings for Claude Code            | Guest environment for the harness CLIs. Also written to `~/.claude/settings.json`.                          |
| `harness_settings.sync`     | `[omp]`                                     | Host harness files pushed into every sandbox on `up` and `asbx settings-sync`. Built-in item names or `{host, guest}` entries; credential items are rejected here. |
| `harness_settings.credentials` | `[]`                                     | Opt-in credential items (`omp-mcp`, `omp-env`, `omp-secrets`) or `{host, guest}` entries, pushed as mode 0600. A manifest's `harness_credentials` adds to it. |

Guest environment precedence, lowest to highest: `harness_env`,
`defaults.env`, then the manifest's `env`. The result is written to
`/etc/profile.d/asbx-env.sh`.
