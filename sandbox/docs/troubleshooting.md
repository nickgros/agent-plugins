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

## `aws sso login`: missing SSO configuration values

The error says `Missing the following required SSO configuration values:
sso_start_url, sso_region`. `aws sso login --profile <aws.profile>` needs a
profile that has an `sso_session` itself.

If `aws.profile` names a role-assumption profile (`role_arn` plus
`source_profile`, as with cross-account Bedrock), set `aws.profile` to the
`source_profile` name instead. Once that profile's SSO token is cached, the
role profile resolves automatically.

`aws.profile` is used only by the `aws-sso` provider. Keep
`harness_env.AWS_PROFILE` pointed at the role profile.
