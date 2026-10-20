# The host enforces every inbound path to a sandbox

ADR 0004 made SSH through the host the only way in. That rules out UDP
protocols such as QUIC and mosh, and any direct client connection. Worse, it
was never enforced: Incus's `fwd.incusbr0` accepts anything routed into the
bridge, and the host has `ip_forward=1`. Now an asbx-owned nftables table,
`inet asbx`, drops every new connection forwarded into the bridge unless it
arrives over IPv4 on an ingress interface. Ingress interfaces are the
operator's own (`network.ingress.interfaces`, for example `tailscale0`) plus,
when enabled, an asbx-managed WireGuard interface (default `asbx0`). Only
eligible interfaces count: kernel WireGuard or `tun` devices. Each sandbox
NIC gets a pinned IPv4 address with IPv4 and MAC filtering, so the address a
client dials is the sandbox it reaches. The host authenticates every inbound
packet; nothing in the guest is an access control. This supersedes ADR
0004's "SSH is the only entry point". SSH through the host stays the default.

## Considered options

- **Guest Tailscale.** Rejected again for ADR 0004's reasons: an auth key in
  every guest, a tag policy, and guest-reported data reaching client config.
- **Guest-app auth over a port publish**, for example an app's own keys
  behind an Incus proxy device. Rejected because the guest is root and
  controls that auth.
- **A source-IP allowlist unlocked over SSH.** Rejected because peers behind
  the same NAT share an IP.
- **asbx-managed WireGuard only, or bring-your-own interface only.** Chose
  both. Enforcement is generic: the firewall keys on interface names, so a
  managed `asbx0` and an adopted `tailscale0` are handled by the same rules.

## Consequences

- The default-deny is opt-in. It applies once `asbx ingress setup` installs
  it, with or without ingress interfaces, and it is required whenever any
  ingress interface is configured. A host that never opts in keeps the
  forwarding gap: Incus accepts anything routed into the bridge.
- The ruleset lives in `/etc/asbx/firewall.nft` and is loaded at boot by
  `asbx-firewall.service`, before Incus starts. Unprivileged asbx can't list
  nftables, so `up` checks that the unit is active and the installed file
  matches the current config. A manual `nft flush ruleset` goes unnoticed.
- On the managed interface asbx also drops traffic to the host itself and
  forwarding anywhere but the bridge. On adopted interfaces asbx governs only
  forwarding into the bridge.
- IPv6 from ingress interfaces is dropped, so there is no dependency on
  `br_netfilter`.
- Every client reaches every sandbox, on any port. There is one global
  client list. Each client keeps its own WireGuard private key.
- asbx does not make the host reachable. The managed WireGuard endpoint is
  config, never autodetected.
- Sandbox addresses come from host data only: the NIC's `ipv4.address`, the
  previous instance's pin on `rebuild`, or the Incus DHCP lease matched by
  MAC. Nothing the guest reports reaches host or client config.
- A guest can still make itself reachable through an outbound tunnel, such
  as an iroh relay. The IP-based egress ACL can't prevent that.
