# Security model

The VM is the boundary: a separate kernel, its own block device, and a
filtered NIC. Nothing inside the guest is a security control. Why VMs:
[ADR 0001](adr/0001-vms-not-containers.md).

## Network egress

An Incus network ACL (`network.acl`, `asbx-egress` by default) is attached to
each sandbox's NIC. It drops outbound traffic to every range in
`network.block_cidrs`:

| Range                          | Why                                                                                              |
| ------------------------------ | ------------------------------------------------------------------------------------------------ |
| RFC1918, `169.254.0.0/16`      | Host LAN and IPv4 link-local                                                                     |
| `100.64.0.0/10`                | Tailscale CGNAT. A host on a tailnet would otherwise route guest traffic there as its own identity |
| `fc00::/7`, `fe80::/10`        | IPv6 ULA and link-local                                                                          |

The only exception is the bridge gateway's DNS ports. Every other port on the
gateway, and every other address on the bridge subnet, is dropped. That
includes other sandboxes. IPv6 gets no exception, because DNS is served over
IPv4 only. All other internet egress is allowed.

There is no fallback. `up` fails if Incus can't attach the ACL. A sandbox
built under the removed in-guest nftables fallback
(`egress_mode=guest-nft`) is refused by `up` and has to be rebuilt.
Rationale: [ADR 0003](adr/0003-per-nic-egress-acl-fail-closed.md).

The ACL's rules are computed from the current bridge address and
`block_cidrs`. `asbx doctor` reports drift; `asbx doctor --fix` rewrites the
ACL.

## Host files

The guest can read only the host paths listed under `mounts:`. These are
virtiofs mounts and are always read-only. There is no other channel from
guest to host filesystem. Repos are cloned inside the VM rather than mounted
([ADR 0002](adr/0002-repos-live-in-the-vm.md)).

A mount source must be world-readable and world-traversable
(`chmod o+rX`). `up` and `doctor` refuse one that isn't. The guest's uid
belongs to the VM's own namespace and has nothing to do with host uids, so
owner and group bits on the host give the guest nothing. Ancestor
directories are not checked, because the Incus daemon serves virtiofs as host
root.

## Inside the guest

`guest_user` (`agent` by default) has `sudo: ALL=(ALL) NOPASSWD:ALL`. This is
deliberate. To keep the agent away from something, don't mount it and don't
give the guest network access to it.

The agent can use any credential placed in the guest:

- `github-gh` leaves a `gh` token in the guest. It has gh's default scopes
  (including `repo`) plus `admin:ssh_signing_key`, and an SSH signing key
  registered to your GitHub account.
- `aws-sso` copies `aws.config_file` in and caches an SSO token. Together
  with `harness_env` (for example `AWS_PROFILE`), it gives the guest that
  profile's AWS access.
- Everything in `harness_env` and `env` is exported to every guest shell
  through `/etc/profile.d/asbx-env.sh`.

## SSH

Sandboxes are reachable only from the Incus host, over the bridge. The
generated host SSH config is built only from the instance name and
`config.yaml`. Nothing the guest reports ends up in it. Access from other
machines goes through the host as a jump host
([ADR 0004](adr/0004-host-is-the-jump-host.md),
[Connecting](connecting.md)).
