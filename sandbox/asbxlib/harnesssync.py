"""Host harness settings (omp config, rules, hooks, ...) pushed into sandboxes.

Sync is one way, host to guest, on every `asbx up` and `asbx settings-sync`.
The host is authoritative: a guest-side edit to a synced file is overwritten on
the next sync, and nothing is ever read back from the guest (docs/security.md,
"no channel back to the host"). Files that carry credentials are only synced
when listed under `harness_settings.credentials` or a manifest's
`harness_credentials`.
"""
from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

from . import config
from . import manifest
from . import ui
from .errors import AsbxError
from .incus import Incus

STATE_VERSION = 1
TMP_SUFFIX = ".asbx-tmp"
SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv"})
# Upper bound on the bytes of one batched exec. A single argument tops out at 128 KiB,
# and exec_in re-quotes the command into one `bash -c` script, so stay well under.
MAX_BATCH_BYTES = 24_000


@dataclass(frozen=True)
class SyncFile:
    host: Path
    guest: str  # absolute guest path
    mode: str


def guest_home(guest_user: str) -> str:
    return f"/home/{guest_user}"


def _roots(entry: Any, guest_user: str) -> list[tuple[Path, str, bool]]:
    """(host_root, guest_root, builtin) for one config entry. Guest paths are
    already normalized: validation resolves inline entries, and the registry's
    are literal."""
    home = guest_home(guest_user)
    if isinstance(entry, str):
        item = config.HARNESS_ITEMS[entry]
        return [(config.expand(f"{item.root}/{p}"), f"{home}/{item.root[2:]}/{p}", True)
                for p in item.paths]
    return [(config.expand(entry["host"]), f"{home}/{entry['guest'][2:]}", False)]


def _mode(st_mode: int, credential: bool) -> str:
    if credential:
        return "0600"
    return "0755" if st_mode & 0o111 else "0644"


def _walk(directory: Path, root_real: Path, guest_dir: str,
          ancestors: frozenset[Path] = frozenset()) -> Iterator[tuple[Path, str]]:
    """Regular files under `directory`. A symlink is followed only when its
    target stays inside the walked root: a link such as `skills/x -> ~/.ssh/id_ed25519`
    must not carry an unrelated host file into the guest."""
    real = directory.resolve()
    if real in ancestors:  # symlink loop
        return
    ancestors = ancestors | {real}
    with os.scandir(directory) as it:
        entries = sorted(it, key=lambda e: e.name)
    for entry in entries:
        path = Path(entry.path)
        if entry.is_symlink() and not path.exists():  # broken link
            continue
        if entry.is_symlink() and not path.resolve().is_relative_to(root_real):
            ui.warn(f"harness settings: skipping {path}: symlink points outside {root_real}")
            continue
        if path.is_dir():
            if entry.name not in SKIP_DIRS:
                yield from _walk(path, root_real, f"{guest_dir}/{entry.name}", ancestors)
        elif path.is_file():  # not a broken symlink, socket or FIFO
            yield path, f"{guest_dir}/{entry.name}"


def collect(entries: list, guest_user: str, credential: bool) -> list[SyncFile]:
    out: list[SyncFile] = []
    for entry in entries:
        for host_root, guest_root, builtin in _roots(entry, guest_user):
            if not host_root.exists():
                # Presets list optional files, so only an explicit entry is worth a warning.
                if not builtin:
                    ui.warn(f"harness settings: {host_root} not found on host, skipping")
                continue
            if host_root.is_file():
                pairs = [(host_root, guest_root)]
            elif host_root.is_dir():
                pairs = _walk(host_root, host_root.resolve(), guest_root)
            else:
                continue
            for path, guest in pairs:
                out.append(SyncFile(path, guest, _mode(path.stat().st_mode, credential)))
    return out


def files_for(cfg: dict, m: manifest.Manifest) -> list[SyncFile]:
    gu = cfg["guest_user"]
    merged: dict[str, SyncFile] = {}
    for f in collect(cfg["harness_settings"]["sync"], gu, False):
        merged[f.guest] = f
    for entry in m.harness_credentials:
        found = collect([entry], gu, True)
        if isinstance(entry, str) and not found:
            ui.warn(f"harness credentials {entry}: no files on host")
        for f in found:  # a credential copy of the same path wins
            merged[f.guest] = f
    return [merged[g] for g in sorted(merged)]


def rewrite_home(text: str, host_home: str, guest_home: str) -> str:
    """Host settings embed absolute host paths that are dead in the guest."""
    return re.sub(re.escape(host_home) + r"(?![\w.-])", lambda _m: guest_home, text)


def _read_state(incus: Incus, instance: str, guest_user: str, state_path: str) -> list[str]:
    proc = incus.exec_in(instance, ["cat", state_path], user=guest_user, check=False)
    if proc.returncode != 0:
        return []
    try:
        data = json.loads(proc.stdout or "")
    except ValueError:
        return []
    paths = data.get("paths") if isinstance(data, dict) else None
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        return []
    return paths


