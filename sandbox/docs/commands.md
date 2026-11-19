# Commands

`<group>` is a manifest name: `~/.config/agent-sandbox/projects/<group>.yaml`.
Behaviour is the same on both backends unless a line says Incus or Tart
([ADR 0006](adr/0006-one-backend-per-host-os.md)).

## Host

| Command             | Behaviour                                                                                                    |
| ------------------- | ------------------------------------------------------------------------------------------------------------ |
| `install [--force]` | Creates the `~/.local/bin/asbx` symlink. `--force` replaces an existing file there.                          |
| `init`              | One-time setup ([host setup](host-setup.md#asbx-init)). Never overwrites an existing `config.yaml`.          |
| `doctor [--fix]`    | Readiness report; exits 1 on any FAIL ([checks](host-setup.md#asbx-doctor)). `--fix` adds the Include line; on Incus it also rewrites the ACL and, once the ingress firewall is opted in, reinstalls it. |
| `ingress setup [--create \| --adopt [IFACE]] [--print] [--yes]` | Incus only. Installs the firewall and ingress config through sudo ([details](host-setup.md#ingress-firewall)). `--create` enables managed WireGuard; `--adopt` adds an existing interface, or lists eligible ones and asks. `--print` prints the files and commands instead; `--yes` skips confirmation. |
| `build-base`        | Bakes the base image ([details](host-setup.md#asbx-build-base)).                                             |
| `aws-refresh [group] [--install-timer]` | Pushes fresh AWS credentials into running sandboxes. `--install-timer` installs the job that re-pushes every 10 minutes and shortly after `~/.aws/sso/cache` changes: systemd user units on Linux, the LaunchAgent `~/Library/LaunchAgents/com.asbx.aws-refresh.plist` on macOS ([manifests](manifest.md#auth-providers)). |
| `settings-sync [group]` | Pushes host harness settings into running sandboxes.                                                     |

## Sandbox lifecycle

### `up <group> [--no-auth] [--no-provision]`

Idempotent. Steps, in order:

1. Host preflight. Incus: refuses if any ingress interface is ineligible.
   Once the ingress firewall is opted in (either of its installed files
   exists, or any ingress interface is configured), it also refuses unless
   `asbx-firewall.service` is active and `/etc/asbx/firewall.nft` matches
   the current config; run `asbx ingress setup` to fix either
   ([troubleshooting](troubleshooting.md#up-refuses-firewall-unit-inactive-or-out-of-date-incus)).
   Then it refreshes the shared ACL's rules, and only warns if this fails.
   Tart: refuses unless `tart` is on PATH, macOS is 26 or later, and
   Softnet is root-owned and setuid ([host setup](host-setup.md#softnet)).
2. Creates the sandbox if it doesn't exist, with its egress policy bound
   before the first boot. Incus: the egress ACL is attached to the stopped
   instance, and an existing sandbox without it gets it now; if attaching
   fails, `up` fails. A legacy `guest-nft` sandbox gets the ACL too, with a
   warning to rebuild it. Tart: the
   Softnet rules are rendered into the VM's launchd job on every start.
   `resources` take effect only at creation; changing them later needs
   `rebuild`.
3. Syncs mounts. A mount newly added to the manifest restarts a running
   sandbox. Incus: a mount Incus refuses is an error. Tart: a share that
   doesn't mount on a fresh boot is an error.
4. Starts the sandbox and waits for the guest agent and cloud-init. Incus:
   then pins the NIC's `ipv4.address` and turns on
   `security.ipv4_filtering` and `security.mac_filtering`; if Incus
   refuses, `up` fails. Tart: refuses a VM that is running outside asbx's
   launchd job, since it may have no egress rules.
5. Sets the hostname to the instance name and rewrites `authorized_keys`
   (the asbx key plus `ssh.extra_pubkeys`).
6. Writes the SSH config, the env files, the git identity and the harness
   settings.
7. Starts `asbx-paseo.service` (the Paseo daemon). A running daemon is left
   alone, so env changes reach it only after a reboot or
   `sudo systemctl restart asbx-paseo` in the guest. An image built before
   the unit existed gets a warning, not a failure.
8. Runs auth, unless `--no-auth`.
9. Runs provision, unless `--no-provision`.

### `auth <group>` / `provision <group>`

These re-run one phase. `auth` skips the providers that are already
satisfied. `provision` does the following:

1. Creates `/workspace/worktrees/<dir>` for each repo.
2. For each repo, in order: clones it if missing, reconciles its remotes,
   and runs its `setup` (on the first clone only).
3. Writes the workspace guide, `/workspace/AGENTS.md`
   ([manifests: workspace guide](manifest.md#workspace-guide)).
4. Runs `docker compose up -d`.
5. Runs the setup hook.

### `start` / `stop` / `restart <group>`

Incus: wrappers over the matching `incus` command. Tart: `start` loads the
VM's launchd job; `stop` asks the guest to power off, runs `tart stop` only
if it hasn't within a minute, then unloads the job. `start` and `restart` then
wait, for up to two minutes, until the sandbox's port 22 accepts a
connection, so an `asbx ssh` straight after doesn't hit "No route to host".
If it never does, they warn and suggest `asbx doctor`.

### `rebuild <group> [--force] [--yes]`

Runs `rm` then `up` with the current manifest. You confirm by typing the
group name; `--yes` skips the prompt. It runs the host preflight before
deleting anything. First it checks for work the rebuild would lose, and
refuses if it finds any:

- **Repos:** every repo found within three levels of `/workspace`, plus
  every worktree git has registered for one, wherever it lives. Each one
  that is dirty or has no remote blocks. Unpushed commits block once per
  repository, reported under its main checkout. A repo whose worktrees
  can't be listed blocks too.
- **Files outside repos:** any file under `/workspace` that is in none of
  those repos blocks, with one example named per top-level entry. Empty
  directories and the generated `AGENTS.md` don't count, since provision
  recreates them. If the guest can't run this check fully (an unreadable
  directory, say), rebuild refuses.

`--force` overrides the refusal. It always refuses if `/workspace` can't be
listed (for example because the sandbox is stopped), rather than treating
that as "no repos". Incus: the new sandbox keeps the old one's pinned IPv4
address, set before its first start.

### `rm <group> [--yes]`

Deletes the sandbox and its generated SSH config, and removes its entry
from `ssh.known_hosts` (`<instance>.incus` on Incus, `<instance>` on Tart).
Tart: also deletes its snapshot VMs and its launchd plist, log and metadata.

## Snapshots

### `snapshot <group> [label]`

The default label is `pre-<UTC timestamp>`.

Incus: if the storage pool can't snapshot the VM, `snapshot` falls back to a
full `incus copy` named `<instance>-snap-<label>`. The copy doesn't keep the
sandbox's pinned IPv4, because Incus won't start two NICs with the same
static address; `restore` puts it back. On a `dir` pool it also prints a
notice that snapshots are full root-disk copies.

Tart: a snapshot is a `tart clone` named `<instance>-snap-<label>`, with a
copy of the sandbox's metadata. A running sandbox is stopped for the clone
and started again as `asbx start` would start it, with its manifest's
mounts and the current egress rules.

### `restore <group> <label> [--yes]`

Restores the snapshot `label` (on Incus, or the `<instance>-snap-<label>`
copy if that is what exists). It errors if the label matches nothing,
before changing anything. Tart: clones the snapshot VM to
`<instance>-restoring` first, so a failed clone leaves the sandbox as it was.
Then it forces the sandbox off, deletes it and renames the clone into its
place; the snapshot is kept. Afterwards it starts the sandbox and rewrites
the SSH config.

## Access

| Command                  | Behaviour                                                                                   |
| ------------------------ | ------------------------------------------------------------------------------------------- |
| `ssh <group> [cmd...]`   | `ssh -F <generated config> <instance> [cmd]`                                                |
| `ssh-config <group> (--jump USER@HOST \| --direct) [--identity PATH]` | Print the `~/.ssh/config` stanza for another machine. `--jump` goes through this host: a `ProxyJump` on Incus; on Tart, a `ProxyCommand` that runs this Mac's `asbx ssh-proxy` command over ssh ([connecting](connecting.md#tart-from-another-machine)). `--direct` (Incus only) uses the pinned IPv4 address with no `ProxyJump` ([connecting](connecting.md#direct-access-over-an-ingress-interface)); it errors on Tart, which pins no addresses. Changes nothing |
| `ssh-proxy <instance>`   | Looks up the sandbox's current address from host-side data and relays stdin/stdout to its port 22. The `ProxyCommand` in Tart SSH configs. Errors if the sandbox has no address (not running). |
| `client-config NAME`     | Incus only. Print the WireGuard config for client `NAME` under `network.ingress.wireguard.clients`. Needs `endpoint` set. Changes nothing |
| `shell <group> [--root]` | `incus exec` / `tart exec` login shell (`shell` config key) as the guest user, or bash as root with `--root` |
| `list`                   | For each manifest: instance, group, state, egress mode, and a note if the manifest changed  |
