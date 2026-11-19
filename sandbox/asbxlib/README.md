# asbxlib

Implementation of the `asbx` CLI. `../asbx` is the entry point: it checks
for PyYAML, then calls `cli.main`. This is the stdlib plus PyYAML only.

## Modules

| Module           | Owns                                                                                                       |
|------------------|------------------------------------------------------------------------------------------------------------|
| `cli.py`         | argparse wiring; `make_backend` (config `backend`, `auto` = Incus on Linux, Tart on macOS); `ssh-proxy`; maps `AsbxError` to `error: ...` and exit 1 |
| `config.py`      | `config.yaml` defaults, strict merge, `ASBX_CONFIG_DIR`, `backend` validation, harness item catalog and validation, `network.ingress` validation, single-key rewrites of `config.yaml` |
| `manifest.py`    | Manifest validation and merging over config defaults; `Manifest`, `Repo`, `Mount`                          |
| `backend.py`     | The seam: `Guest`, `SandboxBackend`, `SandboxSpec`, `SshRoute`, normalized states, metadata keys; the `runuser` command every `Guest` runs non-root commands through; `add_check` for doctor rows |
| `incus.py`       | `Incus`: the only place that spawns `incus`. Non-root `exec_in` goes through `runuser` for a full session. |
| `incusbackend.py`| `IncusGuest`, `IncusBackend` (Linux): create/start with the ACL bound first, mount devices, pinning, waits, snapshots, base image publish/alias, `user.asbx.*` metadata, Incus `doctor` rows |
| `netacl.py`      | Private to `incusbackend`: egress ACL rules, attach to NIC (fail closed), NIC IPv4/MAC filtering and pinned `ipv4.address`, Incus version gate |
| `tart.py`        | macOS backend: `Tart`, the only place that spawns `tart` for VMs                                           |
| `tartbackend.py` | macOS backend: `TartGuest`, `TartBackend` over Tart + the root-owned Softnet egress filter                 |
| `launchd.py`     | macOS backend: LaunchAgent plist rendering, install/bootstrap/bootout (also the AWS refresh job)           |
| `hostsetup.py`   | `init` (config, key, backend `preflight`), `install`                                                       |
| `baseimage.py`   | `build-base`: the bash base recipe (the only owner of SSH password-login settings), `prepare_build_vm` (push and run the recipe, copy host ELF binaries, clean for imaging) that every backend's `build_base` calls, Node download pinning per arch |
| `up.py`          | `up` orchestration over a `SandboxBackend`; `spec_for`, `run_provision` (records the manifest hash)         |
| `guest.py`       | In-guest setup over a `Guest`: hostname, `authorized_keys`, env, shell, git identity, cloud-init wait; mount readability rule |
| `ingress.py`     | Incus only: `inet asbx` ruleset, `asbx-firewall.service`, sudo install, interface eligibility, managed WireGuard, `ingress setup`, `client-config`, firewall status for `up`/`doctor` |
| `sshconf.py`     | Per-instance SSH config rendered from an `SshRoute` (`HostName` or `ProxyCommand`, always `HostKeyAlias`) and the `~/.ssh/config` Include line |
| `auth.py`        | Auth providers (`github-gh`, `aws-creds`, inline `{command, check}`)                                       |
| `awscreds.py`    | Export host AWS credentials, push them into guests, `aws-refresh`, the refresh job (systemd user units on Linux, a LaunchAgent on macOS) |
| `harnesssync.py` | Push host harness settings (omp config, rules, hooks) into guests; `settings-sync`                         |
| `provision.py`   | Clone, remotes, worktree dirs, workspace guide (`/workspace/AGENTS.md`), compose, setup hook               |
| `lifecycle.py`   | `list`, `ssh`, `shell`, start/stop/restart, snapshot/restore, `rebuild` (repo safety), `rm`                |
| `doctor.py`      | `doctor` checks and `--fix`; backend rows come from `SandboxBackend.doctor_checks`                         |
| `netcheck.py`    | Black-box guest network probes used by `doctor`                                                            |
| `host.py`        | Host-side helpers: key generation, known-hosts, DNS lookup, private and atomic file writes, sudo, sysfs, routes, `wg`, TCP relay |
| `ui.py`          | `ok`/`warn`/confirm prompts                                                                                |

