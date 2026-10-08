# The Incus host is the only SSH entry point

Sandboxes used to join the tailnet themselves. That put a Tailscale auth key
in every guest, needed a tailnet tag policy, and wrote the guest-reported
tailnet DNS name into the host's SSH config, which let a compromised guest
inject SSH config. Now guests run no Tailscale. The host reaches each guest
over the Incus bridge at `<instance>.<network.dns_domain>`, as resolved by
Incus's managed DNS. Other machines use `ProxyJump` through the host, with
their own keys added through `ssh.extra_pubkeys`.

## Considered options

- **Keep guest Tailscale.** Rejected because of the auth key in the guest,
  the tag policy, and the guest-controlled data reaching host SSH config.
- **An `asbx proxy` subcommand** as a `ProxyCommand`, instead of host DNS
  setup. Rejected in favour of plain Incus DNS plus a `doctor` check, which
  keeps the SSH stanzas static and usable by any SSH client.
- **Copying the host's asbx key to other machines.** Rejected because each
  machine should keep its own private key.

## Consequences

- The host resolver must forward the DNS domain (default `.incus`) to the
  bridge. `doctor` checks that each running sandbox resolves.
- The generated host SSH config is built only from the instance name and
  `config.yaml`. Nothing the guest reports reaches it.
- Remote stanzas turn off host-key checking for sandbox hosts, because
  rebuilds change keys and only the host's known-hosts file is cleaned.
