#!/bin/bash
# Set up this machine from the repo: skills, omp settings, sandbox projects.
# Safe to rerun. Never replaces a real file; only creates or refreshes symlinks.
set -euo pipefail

repo=$(cd "$(dirname "$0")" && pwd)

link() {
  local src=$1 dest=$2
  mkdir -p "$(dirname "$dest")"
  if [ -L "$dest" ]; then
    ln -sfn "$src" "$dest"
  elif [ -e "$dest" ]; then
    echo "skip: $dest exists and is not a symlink; move it aside to link $src" >&2
    return
  else
    ln -s "$src" "$dest"
  fi
  echo "linked: $dest -> $src"
}

# Skills go to ~/.agents/skills (the skills CLI's universal directory, read by
# omp, Codex and the sandbox mount). Sources install in order, this repo last.
while IFS= read -r source; do
  case $source in ''|'#'*) continue ;; esac
  pnpx skills@latest add "$source" -g -s '*' -a codex -y
done < "$repo/skill-sources.txt"
pnpx skills@latest add "$repo" -g -s '*' -a codex -y

link "$repo/omp/AGENTS.md" "$HOME/.omp/agent/AGENTS.md"
link "$repo/omp/agents" "$HOME/.omp/agent/agents"

for f in "$repo"/sandbox-projects/*.yaml "$repo"/sandbox-projects/*.sh; do
  link "$f" "$HOME/.config/agent-sandbox/projects/$(basename "$f")"
done
