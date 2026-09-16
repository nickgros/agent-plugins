# asbx: agent sandbox VMs on Incus

`asbx` is a Python 3 CLI whose entry point is `sandbox/asbx`, with its
implementation in `sandbox/asbxlib/`. It spins up isolated
Incus **VM** development environments safe to hand to an LLM agent. Each
sandbox is one long-lived VM per project group, holding one or more git repos
cloned inside the VM, reachable over Tailscale SSH from VSCode/JetBrains, with
per-project services (Docker Compose) and a provision hook, and with harness
CLIs (`claude`, `codex`, `gemini`, `opencode`, `omp`) pre-installed.

## The security boundary

The hard boundary is the VM, not guest privilege:

- The guest **cannot reach the host's LAN**. A network ACL (or, as a fallback,
  in-guest nftables) drops outbound traffic to RFC1918 and IPv4 link-local
  ranges, carving out only the bridge gateway's DNS ports: every other port on
  the gateway, and every other address in the bridge subnet, is dropped.
  IPv6 gets no such carve-out — DNS is served over IPv4 only — so the guest's
  IPv6 ULA (`fc00::/7`) and link-local (`fe80::/10`) ranges are dropped whole.
  General internet egress is allowed.
- The guest **cannot read host files** beyond mounts explicitly declared in
  `mounts:`, and those are always mounted read-only (`mode: ro` is the only
  supported mode today). There is no other path from guest to host
  filesystem; virtiofs mounts are the only channel. A mount source must be
  world-readable and world-traversable (`chmod o+rX`), and both `up` and
  `doctor` refuse one that isn't: the guest's uid belongs to the VM's own
  namespace and is unrelated to any host uid, so owner/group bits on the host
  buy the guest nothing. Ancestor directories are not checked — the Incus
  daemon serves virtiofs as host root.
- The guest user (`guest_user` in `config.yaml`, `agent` by default) has
  **passwordless root inside the VM**
  (`sudo: ALL=(ALL) NOPASSWD:ALL`). That's intentional. Isolation comes from
  the VM boundary (a separate kernel, its own block device, filtered network),
  not from restricting what the agent can do once it's in. Don't treat guest
  root as a security control. If you need to keep the agent from touching
  something, don't mount it, or don't grant it network egress to it. Don't
  rely on guest-side permissions.

## Install

```
sandbox/asbx install [--force]
```

Symlinks the running script to `~/.local/bin/asbx` (`--force` replaces an
existing file/symlink at that path). Warns if `~/.local/bin` is not on
`$PATH`. Requires PyYAML. If it's missing, the script exits immediately with:

```
asbx requires PyYAML: pip install --user pyyaml
```

## One-time setup

```
asbx init
asbx build-base
```

`asbx init`:

- Creates `~/.config/agent-sandbox/` and `~/.config/agent-sandbox/projects/`.
- Writes `~/.config/agent-sandbox/config.yaml` at mode `0600`. If the file
  already exists, `init` just prints its path and exits; it never overwrites.
- Prompts for a **Tailscale reusable, non-ephemeral auth key** tagged
  `tag:sandbox` (blank is accepted; leave it blank and fill in
  `tailscale.auth_key` in the config later, and `up` will warn that the
  instance is reachable only via `asbx shell` until a key is set).
- Generates an SSH keypair at `~/.config/agent-sandbox/id_ed25519` (skipped if
  it already exists). This key is pushed into every sandbox's
  `authorized_keys` at creation time.
- Creates the network ACL (`asbx-egress` by default) on the configured bridge.
- Prepends `Include ~/.ssh/agent-sandbox.d/*.conf` as the first line of
  `~/.ssh/config` (creating that file at mode `0600` if absent). This has to
  come before any `Host` block for the generated per-instance configs to take
  effect.

`config.yaml`'s `guest_user` (default `agent`) is honoured everywhere the guest
user appears — the `/home/<guest_user>` paths provision and the auth providers
write to, and the `User` line of every generated SSH config — so it is safe to
change. Change it before `build-base`: the guest account itself is created when
the base image is baked.

