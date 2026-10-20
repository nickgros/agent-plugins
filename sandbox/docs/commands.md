# Commands

`<group>` is a manifest name: `~/.config/agent-sandbox/projects/<group>.yaml`.

## Host

| Command             | Behaviour                                                                                                    |
| ------------------- | ------------------------------------------------------------------------------------------------------------ |
| `install [--force]` | Creates the `~/.local/bin/asbx` symlink. `--force` replaces an existing file there.                          |
| `init`              | One-time setup ([host setup](host-setup.md#asbx-init)). Never overwrites an existing `config.yaml`.          |
| `doctor [--fix]`    | Readiness report; exits 1 on any FAIL ([checks](host-setup.md#asbx-doctor)). `--fix` rewrites the ACL, adds the Include line and reinstalls the firewall unit. |
| `ingress setup [--create \| --adopt [IFACE]] [--print] [--yes]` | Installs the firewall and ingress config through sudo ([details](host-setup.md#ingress-firewall)). `--create` enables managed WireGuard; `--adopt` adds an existing interface, or lists eligible ones and asks. `--print` prints the files and commands instead; `--yes` skips confirmation. |
| `build-base`        | Bakes the base image and repoints the alias ([details](host-setup.md#asbx-build-base)).                      |

## Sandbox lifecycle

### `up <group> [--no-auth] [--no-provision]`

Idempotent. It refuses to start if any ingress interface is ineligible. Once
the ingress firewall is opted in (installed by `asbx ingress setup`, or any
ingress interface configured), it also refuses unless `asbx-firewall.service`
is active and `/etc/asbx/firewall.nft` matches the current config. Run
`asbx ingress setup` to fix either
([troubleshooting](troubleshooting.md#up-refuses-firewall-unit-inactive-or-out-of-date)).
Steps, in order:

1. Refreshes the shared ACL's rules, and only warns if this fails.
2. Creates the sandbox if it doesn't exist, starts it, and waits for the
   Incus agent and cloud-init. `resources` take effect only at creation;
   changing them later needs `rebuild`.
3. Attaches the egress ACL to the NIC. An existing sandbox without the ACL
   gets it now. If attaching fails, `up` fails. A legacy `guest-nft` sandbox
   is refused. Then it pins the NIC's `ipv4.address` and turns on
   `security.ipv4_filtering` and `security.mac_filtering`. If Incus refuses,
   `up` fails.
4. Syncs mounts. A mount newly added to the manifest is attached, and the
   sandbox restarts. A mount Incus refuses is an error.
5. Rewrites `authorized_keys` (the asbx key plus `ssh.extra_pubkeys`).
6. Writes the SSH config, the env files and the git identity.
7. Runs auth, unless `--no-auth`.
8. Runs provision, unless `--no-provision`.

### `auth <group>` / `provision <group>`

These re-run one phase. `auth` skips the providers that are already
satisfied. `provision` does the following:

1. For each repo, in order: clones it if missing, reconciles its remotes,
   and runs its `setup` (on the first clone only).
2. Runs `docker compose up -d`.
3. Runs the setup hook.

### `start` / `stop` / `restart <group>`

Thin wrappers over the matching `incus` command. `start` and `restart` then
wait, for up to two minutes, until `<instance>.incus:22` accepts a
connection, so an `asbx ssh` straight after doesn't hit "No route to host".
If it never does, they warn and suggest `asbx doctor`.

### `rebuild <group> [--force] [--yes]`

Runs `rm` then `up` with the current manifest. You confirm by typing the
group name; `--yes` skips the prompt. First it classifies every entry under
`/workspace` and refuses if any repo is dirty, has no remote, or has
unpushed commits. `--force` overrides that refusal. It always refuses if
`/workspace` can't be listed (for example because the sandbox is stopped),
rather than treating that as "no repos". The new sandbox keeps the old one's
pinned IPv4 address, set before its first start.

### `rm <group> [--yes]`

Deletes the sandbox and its generated SSH config, and removes the
`<instance>.incus` entry from `ssh.known_hosts`.

## Snapshots

### `snapshot <group> [label]`

The default label is `pre-<UTC timestamp>`. If the storage pool can't
snapshot the VM, `snapshot` falls back to a full `incus copy` named
`<instance>-snap-<label>`. The copy doesn't keep the sandbox's pinned IPv4,
because Incus won't start two NICs with the same static address; `restore`
puts it back. On a `dir` pool it also prints a notice that
snapshots are full root-disk copies.

### `restore <group> <label> [--yes]`

Restores the snapshot `label`, or the `<instance>-snap-<label>` copy if that
is what exists. It errors if the label matches neither. It never deletes the
sandbox before confirming the copy it restores from exists. Afterwards it
restarts the sandbox and rewrites the SSH config.

## Access

| Command                  | Behaviour                                                                                   |
| ------------------------ | ------------------------------------------------------------------------------------------- |
| `ssh <group> [cmd...]`   | `ssh -F <generated config> <instance> [cmd]`                                                |
| `ssh-config <group> (--jump USER@HOST \| --direct) [--identity PATH]` | Print the `~/.ssh/config` stanza for another machine. `--direct` uses the pinned IPv4 address with no `ProxyJump` ([connecting](connecting.md#direct-access-over-an-ingress-interface)). Changes nothing |
| `client-config NAME`     | Print the WireGuard config for client `NAME` under `network.ingress.wireguard.clients`. Needs `endpoint` set. Changes nothing |
| `shell <group> [--root]` | `incus exec` login shell (`shell` config key) as the guest user, or bash as root with `--root`                            |
| `list`                   | For each manifest: instance, group, state, egress mode, and a note if the manifest changed  |
