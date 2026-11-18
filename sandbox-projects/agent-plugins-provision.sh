#!/bin/bash
# Runs in the guest as `agent` on every provision; must stay idempotent.
set -euo pipefail

# The asbx test suite (sandbox/tests) needs pytest and PyYAML.
sudo apt-get install -y -qq python3-pytest python3-yaml >/dev/null

# Fresh-machine check: install from the clean checkout. A failure here fails
# the provision, which is the point.
/workspace/agent-plugins/install.sh
