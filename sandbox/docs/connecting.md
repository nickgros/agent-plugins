# Connecting

## From the Incus host

`asbx up <group>` writes `~/.ssh/agent-sandbox.d/<instance>.conf`, for
example `sandbox-demo.conf`. `asbx init` already made `~/.ssh/config`
include that directory. The instance name is then an ordinary SSH host
alias. It connects to `<instance>.incus` with the key from `asbx init` and a
dedicated `known_hosts`.

- **CLI:** `asbx ssh <group> [cmd...]`, or `ssh sandbox-demo`.
- **VSCode:** run *Remote-SSH: Connect to Host*, pick `sandbox-demo`, and
  open `/workspace`.
- **JetBrains Gateway:** add an SSH connection to `sandbox-demo` and open
  `/workspace` as the project root. Gateway reads `~/.ssh/config` the same
  way.

An interactive SSH login starts in `/workspace` instead of the home
directory. `ssh <host> <cmd>`, scp and IDE server bootstraps have no
terminal and keep starting in the home directory.

`asbx shell <group>` is an `incus exec` shell, for when SSH is broken.

## From another machine

Sandboxes are not on your tailnet or LAN. The Incus host is the jump host
([ADR 0004](adr/0004-host-is-the-jump-host.md)). Reach the host however you
normally do, for example over Tailscale. From there, SSH continues to
`<instance>.incus`, which only the host can resolve.

1. Add the other machine's public key to `config.yaml`. Use a full
   `authorized_keys` line with no options:

   ```yaml
   ssh:
     extra_pubkeys:
       - "ssh-ed25519 AAAA... laptop"
   ```

2. Run `asbx up <group>`. On every run, `up` rewrites the guest user's
   `authorized_keys` as the asbx key plus these keys.

3. On the host, print the stanza for that machine, then paste it into its
   `~/.ssh/config`:

   ```
   $ asbx ssh-config demo --jump you@workstation
   Host sandbox-demo
       HostName sandbox-demo.incus
       User agent
       ProxyJump you@workstation
       IdentityFile ~/.ssh/id_ed25519
       IdentitiesOnly yes
       StrictHostKeyChecking no
       UserKnownHostsFile /dev/null
       LogLevel ERROR
   ```

   `--jump` is how the other machine reaches the host. `--identity` sets the
   private key path on that machine. `User` is `guest_user`. Add one block
   per sandbox. Do not use a `Host sandbox-*` wildcard: VSCode lists only
   literal host names. If you type the name instead of picking it, VSCode
   writes its own `Host sandbox-demo` / `HostName sandbox-demo` block at the
   top of the file, and that block wins.

VSCode Remote-SSH honours `ProxyJump` from this file. JetBrains Gateway has
not been checked.

Host-key checking is off in this stanza because `rm` and `rebuild` clean
only the Incus host's known-hosts file. Without it, the other machine would
hit `REMOTE HOST IDENTIFICATION HAS CHANGED` after every rebuild. The cost is
that the guest's host key is not verified on the last hop. That hop runs
from your own jump host, over the bridge on that host.

## Direct access over an ingress interface

This is opt-in. It gives other machines a direct path to sandbox IPs, for
any IPv4 protocol, including UDP (QUIC, mosh). The host's firewall admits
only traffic arriving on an ingress interface
([security](security.md#network-ingress),
[ADR 0005](adr/0005-host-enforced-ingress.md)). Every client reaches every
sandbox, on any port.

Pick one, or both:

- **Adopt an interface you already run**, such as `tailscale0`, ZeroTier or
  OpenVPN. It must be a kernel WireGuard or `tun` device.
  `asbx ingress setup --adopt tailscale0` adds it to
  `network.ingress.interfaces`; `--adopt` alone lists the eligible
  interfaces and asks. Clients on that network must also route the bridge
  subnet through the host; that part is yours, for example a Tailscale
  subnet route.
- **Create managed WireGuard.** `asbx ingress setup --create` enables
  `network.ingress.wireguard`, generates the host key at
  `~/.config/agent-sandbox/wireguard.key`, writes
  `/etc/wireguard/asbx0.conf` after showing it, and enables
  `wg-quick@asbx0`. Set `network.ingress.wireguard.endpoint` to the
  `host:port` clients dial; asbx never guesses it.

For managed WireGuard, each client keeps its own key:

1. On the client, run `wg genkey | tee laptop.key | wg pubkey`.
2. Add the public key to `config.yaml` with an address inside
   `network.ingress.wireguard.address`:

   ```yaml
   network:
     ingress:
       wireguard:
         clients:
           laptop: {public_key: "<base64>", address: 10.99.0.2}
   ```

3. Run `asbx ingress setup` to reinstall the WireGuard config.
4. Run `asbx client-config laptop` and use the output on the client,
   replacing the `PrivateKey` placeholder with the client's key:

   ```
   [Interface]
   PrivateKey = <this machine's private key>
   Address = 10.99.0.2/32

   [Peer]
   PublicKey = <host public key>
   Endpoint = <network.ingress.wireguard.endpoint>
   AllowedIPs = <bridge subnet>
   PersistentKeepalive = 25
   ```

To revoke a client, remove its entry and re-run `asbx ingress setup`.

For SSH over the ingress path, print a stanza with `--direct` instead of
`--jump`:

```
$ asbx ssh-config demo --direct
```

Its `HostName` is the sandbox's pinned IPv4 address and it has no
`ProxyJump`. The key setup is the same as for the jump host:
`ssh.extra_pubkeys` and `--identity`. The address survives `rebuild`.
