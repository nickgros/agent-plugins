# Troubleshooting

Start with `asbx doctor`.

## Guest has no network

`doctor` fails `guest has IPv4 default route` or `guest IPv4 internet
egress`. The usual cause is a host firewall with a default-deny policy
dropping bridge traffic, which Incus never asks it to allow. See
[host setup: host firewall](host-setup.md#host-firewall).

## `<instance>.incus` doesn't resolve

The host's resolver isn't sending `.incus` queries to the bridge's DNS. See
[host setup: name resolution](host-setup.md#name-resolution).

## `up` fails with "could not attach network ACL"

The Incus server can't set `security.acls` on a bridged NIC; `doctor`
reports the version. Upgrade to Incus 7.0 LTS (the minimum is 6.0.4 or
6.10). There is no weaker fallback
([ADR 0003](adr/0003-per-nic-egress-acl-fail-closed.md)).

## `up` refuses a sandbox built with the in-guest firewall fallback

Its `egress_mode` is `guest-nft`, and the agent can turn that firewall off
with root. Save your work and run `asbx rebuild <group>`.

## `up` refuses: firewall unit inactive or out of date

Once the ingress firewall is opted in, `up` checks that
`asbx-firewall.service` is active and that `/etc/asbx/firewall.nft` matches
what the current config renders. It fails after you configure an ingress
interface without running `asbx ingress setup`, or after you change
`network.name` or `network.ingress`. Run `asbx ingress setup` (or
`asbx doctor --fix`). To stop using the firewall instead, remove it
([host setup](host-setup.md#ingress-firewall)) and clear `network.ingress`.

## Ineligible ingress interface

`ingress setup` and `up` refuse, and `doctor` warns, naming the interface
and the reason. Only kernel WireGuard and `tun` devices that exist are
eligible. Bridges, tap devices, the Incus bridge, the default-route
interface, ethernet and wlan are not. Remove the entry from
`network.ingress.interfaces`. The managed interface may be missing until
`asbx ingress setup` creates it.

## `rebuild refused, unsafe repos`

Every entry under `/workspace` that is dirty, has no remote, or has commits
on no remote blocks `rebuild`. Push or stash the work, or copy it out with
`incus file pull -r <instance>/workspace/<dir> <dest>`, then use `--force`.

## Config error: unknown key

`config.yaml` contains a key this version of `asbx` doesn't know. The
removed `tailscale:` block is an example. Delete it.

## Image size exceeds volume size

The error is `Source image size (...) exceeds specified volume size (...)`.
The base image's minimum disk size is fixed at `build-base` time: the build
VM's disk is `defaults.resources.disk`, and `incus publish` bakes that size
into the image as a floor. A manifest whose `resources.disk` is smaller
fails when its sandbox is created. To fix it, lower
`defaults.resources.disk` to at most your smallest manifest's
`resources.disk`, then re-run `asbx build-base`.

## Snapshots are slow and large

The storage pool driver is `dir`, which has no copy-on-write snapshots for
VMs. Each snapshot copies the entire sparse root disk (up to
`resources.disk`). If a live snapshot attempt fails, `snapshot` stops the
sandbox, snapshots it, and restarts it. Move the pool to `zfs` or `btrfs` if
this becomes a bottleneck.

## Stale host key after `rebuild`

On the Incus host this is handled. `rm` removes the `<instance>.incus`
known-hosts entry, and `up` removes it again before writing the SSH config.
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
path unit installed the sandboxes pick the login up within a few seconds;
otherwise run `asbx aws-refresh <group>`. `aws sts get-caller-identity
--profile <p>` on the host confirms the session.

`asbx doctor` warns `asbx-aws-refresh.path active` on an install made before
the path unit existed. Re-run `asbx aws-refresh --install-timer`.

`yields long-term credentials` means the profile has static access keys. Only
temporary credentials are pushed to sandboxes; use an SSO or role profile.

If the `asbx-aws-refresh` service fails with `ModuleNotFoundError: yaml`, the
Python that ran `--install-timer` lacks PyYAML. Re-run `asbx aws-refresh
--install-timer` with an interpreter that has it.

A config written for the removed `aws-sso` provider fails with `unknown key
'aws.profile'`. Replace the `aws:` block with `aws: {profiles: [<profile>]}`
and `aws-sso` in `defaults.auth` with `aws-creds`.
