# Troubleshooting

Start with `asbx doctor`.

## Guest has no network

`doctor` fails `guest has IPv4 default route` or `guest IPv4 internet
egress`. On Incus the usual cause is a host firewall with a default-deny
policy dropping bridge traffic, which Incus never asks it to allow. See
[host setup: host firewall](host-setup.md#host-firewall). On Tart, check
the VM's log, `~/.config/agent-sandbox/logs/<instance>.log`, for Softnet
errors.

## `<instance>.incus` doesn't resolve (Incus)

The host's resolver isn't sending `.incus` queries to the bridge's DNS. See
[host setup: name resolution](host-setup.md#name-resolution).

## `up` fails with "could not attach network ACL" (Incus)

The Incus server can't set `security.acls` on a bridged NIC; `doctor`
reports the version. Upgrade to Incus 7.0 LTS (the minimum is 6.0.4 or
6.10). There is no weaker fallback
([ADR 0003](adr/0003-per-nic-egress-acl-fail-closed.md)).

## `up` warns about a sandbox built with the in-guest firewall fallback

Its `egress_mode` was `guest-nft`. asbx attaches the NIC ACL before the
sandbox boots, so it runs filtered, but the old in-guest rules are still
there. Save your work and run `asbx rebuild <group>` to drop them.

## `up` refuses: firewall unit inactive or out of date (Incus)

Once the ingress firewall is opted in, `up` checks that
`asbx-firewall.service` is active and that `/etc/asbx/firewall.nft` matches
what the current config renders. It fails after you configure an ingress
interface without running `asbx ingress setup`, or after you change
`network.name` or `network.ingress`. Run `asbx ingress setup` (or
`asbx doctor --fix`). To stop using the firewall instead, remove it
([host setup](host-setup.md#ingress-firewall)) and clear `network.ingress`.

## Ineligible ingress interface (Incus)

`ingress setup` and `up` refuse, and `doctor` warns, naming the interface
and the reason. Only kernel WireGuard and `tun` devices that exist are
eligible. Bridges, tap devices, the Incus bridge, the default-route
interface, ethernet and wlan are not. Remove the entry from
`network.ingress.interfaces`. The managed interface may be missing until
`asbx ingress setup` creates it.

## `rebuild refused, unsafe repos`

Every repo under `/workspace` or worktree registered for one that is dirty,
has no remote, or has commits on no remote blocks `rebuild`, as does any
`/workspace` entry holding files outside a repo. Push or stash the work, or
copy it out (`scp -r <instance>:/workspace/<dir> <dest>`, or on Incus
`incus file pull -r <instance>/workspace/<dir> <dest>`), then use `--force`.

## Softnet is not root-owned and setuid (Tart)

`up`, `build-base`, `start` or `doctor` reports a problem with
`<tart.softnet_dir>/softnet` and prints the two `sudo install` commands
that fix it. Run them; after a softnet upgrade, re-run the second one
([host setup: Softnet](host-setup.md#softnet)).

## `<instance> is running outside asbx` (Tart)

The VM was started some other way (for example `tart run` by hand), so it
may have no egress rules. Run `tart stop <instance>`, then `asbx up` or
`asbx start`.

## Guest agent did not come up in time (Tart)

The error names the VM's log, `~/.config/agent-sandbox/logs/<instance>.log`,
which holds `tart run`'s output.

## `tart ip` has no address / `ssh-proxy` fails (Tart)

`asbx ssh-proxy` errors `has no address yet` while the VM is stopped or still
booting. Check `asbx list`, then `asbx start <group>`.

## Config error: unknown key

`config.yaml` contains a key this version of `asbx` doesn't know. The
removed `tailscale:` block is an example. Delete it.

## Image size exceeds volume size (Incus)

The error is `Source image size (...) exceeds specified volume size (...)`.
The base image's minimum disk size is fixed at `build-base` time: the build
VM's disk is `defaults.resources.disk`, and `incus publish` bakes that size
into the image as a floor. A manifest whose `resources.disk` is smaller
fails when its sandbox is created. To fix it, lower
`defaults.resources.disk` to at most your smallest manifest's
`resources.disk`, then re-run `asbx build-base`.

## Snapshots are slow and large (Incus)

The storage pool driver is `dir`, which has no copy-on-write snapshots for
VMs. Each snapshot copies the entire sparse root disk (up to
`resources.disk`). If a live snapshot attempt fails, `snapshot` stops the
sandbox, snapshots it, and restarts it. Move the pool to `zfs` or `btrfs` if
this becomes a bottleneck.

## Stale host key after `rebuild`

On the host this is handled. `rm` removes the instance's known-hosts entry
(`<instance>.incus` on Incus, `<instance>` on Tart), and `up` removes it
again before writing the SSH config.
Other machines keep their own host-key state, which is why the
[remote stanza](connecting.md#from-another-machine) turns host-key checking
off.

## GitHub or AWS token expired

Run `asbx auth <group>`. Providers that are still authenticated are skipped.

## `github-gh` fails with HTTP 404 on `ssh_signing_keys`

The gh token lacks `admin:ssh_signing_key`. Run `asbx auth <group>`, which
widens the token with `gh auth refresh`.

## `aws-creds`: could not export credentials on the host

`asbx auth`, `asbx up` or `asbx aws-refresh` reports `aws profile <p>: could
not export credentials on the host`. The host's SSO session for that profile
expired or was never started. Run `aws sso login --profile <p>` on the host
(for a role-assumption profile, log in to its `source_profile`). With the
refresh job installed (`asbx aws-refresh --install-timer`) the sandboxes
pick the login up within a few seconds; otherwise run
`asbx aws-refresh <group>`. `aws sts get-caller-identity --profile <p>` on
the host confirms the session.

On Linux, `asbx doctor` warns `asbx-aws-refresh.path active` on an install
made before the path unit existed. On macOS it warns
`LaunchAgent com.asbx.aws-refresh loaded` when the job isn't loaded.
Re-run `asbx aws-refresh --install-timer` for either.

`yields long-term credentials` means the profile has static access keys. Only
temporary credentials are pushed to sandboxes; use an SSO or role profile.

If the refresh job fails with `ModuleNotFoundError: yaml`, the Python that
ran `--install-timer` lacks PyYAML. Re-run `asbx aws-refresh
--install-timer` with an interpreter that has it. On Linux the failure is in
`journalctl --user -u asbx-aws-refresh.service`; the macOS LaunchAgent
discards its output, so run `asbx aws-refresh` by hand to see it.

A config written for the removed `aws-sso` provider fails with `unknown key
'aws.profile'`. Replace the `aws:` block with `aws: {profiles: [<profile>]}`
and `aws-sso` in `defaults.auth` with `aws-creds`.
