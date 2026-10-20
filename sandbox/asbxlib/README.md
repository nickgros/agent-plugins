# asbxlib

Implementation of the `asbx` CLI. `../asbx` is the entry point: it checks
for PyYAML, then calls `cli.main`. This is the stdlib plus PyYAML only.

## Modules

| Module           | Owns                                                                                                       |
|------------------|------------------------------------------------------------------------------------------------------------|
| `cli.py`         | argparse wiring; maps `AsbxError` to `error: ...` and exit 1                                               |
| `config.py`      | `config.yaml` defaults, strict merge, `ASBX_CONFIG_DIR`, harness item catalog and validation, `network.ingress` validation, single-key rewrites of `config.yaml` |
| `manifest.py`    | Manifest validation and merging over config defaults; `Manifest`, `Repo`, `Mount`                          |
| `incus.py`       | `Incus`: the only place that spawns `incus`. Non-root `exec_in` goes through `runuser` for a full session. |
| `hostsetup.py`   | `init`, `install`                                                                                          |
| `baseimage.py`   | `build-base`: cloud-init rendering, Node download pinning, publish and alias                               |
| `up.py`          | `up` orchestration                                                                                         |
| `guest.py`       | In-guest setup: cloud-init, waits, mounts and their readability rule, `authorized_keys`, env, git identity |
| `netacl.py`      | Egress ACL rules, attach to NIC (fail closed), NIC IPv4/MAC filtering and pinned `ipv4.address`, Incus version gate |
| `ingress.py`     | `inet asbx` ruleset, `asbx-firewall.service`, sudo install, interface eligibility, managed WireGuard, `ingress setup`, `client-config`, firewall status for `up`/`doctor` |
| `sshconf.py`     | Generated per-instance SSH config and the `~/.ssh/config` Include line                                     |
| `auth.py`        | Auth providers (`github-gh`, `aws-creds`, inline `{command, check}`)                                       |
| `awscreds.py`    | Export host AWS credentials, push them into guests, `aws-refresh`, systemd user timer and path unit        |
| `harnesssync.py` | Push host harness settings (omp config, rules, hooks) into guests; `settings-sync`                         |
| `provision.py`   | Clone, remotes, compose, setup hook                                                                        |
| `lifecycle.py`   | `list`, `ssh`, `shell`, start/stop/restart, snapshot/restore, `rebuild` (repo safety), `rm`                |
| `doctor.py`      | `doctor` checks and `--fix`                                                                                |
| `netcheck.py`    | Black-box guest network probes used by `doctor`                                                            |
| `host.py`        | Host-side helpers: key generation, known-hosts, DNS lookup, private file writes, sudo, sysfs, routes, `wg` |
| `ui.py`          | `ok`/`warn`/confirm prompts                                                                                |

## Invariants

- **Egress fails closed.** `netacl.apply_nic_acl` raises if Incus can't
  attach the ACL. Never add a guest-side fallback
  ([ADR 0003](../docs/adr/0003-per-nic-egress-acl-fail-closed.md)).
- **Ingress fails closed once opted in.** `up` refuses unless the firewall
  unit is active and its file matches the config, whenever either installed
  file exists or an ingress interface is configured. It raises if Incus won't
  pin or filter the NIC ([ADR 0005](../docs/adr/0005-host-enforced-ingress.md)).
- **The firewall table is asbx-owned.** asbx writes only `inet asbx`, and
  replaces it whole. Never add rules to Incus's or other tables.
- **No guest data in host SSH config, client configs or pinned IPs.**
  `sshconf` and `ingress` build them only from the instance name, Incus host
  data (NIC config, DHCP leases by MAC) and `config.yaml`. Never read a
  guest value into them
  ([ADR 0004](../docs/adr/0004-host-is-the-jump-host.md)).
- **Destructive commands don't guess.** `rebuild` and `restore` refuse when
  they can't prove that no work will be lost. An unreadable `/workspace`
  is not treated as an empty one.
- **Instance state is recorded in `user.asbx.*` Incus config keys.** These
  are `group`, `base_image`, `manifest_sha256` and `egress_mode`.

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
- `conftest.py` provides `cfg` and `config_root`, which points
  `ASBX_CONFIG_DIR` at a temporary directory.
- For a live smoke test, use a scratch `ASBX_CONFIG_DIR` and a throwaway
  manifest, and `asbx rm` the sandbox afterwards.
