# Egress is a per-NIC Incus ACL that fails closed

Guests must not reach the host's LAN, tailnet or other sandboxes. Egress is
filtered by one shared Incus network ACL attached to each sandbox's NIC
(`security.acls`). If Incus can't attach it, `up` fails. There is no fallback,
because the guest is root and any control inside it can be removed by the
agent.

## Considered options

- **In-guest nftables.** This was the old fallback, used when the NIC ACL
  couldn't be attached. Removed, because the agent can flush it. Sandboxes
  marked `user.asbx.egress_mode=guest-nft` are refused by `up` and must be
  rebuilt.
- **An ACL on the bridge network as a whole.** Rejected because it can't
  isolate sandboxes from each other on the same bridge.

## Consequences

- Requires Incus that accepts `security.acls` on bridged NICs: 6.0.4+ on the
  6.0 LTS, or 6.10+. 7.0 LTS is recommended. Incus 6.0.0, the Ubuntu
  package, lacks it. `doctor` checks the version.
- `100.64.0.0/10` stays in the drop list even though guests don't run
  Tailscale. A host on a tailnet routes that range out of `tailscale0`, and
  bridge masquerade would present the guest as the host's tailnet identity.
  This was confirmed empirically from an unfiltered VM.
- Each successful attach records `user.asbx.egress_mode=acl` on the
  instance. `up` re-applies the ACL to any sandbox that lacks that mark.