`asbx build-base` bakes the golden image every sandbox is created from. It
launches a throwaway VM (`asbx-base-build`) from `images:debian/13/cloud`,
waits for cloud-init to install Docker, Tailscale, Node.js LTS, mise, the AWS
CLI, and the harness CLIs (`@anthropic-ai/claude-code`, `@openai/codex`,
`@google/gemini-cli`, opencode), copies configured host binaries (e.g. `omp`)
in, cleans the instance, publishes it as `agent-sandbox-base-<YYYYMMDD>`, and
repoints the rolling `agent-sandbox-base` alias to it. Budget several minutes
for this, mostly `apt-get` and `npm install -g`. It refuses to run if
`asbx-base-build` already exists (delete it first). Re-run `build-base`
whenever you want to refresh the installed tool versions in newly-created
sandboxes; it has no effect on already-running instances.

Run `asbx doctor` at any point to check host readiness: `incus`/`tailscale` on
PATH, the qemu driver, `/dev/kvm`, PyYAML, the auth key, SSH key/`Include`
line, the base image alias, the network ACL (and whether its rules match what's
currently computed), the readability of every mount any manifest would attach
(not just `defaults.mounts`), the storage pool driver, and the state of every
manifest's instance. For every `Running` instance, it also probes IPv4
networking from inside the guest (`asbxlib/netcheck.py`): a live default
route, then (only if that holds) a TCP handshake to a public host. Both are
black-box — no assumption about which host firewall tool, if any, is
filtering the bridge — so they catch the same failure whether the cause is a
dead DHCP lease, an Incus ACL misconfiguration, or (the common case) a host
firewall's default-deny policy silently dropping the bridge's DHCP or
forwarded traffic (e.g. `ufw` needs explicit `ufw allow in on <bridge> port
67 proto udp`/`port 53` and `ufw route allow in on <bridge> out on
<uplink>`; `ufw` does not open these for a new bridge on its own). `--fix`
repairs the two host-side items it can: it creates or rewrites the network
ACL from the currently computed drop sets, and prepends the
`~/.ssh/config` Include line (creating that file at mode `0600` if absent).

## Tailscale tailnet policy

Before `asbx up` can bring a sandbox onto the tailnet, the tailnet's policy
must own the `tag:sandbox` tag and allow only inbound SSH to it. Add this to
the tailnet policy file (Tailscale recommends `grants` over legacy `acls` for
new rules; see [migrating ACLs to grants][migrate-acls-grants]). It's close
to what `asbx up` prints to stderr if `tailscale up` fails because the tag
isn't owned:

```jsonc
"tagOwners": { "tag:sandbox": ["autogroup:admin"] },
"grants": [
  // Preserves free SSH between your own devices — Tailscale's SaaS default
  // starter policy ships {"src": ["*"], "dst": ["*"], "ip": ["*"]}, which
  // this replaces. Skip this grant if you'd already scoped that default
  // down to something narrower than "*".
  { "src": ["autogroup:member"], "dst": ["autogroup:member"], "ip": ["*"] },

  { "src": ["autogroup:member"], "dst": ["tag:sandbox"], "ip": ["tcp:22"] }
],
"tests": [
  { "src": "you@example.com", "accept": ["tag:sandbox:22"] },
  { "src": "tag:sandbox", "deny": ["tag:sandbox:22"] }
]
```

**Delete the default `{"src": ["*"], "dst": ["*"], "ip": ["*"]}` starter
grant if it's still present** — don't just append the sandbox rule next to
it. `"*"` matches tagged nodes on both sides, so that rule alone already
lets `tag:sandbox` reach (and be reached by) every other tailnet device;
adding a narrower sandbox-only grant on top of it changes nothing, since
grants are additive with no negation/override. The `autogroup:member`
member-to-member grant above is the replacement for anyone who was only
ever using the wildcard for open SSH between their own manually-added
devices, with no tags or groups otherwise in play.

The `tests` block isn't optional ceremony: Tailscale validates it on every
policy save and rejects the update if a test fails, so it's a real guardrail
against a future edit accidentally widening or dropping this rule. The first
test proves a real member can actually reach a sandbox over SSH; the second
proves a sandbox can't reach another sandbox (or, by extension, anything
else) over Tailscale — swap in a specific other host/tag if you want a
sharper deny check than self-to-self. Replace `you@example.com` with a real
Tailscale-registered identity; `autogroup:member`/`autogroup:admin` aren't
supported as a test `src`.

