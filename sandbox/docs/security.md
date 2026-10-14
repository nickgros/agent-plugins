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

Known gap: IPv6 blocking covers only ULA and link-local. If your LAN hands
out global IPv6 prefixes, a sandbox with IPv6 can reach LAN hosts on them.
Add your LAN's global prefix to `network.block_cidrs`, or set
`ipv6.address=none` on the bridge. Blocking all of `2000::/3` would also cut
off IPv6 internet access.

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
- `aws-creds` pushes short-lived credentials for the profiles in
  `aws.profiles` and the manifest's `aws_profiles`, as
  `~/.asbx/aws/<profile>.json` read through `credential_process`. The guest
  holds no SSO token, no refresh token and no other profile, and it has no
  channel back to the host. A leaked credential works until it expires
  (about an hour); the host timer replaces it every 10 minutes.
- Harness settings sync (`harness_settings`, `asbx settings-sync`) is host to
  guest only. The host is authoritative, so guest edits to synced files are
  overwritten. Files that carry credentials are left out unless listed under
  `harness_settings.credentials` or the manifest's `harness_credentials`;
  runtime state (`agent.db`, `sessions/`, ...) is never synced. A symlink in
  a synced directory is followed only when its target stays inside that
  directory; any other is skipped with a warning. The default `omp` item
  includes `config.yml` and `models.yml` at mode 0644 with no opt-in, so keep
  literal API keys out of them (use environment variables, or the `omp-env`
  credential item).
- Everything in `harness_env` and `env` is exported to every guest shell
  through `/etc/profile.d/asbx-env.sh`.

## Network ingress

Inbound is default-deny, enforced on the host. The asbx-owned nftables table
`inet asbx` drops every new connection forwarded into the bridge unless it
arrives over IPv4 on an ingress interface. This holds with no ingress
interfaces configured. Without it, Incus's own forward chain accepts
anything routed into the bridge. A drop in any nftables table wins over
Incus's accept. `up` refuses to run unless `asbx-firewall.service` is active
and `/etc/asbx/firewall.nft` matches the current config
([host setup](host-setup.md#ingress-firewall)).

Each sandbox NIC has `security.ipv4_filtering`, `security.mac_filtering`
and a pinned `ipv4.address`. The guest can't take another address, so the
IP a client dials is the sandbox it reaches. Like the ACL, this fails
closed: `up` fails if Incus refuses. The pin comes from host data only: an
existing `ipv4.address`, the previous instance's address on `rebuild`, or
the Incus DHCP lease matched by the NIC's MAC.

Ingress interfaces are opt-in. They are the interfaces in
`network.ingress.interfaces` (adopted, run by you, such as `tailscale0`)
plus the asbx-managed WireGuard interface when
`network.ingress.wireguard.enabled` is set. Only kernel WireGuard and `tun`
devices are eligible. Bridges, tap devices, the Incus bridge, the
default-route interface, ethernet, wlan and missing interfaces are refused.
Ingress clients reach any port on any sandbox. IPv6 from ingress interfaces
is dropped.

| Interface | asbx governs                                                                                            |
| --------- | ------------------------------------------------------------------------------------------------------- |
| Managed   | Forwarding into the bridge. Also drops new connections to the host itself and forwarding anywhere else. |
| Adopted   | Forwarding into the bridge only. Host input and other routing stay yours.                               |

The generated SSH configs, client configs and pinned addresses are built
only from the instance name, Incus host data and `config.yaml`. Nothing the
guest reports ends up in them. By default, access from other machines goes
through the host as a jump host
([ADR 0004](adr/0004-host-is-the-jump-host.md),
[ADR 0005](adr/0005-host-enforced-ingress.md),
[Connecting](connecting.md)).

Known gap: a guest can always make itself reachable through an outbound
tunnel, for example an iroh relay. The egress ACL filters by IP, so it can't
prevent that. Treat anything a sandbox serves as reachable by whoever
controls the guest.
