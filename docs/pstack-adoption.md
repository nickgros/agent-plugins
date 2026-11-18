# pstack adoption plan

This plan records which parts of [pstack](https://github.com/cursor/plugins/tree/main/pstack) this repo adopts, and how each part reaches a fresh machine. Principle-by-principle reasoning is in [pstack-principles-vs-omp.md](pstack-principles-vs-omp.md).

## How things reach a machine

`./install.sh` (see `skills/README`) does all of it, and is safe to rerun:

| Channel | Source | Host | Sandbox |
| --- | --- | --- | --- |
| Skills | `skill-sources.txt` (team skills), then `skills/` | `~/.agents/skills`. A skill here overrides a same-named team skill because it installs last. | `defaults.mounts` mounts the host's `~/.agents/skills` read-only |
| OMP rules and agents | `omp/AGENTS.md`, `omp/agents/` | symlinked into `~/.omp/agent/` | `harness_settings.sync: [omp]` pushes them as real files on `up` and `settings-sync` |
| Sandbox projects | `sandbox-projects/` | symlinked into `~/.config/agent-sandbox/projects/` | |

- `config.yml` stays machine-local. OMP rewrites it (`/model`, setup), and provider access differs by machine. Agents name a role (`model: "@peer_gpt"`), and each machine maps that role in `modelRoles`. If a role is not mapped, task dispatch falls back to the parent session's model.
- `omp plugin link <repo>/omp` was rejected because plugins live in `~/.omp/plugins`, which the sandbox does not sync.

## Parts

Each part reaches a machine in one of four ways:

- **Lock.** A `skills-lock.json` entry pointing at `pstack/skills/<name>/SKILL.md`. The files stay unchanged, so `pnpm skills:sync` keeps them current. Do not point an entry at `harness/pstack/claude-code/...`; `pnpx skills add` chooses that path by default.
- **Owned.** A skill in `skills/` with no lock entry. Use this when the upstream text needs edits that a sync would overwrite.
- **omp/.** `omp/AGENTS.md` or `omp/agents/`.
- **Skip.** Not adopted.

| Part | Decision | Mechanism | Status |
| --- | --- | --- | --- |
| 10 design principles, `typescript-best-practices` | adopt | Lock, reached through the owned `design-principles` index | done |
| `unslop` | keep | Lock (already tracked). The next sync replaces the local copy with the condensed upstream version. | done |
| attack-the-premise, encode-lessons-in-structure | adopt | One line each in `omp/AGENTS.md` | done |
| `blast-radius`, `benchmark-checklist`, `why`, `sharpen`, `in-your-own-words`, `bro`, `correct`, `principle-fix-root-causes` | adopt | Lock. All are user-invoked (`/skill:<name>`), so they add nothing to the catalog. | done |
| `architect` | adopt | Lock. Its link to `arena` resolves now that `arena` is adopted. | done |
| `how` | adopt | Owned. Spawns the bundled read-only `scout` agent in place of Cursor's `generalPurpose, readonly: true`. | done |
| `interrogate` + `arena` | adopt together | Owned. Runners are `task` (session model) and `peer-gpt`; the cross-judge is the read-only `judge-gpt`. Both agents are in `omp/agents/` and use the `peer_gpt` role. `arena` checks the role is mapped and falls back to a single-family panel, saying so. The Cursor model slugs and `pstack-models.mdc` are gone. | done |
| `create-` and `maintain-formal-verification-skill`, `statechart` | adopt | Lock. `statechart` installs TLC and Java through `mise` on first run. The base image already has `mise`. `sandbox-projects/synapse-provision.sh` pre-installs both, reading the pins from the mounted `statechart/scripts/run.ts`. | done |
| `reflect` | adopt | Owned. Edits a skill's source (this repo, or its upstream per `skills-lock.json`), never the installed copy that `install.sh` overwrites. | done |
| `create-` and `maintain-verification-skill` | adopt | Owned. The generated skill goes to the project's `.agents/skills/verify-<app>/`, which omp and Codex read, in place of `.cursor/skills/`. | done |
| `tdd` | keep yours | `tdd` comes from `mattpocock/skills` through the lock, so borrowed edits would be overwritten. To take pstack's "confirm the failure reason before fixing" step, move `tdd` to Owned. | open |
| `teach`, `ketchup`, `recall`, `show-me-your-work`, `swarm`, `technical-writing` | skip | They overlap `handoff`, OMP's history, and OMP's `task` tool. `technical-writing` can be revisited later. | |
| `poteto-mode`, `poteto-help`, `setup-pstack`, `poteto-agent`, other principles | skip | They are a Cursor operating mode, or they repeat the OMP prompt. | |
| `automations/benny`, `make-bot-ui`, `ketchup-video` | skip | They depend on Cursor automations, a Grok/X bot, or video tooling. | |
| `no-comments` + `comment-sicko` agent | adopt | Owned skill (step 1 spawns the omp task agent; the `poteto-mode` reasoning reference is dropped), agent in `omp/agents/`. Copied from cursor/plugins `93b916d`. OMP only: other harnesses have no `comment-sicko` agent. It uses `why`, `architect`, and two principles, all installed. | done |

## Model panel

`interrogate` and `arena` need two or three different model families. Models that were confirmed to answer on this host's Bedrock account on 2026-11-17:

- Anthropic: `global.anthropic.claude-{opus,sonnet,haiku}-5-5`
- OpenAI: `global.openai.gpt-5.6-{sol,terra,luna}`, `gpt-6-{sol,astra,luna}`, `gpt-6.1-{sol,terra}`
- Others: `global.moonshotai.kimi-k3`, `global.zai.glm-5.3`

`gpt-6.2-{sol,astra}` appear in `aws bedrock list-inference-profiles`, but OMP 18.22.2 does not list them, even after `omp models refresh`. Every model on GitHub Copilot returned `model_not_supported`. Choosing a vendor is a policy question for the Bedrock account owner.

Each machine maps the role in its own `~/.omp/agent/config.yml`. Without it, `peer-gpt` and `judge-gpt` run on the session's model:

```yaml
modelRoles:
  peer_gpt: amazon-bedrock/global.openai.gpt-6.1-sol:high
```

Mapped on this host on 2026-11-18. A `peer-gpt` smoke task resolved to `amazon-bedrock/global.openai.gpt-6.1-sol` (from the child session's transcript).

## Drift on the current host

A fresh machine reproduces this repo plus the team repo, not the current host. Installed on this host but in neither: `humanizer`, `paseo*` (installed by Paseo), `resolving-merge-conflicts`, `setup-ts-deep-modules`, `wayfinder`.
