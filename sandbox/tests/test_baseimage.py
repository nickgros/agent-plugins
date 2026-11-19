from __future__ import annotations

import subprocess
import urllib.error

import pytest

from asbxlib import baseimage
from asbxlib.errors import AsbxError
from asbxlib.incusbackend import IncusGuest
from fake_incus import FakeIncus

SHASUMS = (
    "deadbeefcafef00d0000000000000000000000000000000000000000000000  node-v20.11.0-linux-x64.tar.xz\n"
    "1111111111111111111111111111111111111111111111111111111111111a  node-v20.11.0-linux-arm64.tar.xz\n"
)


def test_resolve_node_downloads_uses_stubbed_index_and_shasums_for_both_arches():
    entries = [
        {"version": "v22.1.0", "lts": False},
        {"version": "v20.11.0", "lts": "Iron"},
    ]

    def fetch_text(url):
        assert url == "https://nodejs.org/dist/v20.11.0/SHASUMS256.txt"
        return SHASUMS

    downloads = baseimage.resolve_node_downloads(fetch_json=lambda: entries, fetch_text=fetch_text)
    base = "https://nodejs.org/dist/v20.11.0/node-v20.11.0-linux"
    assert downloads == {
        "x64": (f"{base}-x64.tar.xz", "deadbeefcafef00d0000000000000000000000000000000000000000000000"),
        "arm64": (f"{base}-arm64.tar.xz", "1111111111111111111111111111111111111111111111111111111111111a"),
    }


def _pinned() -> dict:
    return {arch: (f"https://nodejs.org/dist/{baseimage.NODE_PINNED_VERSION}/"
                   f"node-{baseimage.NODE_PINNED_VERSION}-linux-{arch}.tar.xz", sha)
            for arch, sha in baseimage.NODE_PINNED_SHA256.items()}


def test_resolve_node_downloads_falls_back_to_the_pins_on_url_error():
    def fetch_json():
        raise urllib.error.URLError("network unreachable")

    assert baseimage.resolve_node_downloads(fetch_json=fetch_json, fetch_text=lambda url: "") == _pinned()


def test_resolve_node_downloads_falls_back_when_one_tarball_sha_is_absent():
    downloads = baseimage.resolve_node_downloads(
        fetch_json=lambda: [{"version": "v20.11.0", "lts": "Iron"}],
        fetch_text=lambda url: "abc  node-v20.11.0-linux-arm64.tar.xz\n",
    )
    assert downloads == _pinned()


NODE = {"x64": ("https://example.invalid/node-x64.tar.xz", "aaa"),
        "arm64": ("https://example.invalid/node-arm64.tar.xz", "bbb")}


def _script() -> str:
    return baseimage.render_base_script("dev", NODE)


def test_render_base_script_picks_node_by_the_guest_dpkg_arch():
    script = _script()
    assert 'case "$(dpkg --print-architecture)" in' in script
    assert "amd64) node_url=https://example.invalid/node-x64.tar.xz; node_sha256=aaa ;;" in script
    assert "arm64) node_url=https://example.invalid/node-arm64.tar.xz; node_sha256=bbb ;;" in script
    assert 'curl -fsSLo /tmp/node.tar.xz "$node_url"' in script


def test_render_base_script_starts_in_a_cwd_the_guest_user_can_enter():
    assert _script().startswith("#!/bin/bash\nset -euo pipefail\n# The guest user's installers inherit this cwd; it must be one they can enter.\ncd /\n")


def test_render_base_script_installers_fail_the_recipe_on_a_failed_download():
    script = _script()
    assert "$(curl" not in script
    for line in script.splitlines():
        if "curl" in line and "|" in line and not line.startswith("echo"):
            assert "set -o pipefail;" in line, line
    assert "curl -fsSLo /tmp/mise-install.sh https://mise.run" in script


def test_render_base_script_is_valid_bash_that_stops_on_errors():
    script = _script()
    assert script.startswith("#!/bin/bash\nset -euo pipefail\n")
    subprocess.run(["bash", "-n"], input=script, text=True, check=True)


def test_render_base_script_installs_per_user_tools_as_the_guest_user():
    script = _script()
    assert "can1357/oh-my-pi/main/scripts/install.sh" in script
    assert "runuser -u dev -- env HOME=/home/dev bash -c 'set -o pipefail; curl -fsSL https://claude.ai/install.sh | bash'" in script
    assert "@anthropic-ai/claude-code" not in script
    assert "npm i -g @openai/codex @google/gemini-cli @getpaseo/cli" in script