No rule grants traffic _from_ `tag:sandbox` to any other tailnet node. That's
what stops a sandbox from reaching the host (or any other machine) over
Tailscale. The Incus network ACL only blocks RFC1918/link-local, not
Tailscale's `100.64.0.0/10` CGNAT range, so the tailnet policy is what closes
that gap.

[migrate-acls-grants]: https://tailscale.com/docs/reference/migrate-acls-grants

## Manifest reference

Manifests live at `~/.config/agent-sandbox/projects/<group>.yaml`. Unknown
top-level keys are a hard error naming the key, and so are unknown keys nested
under `resources`, `repos[*]`, `mounts[*]`, and `services` (named as e.g.
`resources.gpu`).

```yaml
name:
  backend # optional; defaults to the file stem.
  # instance name = f"{instance_prefix}{name}"
resources: # optional; merges over config defaults.resources
  cpu: 6 # int
  memory: 12GiB # must match ^\d+(MiB|GiB)$
  disk: 80GiB # must match ^\d+(MiB|GiB)$
repos:
  - url: https://github.com/nickgros/foo.git
    dir: foo # optional; defaults to the repo basename minus .git
    ref: main # optional; passed to `git clone --branch`
    setup:
      "pnpm install" # optional; bash -lc, cwd = /workspace/<dir>, re-run only
      # on that repo's first clone (skipped once the dir exists)
    remotes: # optional; per-repo remotes, keyed by remote name;
      fork: jsmith # a name also set at top-level `remotes` overrides it here
remotes: # optional; merged over config defaults.remotes, applied
  # to every repo, and winning over a repo's own entry of
  # the same remote name
  upstream:
    Sage-Bionetworks # value is either an org/user name — substituted for the
    # owner segment of that repo's own url, keeping host,
    # scheme, and repo name — or a full URL used verbatim
mounts: # optional; merged with config defaults.mounts,
  # keyed by `guest` path. A manifest entry with the
  # same guest path replaces the matching default.
  # `host` and `guest` are both required.
  - { host: ~/src/reference-docs, guest: /home/agent/reference-docs, mode: ro }
    # mode accepts only "ro" today; anything else errors
services:
  compose:
    services/backend.compose.yaml # path relative to the manifest's directory;
    # pushed into the guest and run with
    # `docker compose -f <path> up -d` (idempotent)
env:
  DATABASE_URL:
    "mysql://root@127.0.0.1:3306/app" # exported to repo setup, the compose
    # run, and the setup hook; also merged
    # into /etc/profile.d/asbx-env.sh
auth:
  [github-gh, aws-sso] # optional; replaces (not merges with) config defaults.auth.
  # entries are either a built-in provider name
  # (github-gh, aws-sso) or an inline dict:
  # {command: "codex login", check: "test -f ~/.codex/auth.json"}
setup:
  backend-provision.sh # optional; path relative to the manifest's directory.
  # Pushed to /home/<guest_user>/.asbx/provision.sh and run
  # with cwd /workspace on every `asbx provision`; must be
  # idempotent, since it re-runs on every call.
```

`services.compose` and `setup` must resolve to files that exist under the
manifest's own directory (a path that escapes it via `..` is rejected).
`repos[*].url` is required; duplicate `repos[*].dir` values are rejected. Remotes
are configured on every `up`/`provision` run (idempotent: added if missing, `set-url`d
if the resolved URL drifted, left alone otherwise), on freshly-cloned and pre-existing
repos alike.

The two built-in auth providers do more than log in:

- `github-gh` runs `gh auth login` (scope `admin:public_key`) and
  `gh auth setup-git`, then generates `~/.ssh/id_ed25519` in the guest if it is
  missing, uploads its public half to the authenticated GitHub account as a
  **signing** key titled `asbx <instance>` (an "already in use" response is
  tolerated), and sets `gpg.format ssh`, `user.signingkey`, and
  `commit.gpgsign true`, so guest commits are signed. The key upload is what
  needs the `admin:public_key` scope.
- `aws-sso` copies the host's `aws.config_file` (`~/.aws/config` by default)
  into the guest at `~/.aws/config`, mode `0600`, owned by the guest user, then
  runs `aws sso login --use-device-code --profile <aws.profile>`.

