#!/bin/bash
# Installs asbx on this machine:
#   1. clones the repo to $ASBX_HOME (default ~/.local/share/asbx), or updates it;
#   2. makes sure python3 can import PyYAML;
#   3. on macOS, installs Tart (which brings Softnet) with Homebrew and, after
#      asking, installs Softnet root-owned and setuid (docs/host-setup.md#softnet);
#   4. links `asbx` into ~/.local/bin.
#
#   curl -fsSL https://raw.githubusercontent.com/nickgros/agent-plugins/main/sandbox/install.sh | bash
#
# Safe to rerun: it updates the clone, refreshes Softnet after a Homebrew
# upgrade, and replaces only a symlink at ~/.local/bin/asbx. sudo runs only
# after you confirm. On Linux it installs nothing system-wide; Incus setup
# stays manual (docs/host-setup.md).
set -euo pipefail

REPO_URL=${ASBX_REPO:-https://github.com/nickgros/agent-plugins.git}
ASBX_HOME=${ASBX_HOME:-$HOME/.local/share/asbx}
SOFTNET_DIR=/usr/local/libexec/asbx

say() { printf '==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# Reads the answer from the terminal: stdin is the script itself under
# `curl | bash`. No terminal (CI, a pipe) counts as no.
confirm() {
  local answer
  { printf '%s [y/N] ' "$1" >/dev/tty && read -r answer </dev/tty; } 2>/dev/null || return 1
  case "$answer" in [yY]|[yY][eE][sS]) return 0 ;; *) return 1 ;; esac
}

fetch_repo() {
  command -v git >/dev/null || die "git is required; install it (macOS: xcode-select --install; Debian/Ubuntu: sudo apt install git) and rerun"
  if [ -d "$ASBX_HOME/.git" ]; then
    local origin
    origin=$(git -C "$ASBX_HOME" remote get-url origin 2>/dev/null || true)
    [ "$origin" = "$REPO_URL" ] || die "$ASBX_HOME tracks '$origin', not $REPO_URL; set ASBX_HOME to another directory or fix its origin"
    say "updating $ASBX_HOME"
    git -C "$ASBX_HOME" pull --ff-only --quiet \
      || die "could not fast-forward $ASBX_HOME (local changes, or it diverged); fix it with git or move it aside"
  elif [ -e "$ASBX_HOME" ]; then
    die "$ASBX_HOME exists and is not a git checkout; move it aside or set ASBX_HOME"
  else
    say "cloning $REPO_URL to $ASBX_HOME"
    mkdir -p "$(dirname "$ASBX_HOME")"
    git clone --quiet --depth 1 "$REPO_URL" "$ASBX_HOME"
  fi
}

ensure_pyyaml() {
  command -v python3 >/dev/null || die "python3 is required; install it (macOS: xcode-select --install; Debian/Ubuntu: sudo apt install python3) and rerun"
  python3 -c 'import yaml' 2>/dev/null && return
  say "installing PyYAML for $(command -v python3)"
  if ! python3 -m pip install --user --quiet pyyaml; then
    die "could not install PyYAML with pip. Install it for this python3, e.g. 'sudo apt install python3-yaml' (Debian/Ubuntu) or 'brew install pyyaml' (Homebrew Python), then rerun"
  fi
}

# Softnet filters every sandbox's network and needs root, so the copy Tart
# runs must be root-owned and setuid in a root-owned directory. Never a sudo
# rule for the Homebrew copy: any process running as you could replace it.

# Asks asbx itself, so the installer accepts exactly what `asbx up` accepts.
softnet_safe() {
  python3 - "$ASBX_HOME/sandbox" "$SOFTNET_DIR" <<'EOF'
import sys
sys.path.insert(0, sys.argv[1])
from asbxlib.tartbackend import softnet_problem
sys.exit(1 if softnet_problem(sys.argv[2]) else 0)
EOF
}

install_softnet() {
  local src
  src="$(brew --prefix softnet)/bin/softnet"
  [ -x "$src" ] || die "softnet not found at $src; run 'brew install openai/tools/tart'"
  # A byte match too, so a rerun after `brew upgrade` refreshes the copy.
  if softnet_safe && cmp -s "$src" "$SOFTNET_DIR/softnet"; then
    say "softnet in $SOFTNET_DIR is up to date"
    return
  fi
  cat <<EOF
Softnet must be installed root-owned and setuid. This runs:
  sudo install -d -o root -g wheel -m 0755 $SOFTNET_DIR
  sudo install -o root -g wheel -m 4555 $src $SOFTNET_DIR/softnet
EOF
  if confirm "Run these with sudo now?"; then
    sudo install -d -o root -g wheel -m 0755 "$SOFTNET_DIR"
    sudo install -o root -g wheel -m 4555 "$src" "$SOFTNET_DIR/softnet"
    say "installed $SOFTNET_DIR/softnet"
  else
    say "skipped; run the two commands above before 'asbx up'"
  fi
}

macos_deps() {
  [ "$(uname -m)" = arm64 ] || die "asbx on macOS needs Apple silicon"
  local major
  major=$(sw_vers -productVersion | cut -d. -f1)
  [ "$major" -ge 26 ] || die "asbx on macOS needs macOS 26 or later (Softnet requires it); this is $(sw_vers -productVersion)"
  command -v brew >/dev/null || die "Homebrew is required: https://brew.sh"
  if brew list --formula tart >/dev/null 2>&1; then
    say "tart is installed"
  else
    say "installing Tart and Softnet with Homebrew"
    brew install openai/tools/tart
  fi
  install_softnet
}

link_asbx() {
  local target="$HOME/.local/bin/asbx"
  if [ -L "$target" ]; then
    "$ASBX_HOME/sandbox/asbx" install --force
  else
    "$ASBX_HOME/sandbox/asbx" install
  fi
}

main() {
  fetch_repo
  ensure_pyyaml
  case "$(uname -s)" in
    Darwin) macos_deps ;;
    Linux) say "Linux: install Incus and set up name resolution yourself: $ASBX_HOME/sandbox/docs/host-setup.md" ;;
    *) die "unsupported OS: $(uname -s)" ;;
  esac
  link_asbx
  cat <<EOF

asbx is installed. Next:
  asbx init          # config, SSH key, ~/.ssh/config Include
  asbx build-base    # bake the base image (several minutes)
  asbx doctor        # check what's still missing
Docs: $ASBX_HOME/sandbox/README.md
EOF
}

# Everything runs from here, so a download cut short runs nothing.
main "$@"