def test_render_base_script_adds_the_guest_user_to_docker_after_docker_is_installed():
    script = _script()
    assert script.index("docker-ce") < script.index("usermod -aG sudo,users,docker dev")
    assert "dev ALL=(ALL) NOPASSWD:ALL" in script


def test_render_base_script_installs_and_enables_the_paseo_unit_verbatim():
    script = _script()
    unit = script.split("<<'ASBX_UNIT'\n", 1)[1].split("ASBX_UNIT\n", 1)[0]
    assert unit == baseimage.render_paseo_unit("dev")
    assert "runuser -u dev -- env HOME=/home/dev paseo daemon config set features.dictation.enabled false" in script
    assert "systemctl enable ssh asbx-paseo.service" in script


def test_render_base_script_picks_the_aws_cli_for_the_guest_arch_and_installs_zsh():
    script = _script()
    assert 'awscli-exe-linux-$(uname -m).zip' in script
    assert " zsh " in script


def test_render_base_script_turns_ssh_password_auth_off_everywhere():
    script = _script()
    assert "/etc/ssh/sshd_config.d/00-asbx.conf" in script
    assert "PasswordAuthentication[[:space:]]+yes" in script


ELF = b"\x7fELF\x02\x01\x01"


def test_copy_host_binaries_pushes_into_guest_local_bin_owned_by_guest(tmp_path):
    tool = tmp_path / "tool"
    tool.write_bytes(ELF)
    fake = FakeIncus()

    baseimage.copy_host_binaries(IncusGuest(fake, "vm"), "dev", [str(tool), str(tmp_path / "missing")])

    pushes = [c for c in fake.calls if c[:2] == ["file", "push"]]
    assert pushes == [[
        "file", "push", str(tool), "vm/home/dev/.local/bin/tool",
        "--mode", "0755", "--uid", "1000", "--gid", "1000",
    ]]
    mkdirs = [c for c in fake.calls if c[0] == "exec" and "mkdir -p /home/dev/.local/bin" in " ".join(c)]
    assert len(mkdirs) == 1
    assert "runuser" in " ".join(mkdirs[0])


def test_copy_host_binaries_skips_non_elf_files_with_a_warning(tmp_path, capsys):
    macho = tmp_path / "tool"
    macho.write_bytes(b"\xcf\xfa\xed\xfe")
    fake = FakeIncus()

    baseimage.copy_host_binaries(IncusGuest(fake, "vm"), "dev", [str(macho)])

    assert fake.calls == []
    assert "not a Linux (ELF) binary" in capsys.readouterr().err


def test_copy_host_binaries_does_nothing_when_no_binary_exists(tmp_path):
    fake = FakeIncus()

    baseimage.copy_host_binaries(IncusGuest(fake, "vm"), "dev", [str(tmp_path / "missing")])

    assert fake.calls == []


def test_prepare_build_vm_runs_the_recipe_then_copies_binaries_then_cleans_for_imaging(cfg, tmp_path):
    tool = tmp_path / "tool"
    tool.write_bytes(ELF)
    cfg["defaults"]["copy_binaries"] = [str(tool)]
    fake = FakeIncus()

    baseimage.prepare_build_vm(IncusGuest(fake, "vm"), cfg, "echo recipe\n")

    assert fake.pushed[baseimage.BASE_RECIPE_PATH] == "echo recipe\n"
    run = fake.calls.index(["exec", "vm", "-t", "--", "bash", baseimage.BASE_RECIPE_PATH])
    removed = fake.calls.index(["exec", "vm", "--", "rm", "-f", baseimage.BASE_RECIPE_PATH])
    copied = next(i for i, c in enumerate(fake.calls) if c[:3] == ["file", "push", str(tool)])
    clean = fake.calls.index(["exec", "vm", "--", "cloud-init", "clean", "--logs", "--machine-id"])
    host_keys = fake.calls.index(["exec", "vm", "--", "bash", "-c", "rm -f /etc/ssh/ssh_host_*"])
    assert run < removed < copied < clean < host_keys


def test_prepare_build_vm_stops_when_the_recipe_fails(cfg):
    fake = FakeIncus()
    fake.stub("exec", "-t", "bash", baseimage.BASE_RECIPE_PATH, rc=3)

    with pytest.raises(AsbxError, match=r"base recipe failed in vm \(exit 3\)"):
        baseimage.prepare_build_vm(IncusGuest(fake, "vm"), cfg, "false\n")

    assert not any("cloud-init" in c for c in fake.calls)
