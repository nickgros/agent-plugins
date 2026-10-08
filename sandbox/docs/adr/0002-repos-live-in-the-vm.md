# Repos live inside the VM; host mounts are read-only

Repos are cloned into the VM's `/workspace` rather than shared from host
checkouts, and the only host mounts are read-only virtiofs mounts. A writable
mount would let the agent change files the host later acts on, such as
`.git/hooks`, build scripts or editor config, which turns a guest compromise
into a host one.

## Consequences

- Work in progress exists only on the VM's disk until it is pushed. `rebuild`
  refuses to delete a sandbox while any repo is dirty, has no remote, or has
  unpushed commits, and it refuses when it can't list `/workspace` at all.
- IDEs edit over SSH (Remote-SSH, Gateway) rather than on host files.
- A mount source must be world-readable, because guest uids don't map to
  host uids ([security model](../security.md#host-files)).
