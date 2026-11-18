# agent-plugins

This repo sets up agent environments: `install.sh` writes to `~/.agents/skills`, `~/.omp/agent` and `~/.config/agent-sandbox`.

- Work on this repo in the `agent-plugins` sandbox (`asbx up agent-plugins`, manifest in `sandbox-projects/`). Its guest has no host skills mount, and every provision runs `install.sh` from a clean checkout, so it is the fresh-machine test.
- On the host, run only what touches the host on purpose: `./install.sh`, and `asbx` commands, which need host Incus. Test installs and syncs in the sandbox or in a scratch project directory, never by redirecting `HOME`.
- `sandbox/` is the `asbx` tool (tests: `python3 -m pytest sandbox/tests`). `sandbox-projects/` is how I use it.
