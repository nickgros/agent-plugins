# Commands

`<group>` is a manifest name: `~/.config/agent-sandbox/projects/<group>.yaml`.

## Host

| Command             | Behaviour                                                                                                    |
| ------------------- | ------------------------------------------------------------------------------------------------------------ |
| `install [--force]` | Creates the `~/.local/bin/asbx` symlink. `--force` replaces an existing file there.                          |
| `init`              | One-time setup ([host setup](host-setup.md#asbx-init)). Never overwrites an existing `config.yaml`.          |
| `doctor [--fix]`    | Readiness report; exits 1 on any FAIL ([checks](host-setup.md#asbx-doctor)). `--fix` rewrites the ACL and adds the Include line. |
| `build-base`        | Bakes the base image and repoints the alias ([details](host-setup.md#asbx-build-base)).                      |

## Sandbox lifecycle

### `up <group> [--no-auth] [--no-provision]`

Idempotent. Steps, in order:

1. Refreshes the shared ACL's rules, and only warns if this fails.
2. Creates the sandbox if it doesn't exist, starts it, and waits for the
   Incus agent and cloud-init. `resources` take effect only at creation;
   changing them later needs `rebuild`.
3. Attaches the egress ACL to the NIC. An existing sandbox without the ACL
   gets it now. If attaching fails, `up` fails. A legacy `guest-nft` sandbox
   is refused.
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

Thin wrappers over the matching `incus` command.

### `rebuild <group> [--force] [--yes]`

Runs `rm` then `up` with the current manifest. You confirm by typing the
group name; `--yes` skips the prompt. First it classifies every entry under
`/workspace` and refuses if any repo is dirty, has no remote, or has
unpushed commits. `--force` overrides that refusal. It always refuses if
`/workspace` can't be listed (for example because the sandbox is stopped),
rather than treating that as "no repos".

### `rm <group> [--yes]`

Deletes the sandbox and its generated SSH config, and removes the
`<instance>.incus` entry from `ssh.known_hosts`.

## Snapshots

### `snapshot <group> [label]`

The default label is `pre-<UTC timestamp>`. If the storage pool can't
snapshot the VM, `snapshot` falls back to a full `incus copy` named
`<instance>-snap-<label>`. On a `dir` pool it also prints a notice that
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
| `shell <group> [--root]` | `incus exec` shell as the guest user, or as root with `--root`                              |
| `list`                   | For each manifest: instance, group, state, egress mode, and a note if the manifest changed  |
