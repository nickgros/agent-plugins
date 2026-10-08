from __future__ import annotations

import urllib.error

from asbxlib import baseimage
from fake_incus import FakeIncus


def test_resolve_node_download_uses_stubbed_index_and_shasums():
    entries = [
        {"version": "v22.1.0", "lts": False},
        {"version": "v20.11.0", "lts": "Iron"},
    ]
    shasums_text = (
        "deadbeefcafef00d0000000000000000000000000000000000000000000000  node-v20.11.0-linux-x64.tar.xz\n"
        "1111111111111111111111111111111111111111111111111111111111111a  node-v20.11.0-linux-arm64.tar.xz\n"
    )

    def fetch_json():
        return entries

    def fetch_text(url):
        assert url == "https://nodejs.org/dist/v20.11.0/SHASUMS256.txt"
        return shasums_text

    url, sha256 = baseimage.resolve_node_download(fetch_json=fetch_json, fetch_text=fetch_text)
    assert url == "https://nodejs.org/dist/v20.11.0/node-v20.11.0-linux-x64.tar.xz"
    assert sha256 == "deadbeefcafef00d0000000000000000000000000000000000000000000000"


def test_resolve_node_download_falls_back_on_url_error():
    def fetch_json():
        raise urllib.error.URLError("network unreachable")

    url, sha256 = baseimage.resolve_node_download(fetch_json=fetch_json, fetch_text=lambda url: "")
    assert baseimage.NODE_PINNED_VERSION in url
    assert sha256 == baseimage.NODE_PINNED_SHA256


def test_resolve_node_download_falls_back_when_tarball_sha_is_absent():
    def fetch_json():
        return [{"version": "v20.11.0", "lts": "Iron"}]

    url, sha256 = baseimage.resolve_node_download(
        fetch_json=fetch_json, fetch_text=lambda url: "abc  node-v20.11.0-linux-arm64.tar.xz\n"
    )
    assert baseimage.NODE_PINNED_VERSION in url
    assert sha256 == baseimage.NODE_PINNED_SHA256


def test_render_base_cloud_init_installs_omp_for_guest_user_and_puts_it_on_path():
    rendered = baseimage.render_base_cloud_init("dev", "https://example.invalid/node.tar.xz", "abc")
    assert "runuser -u dev -- env HOME=/home/dev sh -c" in rendered
    assert "can1357/oh-my-pi/main/scripts/install.sh" in rendered
    path_line = next(line for line in rendered.splitlines() if "/etc/environment" in line)
    assert "PATH=\"/home/dev/.local/bin:" in path_line


def test_copy_host_binaries_pushes_into_guest_local_bin_owned_by_guest(tmp_path):
    tool = tmp_path / "tool"
    tool.write_text("#!/bin/sh\n")
    fake = FakeIncus()

    baseimage.copy_host_binaries(fake, "vm", "dev", [str(tool), str(tmp_path / "missing")])

    pushes = [c for c in fake.calls if c[:2] == ["file", "push"]]
    assert pushes == [[
        "file", "push", str(tool), "vm/home/dev/.local/bin/tool",
        "--mode", "0755", "--uid", "1000", "--gid", "1000",
    ]]
    mkdirs = [c for c in fake.calls if c[0] == "exec" and "mkdir -p /home/dev/.local/bin" in " ".join(c)]
    assert len(mkdirs) == 1
    assert "runuser" in " ".join(mkdirs[0])


def test_copy_host_binaries_does_nothing_when_no_binary_exists(tmp_path):
    fake = FakeIncus()

    baseimage.copy_host_binaries(fake, "vm", "dev", [str(tmp_path / "missing")])

    assert fake.calls == []
