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
