# Host setup

Once per host: `asbx install`, `asbx init`, `asbx build-base`, then
configure name resolution. Run `asbx doctor` at any time to see what's
missing.

## Install

```
sandbox/asbx install [--force]
```

Creates a symlink to the script at `~/.local/bin/asbx`. `--force` replaces
whatever is already at that path. Warns if `~/.local/bin` is not on `$PATH`.
The script exits with `asbx requires PyYAML: pip install --user pyyaml` if
the `python3` that runs it lacks PyYAML.

## `asbx init`

- Creates `~/.config/agent-sandbox/` and its `projects/` directory. Set
  `ASBX_CONFIG_DIR` to use a different directory.
- Writes `config.yaml` with every default, at mode `0600`. If the file
  already exists, `init` prints its path and does nothing else. See
  [Configuration](configuration.md).
- Generates the SSH keypair `~/.config/agent-sandbox/id_ed25519`, unless it
  already exists.
- Creates the network ACL on the bridge. If that fails, `init` warns and
  continues; `asbx doctor --fix` creates it later.
- Puts `Include ~/.ssh/agent-sandbox.d/*.conf` on the first line of
  `~/.ssh/config`, creating the file at mode `0600` if needed. The line has
  to come before any `Host` block for the per-instance configs to apply.

Set `guest_user` before `build-base`, because the guest account is created
in the base image.

## `asbx build-base`

Bakes the image every sandbox is created from:

1. Launches a throwaway VM `asbx-base-build` from `base_image_source`
   (`images:debian/13/cloud`). The VM's disk size is
   `defaults.resources.disk`.
2. Cloud-init installs Docker, Node.js LTS, mise, the AWS CLI, and the
   harness CLIs (`@anthropic-ai/claude-code`, `@openai/codex`,
   `@google/gemini-cli`, opencode).
3. Copies the host binaries listed in `defaults.copy_binaries` (for
   example `omp`) into the VM.
4. Cleans the VM and publishes it as `agent-sandbox-base-<YYYYMMDD>`.
5. Points the `agent-sandbox-base` alias at the new image.

This takes several minutes, mostly `apt-get` and `npm install -g`. It refuses
to start if `asbx-base-build` already exists, so delete it first. Re-run it
to refresh tool versions for sandboxes created afterwards. Existing sandboxes
are not affected.

The published image keeps the build VM's disk size as its minimum. A
manifest asking for a smaller `resources.disk` fails at creation
([troubleshooting](troubleshooting.md#image-size-exceeds-volume-size)).

## Name resolution

The generated SSH config connects to `<instance>.<network.dns_domain>`, for
example `sandbox-demo.incus`. Incus's managed DNS on the bridge answers for
that name. The host's resolver has to be told to send those queries there.

The bridge is the virtual switch Incus created for the VMs (`network.name`,
default `incusbr0`). The host's address on it is both the VMs' gateway and
the DNS server to use. `incus network get incusbr0 ipv4.address` prints it
with a netmask, for example `10.210.86.1/24`; use the part before the `/`.
The bridge's `dns.mode` must be `managed`, which is the default.

With systemd-resolved, these settings last only as long as the bridge
exists, so put them in a unit:

```ini
# /etc/systemd/system/incus-dns-incusbr0.service
[Unit]
Description=Incus per-link DNS configuration for incusbr0
BindsTo=sys-subsystem-net-devices-incusbr0.device
After=sys-subsystem-net-devices-incusbr0.device

[Service]
Type=oneshot
ExecStart=/usr/bin/resolvectl dns incusbr0 <bridge ipv4 address>
ExecStart=/usr/bin/resolvectl domain incusbr0 ~incus
ExecStart=/usr/bin/resolvectl dnssec incusbr0 off
ExecStart=/usr/bin/resolvectl dnsovertls incusbr0 off
ExecStopPost=/usr/bin/resolvectl revert incusbr0
RemainAfterExit=yes

[Install]
WantedBy=sys-subsystem-net-devices-incusbr0.device
```

```
sudo systemctl daemon-reload && sudo systemctl enable --now incus-dns-incusbr0
```

If you changed the bridge's `dns.domain`, set `network.dns_domain` to match.

## Host firewall

Incus manages its own nftables tables for the bridge: masquerade, plus
dnsmasq for DHCP and DNS. It never touches the host's general-purpose
firewall. A host firewall with a default-deny policy therefore silently
drops bridge traffic. `ufw` has two common gaps:

- Its default `after.rules` drop inbound DHCP (67/udp) before any `allow`
  rule is consulted.
- A `deny (routed)` default policy drops everything the bridge forwards
  outbound, except ICMP and established connections.

Substitute your bridge and uplink (check the uplink with
`ip route get 1.1.1.1`; a rule on the wrong interface does nothing):

```
sudo ufw allow in on <bridge> to any port 67 proto udp
sudo ufw allow in on <bridge> to any port 53
sudo ufw route allow in on <bridge> out on <uplink>
sudo ufw reload
```

## `asbx doctor`

Prints PASS/FAIL/WARN for each check and exits 1 if anything FAILs.

- **Host:**
  - `incus` is on PATH, and the Incus version supports bridged-NIC ACLs.
  - The qemu driver and `/dev/kvm` are available.
  - PyYAML is installed.
  - The SSH key exists and the `Include` line is in `~/.ssh/config`.
  - The base image alias exists.
  - The network ACL exists and its rules match the current computation.
  - Every mount source any manifest would attach is readable.
  - The storage pool driver is reported. `dir` gets a WARN, because its VM
    snapshots are full copies.
- **Each manifest's sandbox:**
  - Its state.
  - Whether the egress ACL is attached.
- **Each running sandbox:**
  - Its name resolves on the host.
  - It has a live IPv4 default route.
  - It can complete a TCP handshake to a public host. This is checked only
    if the route is there.

The guest network probes are black-box. They don't assume which host
firewall, if any, is filtering, so they catch a dead DHCP lease, an ACL
misconfiguration and a host firewall drop the same way.

`--fix` repairs the two host-side items it can. It creates or rewrites the
network ACL, and adds the `~/.ssh/config` Include line.