def _chunks(items: list[str]) -> Iterator[list[str]]:
    """Batches of `items` whose total size stays under MAX_BATCH_BYTES."""
    chunk: list[str] = []
    size = 0
    for item in items:
        n = len(item.encode()) + 1
        if chunk and size + n > MAX_BATCH_BYTES:
            yield chunk
            chunk, size = [], 0
        chunk.append(item)
        size += n
    if chunk:
        yield chunk


def _guest_state(incus: Incus, instance: str, guest_user: str, paths: list[str]) -> dict[str, tuple[str, str]]:
    """{path: (sha256, octal mode)} for those of `paths` that exist in the guest."""
    digests: dict[str, str] = {}
    modes: dict[str, str] = {}
    for chunk in _chunks(paths):
        proc = incus.exec_in(instance, ["sha256sum", *chunk], user=guest_user, check=False)
        for line in (proc.stdout or "").splitlines():
            digest, sep, path = line.partition("  ")
            if sep:
                digests[path] = digest
        proc = incus.exec_in(instance, ["stat", "-c", "%a %n", *chunk], user=guest_user, check=False)
        for line in (proc.stdout or "").splitlines():
            mode, sep, path = line.partition(" ")
            if sep:
                modes[path] = mode
    return {p: (digests[p], modes[p]) for p in digests if p in modes}


def push(incus: Incus, instance: str, guest_user: str, files: list[SyncFile]) -> int:
    """Brings the guest's copies of `files` in line with the host's, pushing
    only those whose content or mode differ, then removes files the previous
    sync pushed that are no longer in `files`. Returns how many were pushed.
    The guest is compared directly rather than trusting a recorded hash, so a
    guest-side edit is overwritten, as the host is authoritative."""
    uid = incus.uid_of(instance, guest_user)
    home = guest_home(guest_user)
    state_path = f"{home}/.asbx/harness-settings.json"
    previous = _read_state(incus, instance, guest_user, state_path)

    host_home = str(Path.home())
    payloads: dict[str, tuple[SyncFile, bytes, bool]] = {}  # guest path -> (file, bytes, is_text)
    for f in files:
        try:
            raw = f.host.read_bytes()
        except OSError as e:
            raise AsbxError(f"harness settings: cannot read {f.host}: {e}") from e
        try:
            data = rewrite_home(raw.decode("utf-8"), host_home, home).encode("utf-8")
            payloads[f.guest] = (f, data, True)
        except UnicodeDecodeError:
            payloads[f.guest] = (f, raw, False)

    in_guest = _guest_state(incus, instance, guest_user, list(payloads))
    changed = [(f, data, text) for f, data, text in payloads.values()
               if in_guest.get(f.guest) != (hashlib.sha256(data).hexdigest(), f"{int(f.mode, 8):o}")]

    dirs = sorted({posixpath.dirname(f.guest) for f, _d, _t in changed} | {f"{home}/.asbx"})
    for chunk in _chunks(dirs):
        incus.exec_in(instance, ["mkdir", "-p", *chunk], user=guest_user)

    moves = []
    for f, data, text in changed:
        tmp = f.guest + TMP_SUFFIX
        if text:
            incus.push_text(instance, tmp, data.decode("utf-8"), mode=f.mode, uid=uid, gid=uid)
        else:
            incus.file_push(instance, str(f.host), tmp, mode=f.mode, uid=uid, gid=uid)
        moves.append(f"mv -f {shlex.quote(tmp)} {shlex.quote(f.guest)}")
    # Renames are batched to stay under the argument-size limit; omp's config
    # watcher still never sees a half-written file.
    for chunk in _chunks(moves):
        incus.exec_in(instance, ["bash", "-c", " && ".join(chunk)], user=guest_user)

    # The state file is guest-writable, so only prune plain paths under the guest home.
    stale = sorted(p for p in set(previous) - set(payloads)
                   if p.startswith(home + "/") and posixpath.normpath(p) == p)
    for chunk in _chunks(stale):
        incus.exec_in(instance, ["rm", "-f", *chunk], user=guest_user)

    state = json.dumps({"version": STATE_VERSION, "paths": list(payloads)}) + "\n"
    incus.push_text(instance, state_path, state, mode="0644", uid=uid, gid=uid)
    return len(changed)


def sync_instance(incus: Incus, cfg: dict, m: manifest.Manifest) -> None:
    files = files_for(cfg, m)
    pushed = push(incus, m.instance, cfg["guest_user"], files)
    ui.ok(f"{m.instance}: harness settings ({len(files)} files, {pushed} updated)")


def cmd_settings_sync(incus: Incus, cfg: dict, group: Optional[str]) -> int:
    if group:
        targets = [manifest.load_manifest(group, cfg)]
    else:
        targets = []
        for g, _instance in manifest.manifest_groups(cfg):
            try:
                targets.append(manifest.load_manifest(g, cfg))
            except AsbxError as e:
                ui.warn(f"{g}: {e}")
    failed = False
    for target in targets:
        instance = target.instance
        state = incus.instance_state(instance) if incus.instance_exists(instance) else "absent"
        if state != "Running":
            ui.ok(f"{instance}: skipped ({state})")
            continue
        try:
            sync_instance(incus, cfg, target)
        except AsbxError as e:
            ui.warn(f"{instance}: {e}")
            failed = True
    return 1 if failed else 0