### Worked example

```yaml
name: demo
resources:
  cpu: 6
  memory: 12GiB
repos:
  - url: https://github.com/nickgros/agent-plugins.git
    setup: "git log --oneline -1"
mounts:
  - { host: ~/.agents/skills, guest: /home/agent/.agents/skills, mode: ro }
services:
  compose: services/demo.compose.yaml
env:
  DATABASE_URL: "mysql://root@127.0.0.1:3306/app"
setup: demo-provision.sh
```

`services/demo.compose.yaml` and `demo-provision.sh` live next to
`demo.yaml` in `~/.config/agent-sandbox/projects/`.

## Command reference

| Command                              | Does                                                                                                                                                                                                                                                                                                                                                         | Flags                         |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------- |
| `init`                               | One-time host setup: config file, SSH keypair, network ACL, `~/.ssh/config` Include line.                                                                                                                                                                                                                                                                    |                               |
| `doctor`                             | PASS/FAIL/WARN readiness report; exits 1 on any FAIL. `--fix` creates/rewrites the network ACL and prepends the `~/.ssh/config` Include line.                                                                                                                                                                                                                | `--fix`                       |
| `install`                            | Symlinks this script to `~/.local/bin/asbx`.                                                                                                                                                                                                                                                                                                                 | `--force`                     |
| `build-base`                         | Bakes/refreshes the golden base image and repoints the `agent-sandbox-base` alias.                                                                                                                                                                                                                                                                           |                               |
| `up <group>`                         | Creates (if absent) or starts the sandbox, waits for the agent/cloud-init, syncs mounts, brings up Tailscale, writes SSH config + env files + git identity, then runs auth and provision. Idempotent: a mount added to the manifest is attached to an existing sandbox on the next `up` (which then restarts it), and a mount incus refuses is a hard error. | `--no-auth`, `--no-provision` |
| `auth <group>`                       | Re-runs just the auth phase (skips providers already satisfied).                                                                                                                                                                                                                                                                                             |                               |
| `provision <group>`                  | Re-runs just the provision phase (clone repos, compose up, setup hook).                                                                                                                                                                                                                                                                                      |                               |
| `list`                               | Table of every manifest's instance, group, state, tailnet name, egress mode, and manifest-drift note.                                                                                                                                                                                                                                                        |                               |
| `ssh <group> [cmd...]`               | `ssh -F <generated config> <instance> [cmd]`.                                                                                                                                                                                                                                                                                                                |                               |
| `shell <group>`                      | `incus exec` shell into the instance. Host-side rescue path when SSH/Tailscale is broken.                                                                                                                                                                                                                                                                    | `--root`                      |
| `start` / `stop` / `restart <group>` | Thin wrappers over the matching `incus` lifecycle command.                                                                                                                                                                                                                                                                                                   |                               |
| `snapshot <group> [label]`           | Snapshots the instance (default label `pre-<UTC timestamp>`); falls back to a full `incus copy` if the pool can't snapshot VMs. Prints the full-root-disk notice only when the `default` pool's driver is `dir`.                                                                                                                                             |                               |
| `restore <group> <label>`            | Restores the snapshot named by `label`, or the `<instance>-snap-<label>` copy if that is what exists; errors out when the label names neither, and never deletes the instance before the copy it restores from is known to exist. Restarts and rewrites the SSH config.                                                                                      | `--yes`                       |
| `rebuild <group>`                    | Classifies every repo under `/workspace` (dirty, no-remote, and unpushed are all blocking), then `rm` + `up` with the current manifest. Refuses outright if `/workspace` can't be listed (e.g. the instance is stopped) rather than reading that as "no repos to lose".                                                                                      | `--force`, `--yes`            |
| `rm <group>`                         | Deregisters the tailnet node, deletes the instance, removes its SSH config and known-hosts entry. `--keep-node` skips only the tailnet deregistration; the known-hosts entry is removed either way.                                                                                                                                                          | `--yes`, `--keep-node`        |

## Day to day

VSCode: `asbx up <group>` writes `~/.ssh/agent-sandbox.d/<instance>.conf`
(e.g. `sandbox-demo.conf`), and `asbx init` made `~/.ssh/config` include that
directory. In VSCode, run Remote-SSH: Connect to Host and pick the instance
name (e.g. `sandbox-demo`). It's a normal SSH host alias, no extra
configuration needed. Open `/workspace` once connected.

