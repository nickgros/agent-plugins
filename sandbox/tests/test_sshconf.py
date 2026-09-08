"""Pure-logic tests for asbxlib.sshconf. No Incus required."""
from __future__ import annotations

from asbxlib import sshconf


# ---------------------------------------------------------------------------
# render_ssh_config_block
# ---------------------------------------------------------------------------

def test_render_ssh_config_block_matches_exact_format():
    rendered = sshconf.render_ssh_config_block(
        "sandbox-backend",
        "sandbox-backend.tail8b5d08.ts.net",
        "~/.config/agent-sandbox/id_ed25519",
        "~/.config/agent-sandbox/known_hosts",
        "dev",
    )
    expected = (
        "# managed by asbx — do not edit\n"
        "Host sandbox-backend\n"
        "    HostName sandbox-backend.tail8b5d08.ts.net\n"
        "    User dev\n"
        "    IdentityFile ~/.config/agent-sandbox/id_ed25519\n"
        "    IdentitiesOnly yes\n"
        "    StrictHostKeyChecking accept-new\n"
        "    UserKnownHostsFile ~/.config/agent-sandbox/known_hosts\n"
    )
    assert rendered == expected
