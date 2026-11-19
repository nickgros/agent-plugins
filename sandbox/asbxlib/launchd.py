"""Per-user launchd jobs on macOS: render a job plist, write it, and load or
unload it in the user's GUI domain (`gui/<uid>`).

Used by the Tart backend (one job per running sandbox VM) and by the AWS
credential refresh job. `run_launchctl` is the only place that spawns
`launchctl`, so tests replace it and never touch the real launchd.
"""
from __future__ import annotations

import os
import plistlib
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

from . import host
from .errors import AsbxError

# How long `bootout` waits for launchd to drop a job before giving up. launchd
# can report bootout done while the service is still being torn down, and a
# bootstrap of the same label in that window fails with an I/O error.
BOOTOUT_POLLS = 40
BOOTOUT_POLL_INTERVAL = 0.25


def run_launchctl(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, check=False)


def domain() -> str:
    return f"gui/{os.getuid()}"


def launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def render_plist(label: str, program_args: list[str], *, env: Optional[dict[str, str]] = None,
                 start_interval: Optional[int] = None, watch_paths: Optional[list[str]] = None,
                 keep_alive_on_failure: bool = False, log_path: Optional[str] = None) -> bytes:
    """A job plist. Every job runs once when it is loaded (`RunAtLoad`).
    `keep_alive_on_failure` restarts the program only after a non-zero exit,
    so a clean stop stays stopped. stdout and stderr both go to `log_path`
    when set, and are discarded otherwise. Keys are sorted, so the same job
    always renders the same bytes and callers can compare renders."""
    job: dict = {"Label": label, "ProgramArguments": list(program_args), "RunAtLoad": True}
    if env:
        job["EnvironmentVariables"] = dict(env)
    if start_interval is not None:
        job["StartInterval"] = int(start_interval)
    if watch_paths:
        job["WatchPaths"] = list(watch_paths)
    if keep_alive_on_failure:
        job["KeepAlive"] = {"SuccessfulExit": False}
    if log_path:
        job["StandardOutPath"] = str(log_path)
        job["StandardErrorPath"] = str(log_path)
    return plistlib.dumps(job, sort_keys=True)


def write_plist(path: Path, plist_bytes: bytes) -> None:
    """Replaces `path` atomically, creating parent dirs, so launchd never reads
    a half-written job. Mode 0644: launchd refuses group- or world-writable
    job files."""
    host.write_atomic(path, plist_bytes, 0o644)


def install_agent(label: str, plist_bytes: bytes) -> Path:
    """Writes `~/Library/LaunchAgents/<label>.plist`, so launchd also loads the
    job at every login, and (re)loads it now. Returns the plist path."""
    path = launch_agents_dir() / f"{label}.plist"
    write_plist(path, plist_bytes)
    reload(label, path)
    return path


def agent_loaded(label: str) -> bool:
    return run_launchctl(["print", f"{domain()}/{label}"]).returncode == 0


# `launchctl print` lists `pid = <n>` only while the job's program runs.
_PID_RE = re.compile(r"^\s*pid = \d+\s*$", re.MULTILINE)


def job_running(label: str) -> bool:
    """Whether the job is loaded and its program is running now: a job
    stays loaded after its program exits when launchd doesn't restart it."""
    proc = run_launchctl(["print", f"{domain()}/{label}"])
    return proc.returncode == 0 and _PID_RE.search(proc.stdout or "") is not None


def bootstrap(path: Path) -> None:
    """Loads the job at `path` into the GUI domain. The plist can live
    anywhere; outside ~/Library/LaunchAgents it is not loaded at login."""
    proc = run_launchctl(["bootstrap", domain(), str(path)])
    if proc.returncode != 0:
        raise AsbxError(f"command failed: launchctl bootstrap {domain()} {path}\n{(proc.stderr or '').strip()}")


def bootout(label: str) -> None:
    """Unloads the job, stopping its program. A job that isn't loaded is not an error."""
    if not agent_loaded(label):
        return
    proc = run_launchctl(["bootout", f"{domain()}/{label}"])
    for _ in range(BOOTOUT_POLLS):
        if not agent_loaded(label):
            return
        time.sleep(BOOTOUT_POLL_INTERVAL)
    raise AsbxError(f"command failed: launchctl bootout {domain()}/{label}\n{(proc.stderr or '').strip()}")


def reload(label: str, path: Path) -> None:
    """Loads `path` fresh: launchd rejects a bootstrap of a label that is
    already loaded, so any loaded copy is unloaded first."""
    bootout(label)
    bootstrap(path)