JetBrains Gateway: same SSH host alias. Add a new SSH connection in Gateway
pointing at `sandbox-demo` (Gateway reads `~/.ssh/config` the same way), and
open `/workspace` as the project root.

Both tools connect over Tailscale SSH using the key generated by `asbx init`.
No per-IDE credentials needed.

## Troubleshooting

- **GitHub/AWS SSO token expired.** Run `asbx auth <group>` again. Each
  provider checks whether it's already authenticated (`gh auth status`,
  `aws sts get-caller-identity`) and skips itself if so, so re-running is safe
  and only re-prompts the providers that actually need it.
- **Stale host key after `rebuild`.** Handled automatically. `rm` (which
  `rebuild` calls internally) removes the tailnet-hostname entry from
  `known_hosts`, and `up` purges it again before writing the new SSH config.
  You should never see `REMOTE HOST IDENTIFICATION HAS CHANGED`.
- **Snapshots are full root-disk copies.** The default Incus storage pool
  driver here is `dir`, which doesn't support fast copy-on-write snapshots for
  VMs. `asbx snapshot` still works, but each snapshot copies the entire
  (sparse, up to `resources.disk`, default 30GiB) root disk, takes real time
  and disk space, and freezes a running instance for the duration of the copy
  (stop/snapshot/restart happens automatically if a live snapshot attempt
  fails). `doctor` prints a WARN for this pool driver. Migrate the pool to
  `zfs`/`btrfs` if snapshotting becomes a bottleneck.
- **`Source image size (...) exceeds specified volume size (...)`.** The
  base image's minimum disk size is fixed at `build-base` time, not `up`
  time: `build-base` sizes its throwaway build VM from
  `defaults.resources.disk` (default `30GiB`), and `incus publish` bakes
  that disk's size into the published image as a floor. Any manifest whose
  `resources.disk` is smaller than whatever `defaults.resources.disk` was
  at the last `build-base` run fails at instance creation with this error —
  it's unrelated to the guest OS choice or to repo size (nothing is cloned
  until provisioning, after the volume already exists). Fix: lower
  `defaults.resources.disk` in `config.yaml` to at or below your smallest
  manifest's `resources.disk`, then re-run `asbx build-base`.
- **`aws sso login` fails with "Missing the following required SSO
  configuration values: sso_start_url, sso_region".** The `aws-sso`
  provider runs `aws sso login --use-device-code --profile <aws.profile>`,
  which requires that profile to carry an `sso_session` directly. If
  `aws.profile` instead points at a role-assumption profile (`role_arn` +
  `source_profile`, e.g. cross-account Bedrock access), the login step has
  to target the _upstream_ SSO-session profile named by that
  `source_profile`, not the role profile itself — the role profile's
  `credential_process` resolves automatically once that upstream profile's
  SSO token is cached. Set `aws.profile` to the `source_profile` name; it's
  used only by this auth provider, not by `harness_env.AWS_PROFILE` (which
  stays pointed at the role profile for actual guest usage).
- **Guest has no network** (`asbx doctor` fails `guest has IPv4 default
  route` or `guest IPv4 internet egress`). Incus manages its own `nft`
  tables for the bridge (masquerade, `dnsmasq` DHCP/DNS) but never touches
  the host's general-purpose firewall, so a host firewall with a
  default-deny policy silently drops bridge traffic Incus never asked it to
  allow. With `ufw`, two gaps are common: its default `after.rules` drop
  inbound DHCP (port 67/udp) before any `allow` rule is even consulted, and
  a `deny (routed)` default policy blackholes everything the bridge tries
  to forward outbound except ICMP and already-established connections.
  Fix (substitute your bridge name and uplink interface, e.g. `incusbr0`/
  `eth0`):
  ```
  sudo ufw allow in on <bridge> to any port 67 proto udp
  sudo ufw allow in on <bridge> to any port 53
  sudo ufw route allow in on <bridge> out on <uplink>
  sudo ufw reload
  ```
  Confirm the uplink interface with `ip route get 1.1.1.1` first — a rule
  scoped to the wrong interface silently does nothing.