## Invariants

- **One seam.** Only `incusbackend.py` (with `incus.py`, `netacl.py`) knows
  Incus devices, ACLs, image aliases and `user.asbx.*` keys; only the Tart
  modules know Tart. Everything else talks to `SandboxBackend` or `Guest`.
- **Egress fails closed.** No sandbox boots without its egress policy:
  `IncusBackend.create` attaches the ACL before the first start and
  `netacl.apply_nic_acl` raises if Incus can't attach it. Never add a
  guest-side fallback
  ([ADR 0003](../docs/adr/0003-per-nic-egress-acl-fail-closed.md)).
- **Ingress fails closed once opted in.** `up` refuses unless the firewall
  unit is active and its file matches the config, whenever either installed
  file exists or an ingress interface is configured. It raises if Incus won't
  pin or filter the NIC ([ADR 0005](../docs/adr/0005-host-enforced-ingress.md)).
- **The firewall table is asbx-owned.** asbx writes only `inet asbx`, and
  replaces it whole. Never add rules to Incus's or other tables.
- **No guest data in host SSH config, client configs or pinned IPs.**
  Backends build `SshRoute`s and addresses only from the instance name,
  host-side VM-service data (Incus NIC config and DHCP leases by MAC) and
  `config.yaml`; `sshconf` and `ingress` use nothing else. Never read a
  guest value into them
  ([ADR 0004](../docs/adr/0004-host-is-the-jump-host.md)).
- **Destructive commands don't guess.** `rebuild` and `restore` refuse when
  they can't prove that no work will be lost. An unreadable `/workspace`
  is not treated as an empty one.
- **Instance state is recorded as backend metadata** (`meta_get`/`meta_set`;
  `user.asbx.*` config keys on Incus): `group`, `base_image`,
  `manifest_sha256` and `egress_mode`.

## Tests

```
cd sandbox && pytest -q -p no:cacheprovider
```

- Tests never spawn `incus`. `tests/fake_incus.py`'s `FakeIncus` does three
  things:
  - Records every argv in `.calls`.
  - Replays scripted results through `.stub(*tokens, rc=, stdout=, stderr=)`.
    A rule matches when its tokens appear in order (subsequence match).
  - Captures pushed file bodies in `.pushed`.
- Like the real client, `FakeIncus` returns no stdout or stderr for tty
  runs. Code that needs to inspect output must run captured.
- Code behind the seam is tested through the real Incus implementation over
  the fake: `IncusGuest(FakeIncus(), "inst")` for guest-side code,
  `IncusBackend(FakeIncus())` for commands. `FakeIncus` stays the only
  test double for Incus.
- Tests never spawn `tart` or `launchctl` either. `tests/fake_tart.py`'s
  `FakeTart` is the runner of a real `Tart` (`FakeTart().tart()`): it records
  argv, replays `.stub(...)` rules like `FakeIncus`, and keeps a VM
  inventory. `FakeLaunchctl` replaces `launchd.run_launchctl`; linked to a
  `FakeTart`, loading a VM's job marks the VM running. Tart code is tested
  through `TartGuest` and `TartBackend(FakeTart().tart(), stat_fn=...)`,
  where `stat_fn` decides what the Softnet guard sees. These two stay the
  only doubles for Tart and launchd.
- `conftest.py` provides `cfg` and `config_root`, which points
  `ASBX_CONFIG_DIR` at a temporary directory.
- For a live smoke test, use a scratch `ASBX_CONFIG_DIR` and a throwaway
  manifest, and `asbx rm` the sandbox afterwards.
