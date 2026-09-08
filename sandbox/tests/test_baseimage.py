from __future__ import annotations

import urllib.error

from asbxlib import baseimage


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
