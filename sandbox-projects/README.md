# Sandbox projects

`asbx` manifests and provision hooks for the sandboxes I use. The tool itself lives in `sandbox/`. These files hold no credentials: `harness_credentials` only names host items to push.

`./install.sh` symlinks each file into `~/.config/agent-sandbox/projects/`. `asbx` resolves `setup:` relative to the manifest, so the hook is read through the symlink from this directory.
