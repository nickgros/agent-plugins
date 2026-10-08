# Sandboxes are Incus VMs, not containers

The threat model has two parts. One is an over-eager agent holding
production credentials, or taking down the host. The other is prompt
injection steering the agent. The agent gets root and Docker inside the
sandbox, so the boundary has to hold against a hostile root user. We chose
Incus VMs: each sandbox gets its own kernel and block device, and guest
privileges are deliberately not restricted
([security model](../security.md)).

## Considered options

- **Incus system containers.** Lighter and faster to snapshot, but they
  share the host kernel. Running Docker inside a container needs nesting
  settings that widen the attack surface further.
- **Restricting the agent inside the guest** (no sudo, an in-guest
  firewall). Rejected because it gets in the agent's way, and a root
  compromise undoes it anyway.

## Consequences

- The host needs `/dev/kvm`, and every sandbox pays a full VM's memory and
  boot time.
- On a `dir` storage pool, VM snapshots are full disk copies.
- If containers are ever wanted for low-risk groups, an `instance_type`
  setting is the seam to add. Nothing assumes it today.
