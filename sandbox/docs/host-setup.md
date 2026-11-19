# Host setup

Once per host: `asbx install`, `asbx init`, `asbx build-base`, then, on
Linux, configure name resolution. On macOS install Tart and Softnet first
([macOS](#macos-tart)). Run `asbx doctor` at any time to see what's missing.

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
- Runs the backend's host checks. Incus: refreshes the network ACL on the
  bridge, then checks the ingress firewall if you have opted into it; a
  firewall that isn't loaded doesn't stop the ACL refresh. Tart: checks for
  `tart`, macOS 26+ and a root-owned setuid Softnet. A failure is a warning
  and `init` continues; `asbx doctor` says what's missing (on Incus,
  `asbx doctor --fix` creates the ACL).
- Puts `Include ~/.ssh/agent-sandbox.d/*.conf` on the first line of
  `~/.ssh/config`, creating the file at mode `0600` if needed. The line has
  to come before any `Host` block for the per-instance configs to apply.

Set `guest_user` before `build-base`, because the guest account is created
in the base image.

## `asbx build-base`

Bakes the image every sandbox is created from:

1. Boots a throwaway VM `asbx-base-build` from the backend's source image:
   `base_image_source` (`images:debian/13/cloud`) on Incus,
   `tart.base_image_source` (`ghcr.io/cirruslabs/debian:trixie`) on Tart.
   Its CPU, memory and disk are `defaults.resources`.
2. Tart only: makes the guest agent run as root and deletes `admin`
   ([below](#build-base-on-tart)).
3. Runs the base recipe, one bash script, as root. It installs Docker,
   Node.js LTS, mise, the AWS CLI, and the harness CLIs (`@openai/codex`,
   `@google/gemini-cli`, opencode). It also runs the omp and Claude Code
   installers as the guest user (`~/.local/bin/omp`, `~/.local/bin/claude`,
   so `omp update` and `claude update` work in the guest), and installs
   `@getpaseo/cli` with an enabled `asbx-paseo.service` that runs
   `paseo daemon run` as the guest user in a login shell. The unit waits for
   `/etc/profile.d/asbx-env.sh`, so a new sandbox's daemon first starts from
   `asbx up`, with the harness env in place. Dictation is turned off in the
   guest's `~/.paseo/config.json`, so the daemon downloads no speech models;
   clients do dictation locally. Node and the AWS CLI match the host's CPU
   architecture (x86_64 or arm64).
   `asbx up` (not the image) owns `/etc/environment`, which puts mise shims
   and `~/.local/bin` on PATH for non-login shells.
4. Copies the host binaries listed in `defaults.copy_binaries` (empty by
   default) into the guest user's `~/.local/bin`. Non-ELF files (such as
   macOS builds) are skipped with a warning.
5. Cleans the VM and publishes it. Incus: as `agent-sandbox-base-<YYYYMMDD>`,
   then points the `agent-sandbox-base` alias at it. Tart: renames the VM to
   `agent-sandbox-base` (`image_alias`), replacing the previous one.

This takes several minutes, mostly `apt-get` and `npm install -g`. It refuses
to start if `asbx-base-build` already exists, so delete it first
(`incus delete --force asbx-base-build` / `tart delete asbx-base-build`).
Re-run it to refresh tool versions for sandboxes created afterwards.
Existing sandboxes are not affected.

On Incus, the published image keeps the build VM's disk size as its
minimum. A manifest asking for a smaller `resources.disk` fails at creation
([troubleshooting](troubleshooting.md#image-size-exceeds-volume-size-incus)). Tart
can only grow a disk, so a smaller `resources.disk` keeps the image's size.

## macOS (Tart)

### Install

macOS 26 or later (Softnet requires it), Apple silicon, Python 3 with PyYAML.

```
brew install openai/tools/tart     # pulls in softnet
```

### Softnet

Softnet is the egress filter and needs root. Tart runs the first `softnet`
on its PATH, and asbx puts `tart.softnet_dir` first. Install it root-owned
and setuid in a root-owned directory:

```
sudo install -d -o root -g wheel -m 0755 /usr/local/libexec/asbx
sudo install -o root -g wheel -m 4555 "$(brew --prefix softnet)/bin/softnet" /usr/local/libexec/asbx/softnet
```

`up`, `build-base` and every VM boot refuse unless the directory and the
binary are owned by root and not group- or world-writable, and the binary
is setuid. Don't use a passwordless sudo rule for Homebrew's copy instead:
the user can write it, so any process running as the user could swap the
binary and gain root ([ADR 0006](adr/0006-one-backend-per-host-os.md)).
Re-run the `install` line after upgrading softnet.

### `build-base` on Tart

The stock image's guest agent runs as `admin`, which has passwordless sudo.
Before the base recipe runs, `build-base`:

1. Adds a systemd drop-in
   (`/etc/systemd/system/tart-guest-agent.service.d/asbx.conf`) that runs
   the agent as root.
2. Reboots, checks that the agent now runs as uid 0, and deletes `admin`
   (`userdel -r`). It fails if the agent isn't root.

Then the base recipe and `copy_binaries` run through the root agent, as on
Linux. The recipe turns off SSH password logins on both backends (the stock
image's cloud-init turns them on): it deletes every
`/etc/ssh/sshd_config.d/*.conf` that sets `PasswordAuthentication yes` and
writes `00-asbx.conf` with `PasswordAuthentication no`.

Before publishing, the guest powers itself off, so its last changes reach
the disk. If it hasn't within a minute, `build-base` forces it off with
`tart stop` and fails instead of publishing an image that may lack them;
delete `asbx-base-build` and retry.

A VM named `image_alias` that `build-base` didn't publish doesn't count as a
base image.

### Differences from Linux

- No name-resolution or host-firewall setup: the SSH config reaches a
  sandbox through `asbx ssh-proxy` ([connecting](connecting.md#tart-macos)).
- No ingress firewall or managed WireGuard.
- Each sandbox runs from a launchd job (`com.asbx.vm.<instance>`) loaded
  into your GUI session by `asbx up`, `start` and `restart`. Its plist lives
  in `~/.config/agent-sandbox/launchd/`, not `~/Library/LaunchAgents`, so
  sandboxes don't autostart at login. launchd restarts `tart run` only
  after a non-zero exit.
- `tart run` output: `~/.config/agent-sandbox/logs/<instance>.log`.
  Metadata: `~/.config/agent-sandbox/state/<instance>.json`.

## Name resolution

Linux (Incus) only.

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

Linux (Incus) only.

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

## Ingress firewall

Linux (Incus) only.

The firewall is opt-in. `asbx ingress setup` installs or updates it, and once
it is installed `asbx doctor --fix` reinstalls it. Both show what they write
and ask before running sudo. `--print` prints the files and commands instead.
Configuring any ingress interface makes it required. Without it, a LAN
device with a route to the bridge subnet through the host can reach any
sandbox port.

To remove it, delete both files; while either remains, `up` keeps requiring
the firewall:

```
sudo systemctl disable --now asbx-firewall.service
sudo nft delete table inet asbx
sudo rm /etc/systemd/system/asbx-firewall.service /etc/asbx/firewall.nft
sudo rmdir /etc/asbx
sudo systemctl daemon-reload
```

- `/etc/asbx/firewall.nft` holds the `inet asbx` table. It drops every new
  connection forwarded into the bridge unless it arrives over IPv4 on an
  ingress interface. On the managed WireGuard interface it also drops new
  connections to the host itself and forwarding anywhere but the bridge
  ([security](security.md#network-ingress)).
- `/etc/systemd/system/asbx-firewall.service` loads it at boot, before the
  network and Incus start. The unit is enabled and started.
- With managed WireGuard (`--create`), the host key is generated at
  `~/.config/agent-sandbox/wireguard.key` (mode `0600`),
  `/etc/wireguard/<interface>.conf` is written, and `wg-quick@<interface>`
  is enabled. This needs `wireguard-tools` (`wg`, `wg-quick`) on the host.

`--adopt` and `--create` also edit `config.yaml`. If the file has comments,
they print the snippet to add instead.

asbx's table only adds drops. Other host firewalls can still drop forwarded
ingress traffic: `ufw`'s routed policy, firewalld, or Docker's `FORWARD
DROP`. Allow forwarding from the ingress interface to the bridge in those.
For managed WireGuard, open `network.ingress.wireguard.listen_port`
(UDP, default 51820) on the host, for example
`sudo ufw allow 51820/udp`.

Known gap: unprivileged asbx can't read live nftables, so `up` and `doctor`
trust the active unit and the installed file. A manual
`sudo nft flush ruleset` removes the table without either noticing. Restart
the unit (`sudo systemctl restart asbx-firewall`) after a flush.

## `asbx doctor`

Prints PASS/FAIL/WARN for each check and exits 1 if anything FAILs.

- **Host, both backends:**
  - The backend in use.
  - PyYAML is installed.
  - The SSH key exists and the `Include` line is in `~/.ssh/config`.
  - Every mount source any manifest would attach is readable.
  - When a manifest uses `aws-creds`: the `aws` CLI is on PATH, and the
    refresh job is installed (WARN otherwise).
- **Host, Incus:**
  - `incus` is on PATH, and the Incus version supports bridged-NIC ACLs.
  - The qemu driver and `/dev/kvm` are available.
  - The base image alias exists.
  - The network ACL exists and its rules match the current computation.
  - Once the ingress firewall is opted in, `asbx-firewall.service` is active
    and the installed rules match the config (FAIL). A host that never opted
    in gets a PASS saying so. Each ineligible ingress interface gets a WARN,
    as do an inactive `wg-quick@<interface>` and an empty `endpoint` when
    managed WireGuard is enabled.
  - The storage pool driver is reported. `dir` gets a WARN, because its VM
    snapshots are full copies.
- **Host, Tart:**
  - `tart` is on PATH (with its version).
  - macOS is 26 or later, and the machine is Apple silicon.
  - `<tart.softnet_dir>/softnet` is root-owned and setuid in a root-owned
    directory. On failure it prints the install commands.
  - The base image exists and was built by `asbx build-base`.
- **Each manifest's sandbox:**
  - Its state and egress mode (`acl` on Incus, `softnet` on Tart).
  - Incus: whether the egress ACL is attached, and whether the NIC has IPv4
    and MAC filtering and a pinned `ipv4.address`.
- **Each running sandbox:**
  - Incus: its name resolves on the host.
  - It has a live IPv4 default route.
  - It can complete a TCP handshake to a public host. This is checked only
    if the route is there.
  - Each pushed AWS credential's remaining lifetime.

The guest network probes are black-box. They don't assume which host
firewall, if any, is filtering, so they catch a dead DHCP lease, an ACL
misconfiguration and a host firewall drop the same way.

`--fix` repairs the host-side items it can: the `~/.ssh/config` Include
line, and on Incus the network ACL and, on a host that has opted in, the
firewall unit through sudo. It never installs the firewall on a host that
hasn't. On Tart it changes nothing else: installing Softnet needs sudo.
