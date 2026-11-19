# One backend per host OS: Incus on Linux, Tart with Softnet on macOS

asbx has to run on Macs, including M1 and M2, which have no nested
virtualization, so Incus can't run inside a Linux VM there. On macOS the
sandboxes are therefore Virtualization.framework VMs managed by
[Tart](https://github.com/openai/tart), with
[Softnet](https://github.com/openai/softnet) as the host-enforced egress
filter. Linux keeps Incus. `backend.py` is the seam: `up`, `lifecycle`,
`doctor` and `build-base` talk to a `SandboxBackend`, and everything that acts
inside a guest talks to a `Guest`. `backend: auto` picks Tart on macOS and
Incus on Linux.

## Considered options

- **Incus inside a Linux VM on the Mac.** Needs nested virtualization (M3
  or later on macOS 15 or later). Rejected because it excludes M1 and M2.
- **Lima (vz).** No host-side egress filter: guest traffic leaves through a
  user-mode network stack whose outbound filtering is an unmerged
  gvisor-tap-vsock PR. It also has no snapshots on vz.
- **Apple `container`.** The host gateway is reachable, pf doesn't filter
  its vmnet traffic, there are no snapshots, and Docker inside the guest
  has been reported broken.
- **One backend everywhere.** Neither Incus nor Tart covers both
  platforms, and Lima on Linux has the same filtering gap as on macOS.

## Consequences

- macOS 26 or later, because Softnet requires it. Any Apple silicon Mac
  works.
- Softnet's rules replace the Incus ACL (ADR 0003 still holds, enforced
  differently). They block new connections from the guest to the Mac except
  DNS, block every `network.block_cidrs` IPv4 range, and allow connections
  the Mac starts. Softnet drops all IPv6. The rules are on `tart run`'s
  command line, so a sandbox can't boot without them.
- Softnet needs root: it is installed root-owned and setuid in a root-owned
  directory (`tart.softnet_dir`), and `up` refuses otherwise. Never use a
  passwordless sudo rule for a user-writable copy instead: any process
  running as the user could swap the binary and gain root.
- The guest's IP changes on every start (Softnet picks a fresh subnet), so
  the generated SSH config uses `ProxyCommand asbx ssh-proxy <instance>`
  with `HostKeyAlias`. This reverses ADR 0004's rejection of a proxy
  subcommand, for Tart only. The address comes from `tart ip --resolver
  dhcp`, which reads Softnet's root-owned DHCP reservations, never from the
  guest agent.
- The stock image's guest agent runs as `admin`, which has passwordless
  sudo. `build-base` makes the agent run as root and deletes `admin` before
  the base recipe runs; the recipe turns off SSH password logins, as on
  Linux.
- Not on Tart: the ingress firewall and managed WireGuard (ADR 0005),
  `client-config` and `ssh-config --direct`. Other machines reach sandboxes
  through the Mac with a `ProxyCommand` that runs `ssh <mac>` with the
  Mac's own `asbx ssh-proxy` command (`ssh-config --jump`).
- Tart sandboxes don't autostart at login: their launchd jobs are loaded
  only by `asbx start` and `up`.
- Tart and Softnet are under FSL-1.1-ALv2. asbx doesn't bundle them; users
  install them with Homebrew.
