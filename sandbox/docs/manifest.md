# Manifests

A manifest defines one project group. Each lives at
`~/.config/agent-sandbox/projects/<group>.yaml`, and `<group>` is the name
you pass to `asbx up`, `asbx ssh` and so on.

Unknown keys are a hard error. This applies at the top level and under
`resources`, `repos[*]`, `mounts[*]` and `services`, and the error names the
key (for example `resources.gpu`).

## Keys

All keys are optional.

| Key                  | Meaning                                                                                                                                |
| -------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `name`               | Instance name suffix (instance is `<instance_prefix><name>`). Defaults to the file stem.                                               |
| `resources`          | `cpu` (int), `memory` and `disk` (`^\d+(MiB\|GiB)$`). Merged over `defaults.resources`.                                                |
| `repos`              | Repos cloned into `/workspace/<dir>`. See [below](#repos).                                                                             |
| `remotes`            | Remotes for every repo, merged over `defaults.remotes`. See [below](#remotes).                                                         |
| `mounts`             | Read-only host mounts: `{host, guest, mode: ro}`. Merged with `defaults.mounts` by `guest` path; a manifest entry replaces a default. |
| `services.compose`   | Compose file pushed into the guest and run with `docker compose -f <path> up -d`.                                                      |
| `env`                | Environment for repo setup, compose and the setup hook. Also written to `/etc/profile.d/asbx-env.sh`.                                  |
| `auth`               | Auth providers. Replaces `defaults.auth`. See [below](#auth-providers).                                                                |
| `aws_profiles`       | Extra host AWS profiles for the `aws-creds` provider, added to `aws.profiles` from `config.yaml`. Names match `[A-Za-z0-9._-]+`.       |
| `setup`              | Provision hook. Pushed to `/home/<guest_user>/.asbx/provision.sh` and run from `/workspace` on every provision, so it must be idempotent. |

Rules for specific keys:

- **`services.compose` and `setup`:** paths relative to the manifest's
  directory. The file must exist, and a path that escapes the directory
  through `..` is rejected.
- **`mounts`:** `host` and `guest` are required. `mode` accepts only `ro`.
  Mount sources must be world-readable
  ([security](security.md#host-files)).

## Repos

```yaml
repos:
  - url: https://github.com/nickgros/foo.git   # required
    dir: foo          # default: repo basename minus .git; must be unique
    ref: main         # passed to `git clone --branch`
    setup: pnpm install   # bash -lc in /workspace/<dir>, first clone only
    remotes:
      fork: jsmith
```

A repo is cloned only if its directory doesn't exist yet, and `setup` runs
only on that first clone.

## Remotes

Each remote maps a name to one of:

- an owner, which replaces the owner segment of the repo's own URL and keeps
  host, scheme and repo name;
- a full URL, which is used as given.

Precedence, highest first: top-level `remotes`, then `defaults.remotes`,
then the repo's own `remotes`. Remotes are reconciled on every `up` and
`provision`, for new and existing clones alike. A missing remote is added, a
remote whose URL has drifted gets `set-url`, and the rest are left alone.

## Auth providers

Each `auth` entry is either a built-in provider name or an inline
`{command, check}`. `check` is run first, and if it succeeds the provider is
skipped. `asbx auth <group>` re-runs only the providers that aren't
satisfied.

```yaml
auth:
  - github-gh
  - aws-creds
  - { command: "codex login", check: "test -f ~/.codex/auth.json" }
```

- **`github-gh`:**
  - Runs `gh auth login` with the scope `admin:ssh_signing_key`. If gh is
    already logged in without that scope, it widens the existing token with
    `gh auth refresh` instead.
  - Runs `gh auth setup-git`.
  - Generates `~/.ssh/id_ed25519` in the guest if it is missing, and uploads
    the public half to your GitHub account as a **signing** key titled
    `asbx <instance>`. A "key is already in use" response is tolerated.
  - Sets `gpg.format ssh`, `user.signingkey` and `commit.gpgsign true`, so
    commits made in the guest are signed.
  - It counts as satisfied when `gh auth status` succeeds and lists the
    signing-key scope.
- **`aws-creds`:**
  - Pushes host-minted, short-lived credentials into the guest; the guest
    never gets an SSO token or any profile outside the allowed set. The
    allowed set is `aws.profiles` from `config.yaml` plus the manifest's
    `aws_profiles`.
  - On the host it runs `aws configure export-credentials --profile <p>` per
    profile. Long-term keys are refused: the output needs a session token and
    an expiration. If the host's SSO session expired, run `aws sso login`
    there.
  - In the guest it deletes `~/.aws`, writes
    `~/.asbx/aws/<profile>.json` (mode `0600`) and an `~/.aws/config` whose
    profiles use `credential_process = cat ~/.asbx/aws/<profile>.json`.
    Credential files for profiles no longer allowed are removed.
  - It has no "already authenticated" short-circuit: every run re-exports.
  - Credentials last about an hour. `asbx aws-refresh --install-timer`
    installs a systemd user timer on the host that re-pushes them every 10
    minutes (`asbx aws-refresh [group]` does one push by hand). `asbx doctor`
    reports the timer and each profile's remaining lifetime.

## Example

```yaml
name: demo
resources:
  cpu: 6
  memory: 12GiB
repos:
  - url: https://github.com/nickgros/agent-plugins.git
    setup: "git log --oneline -1"
remotes:
  upstream: Sage-Bionetworks
mounts:
  - { host: ~/.agents/skills, guest: /home/agent/.agents/skills, mode: ro }
services:
  compose: services/demo.compose.yaml
env:
  DATABASE_URL: "mysql://root@127.0.0.1:3306/app"
auth: [github-gh]
setup: demo-provision.sh
```

`services/demo.compose.yaml` and `demo-provision.sh` sit next to `demo.yaml`
in `projects/`.
