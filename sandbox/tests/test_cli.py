from __future__ import annotations

import os
import socket
import threading

import pytest

from asbxlib import cli, host
from asbxlib.errors import AsbxError
from asbxlib.incusbackend import IncusBackend
from asbxlib.tartbackend import TartBackend

from fake_tart import FakeTart


def test_main_list_with_no_config_returns_1_and_reports_no_config(config_root, capsys):
    rc = cli.main(["list"])

    assert rc == 1
    assert "no config at" in capsys.readouterr().err


def test_settings_sync_command_is_registered_with_an_optional_group():
    parser = cli.build_parser()

    assert parser.parse_args(["settings-sync"]).group is None
    assert parser.parse_args(["settings-sync", "demo"]).group == "demo"


def test_ssh_config_takes_exactly_one_of_jump_or_direct(capsys):
    parser = cli.build_parser()

    assert parser.parse_args(["ssh-config", "demo", "--direct"]).direct is True
    for argv in (["ssh-config", "demo"], ["ssh-config", "demo", "--direct", "--jump", "me@host"]):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)


def test_settings_sync_unknown_group_reports_missing_manifest(config_root, capsys):
    (config_root.parent / "config.yaml").write_text("backend: incus\n")

    rc = cli.main(["settings-sync", "nope"])

    assert rc == 1
    assert "no manifest at" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# make_backend
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("platform, cfg, expected", [
    ("linux", None, IncusBackend),
    ("darwin", None, TartBackend),
    ("linux", {"backend": "auto"}, IncusBackend),
    ("darwin", {"backend": "auto"}, TartBackend),
    ("darwin", {"backend": "incus"}, IncusBackend),
    ("linux", {"backend": "tart"}, TartBackend),
])
def test_make_backend_resolves_auto_by_platform_and_honours_an_explicit_choice(
        monkeypatch, platform, cfg, expected):
    monkeypatch.setattr(cli.sys, "platform", platform)

    assert isinstance(cli.make_backend(cfg), expected)


def test_make_backend_auto_refuses_an_unsupported_platform(monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "freebsd14")

    with pytest.raises(AsbxError, match="freebsd14"):
        cli.make_backend({"backend": "auto"})


def test_install_needs_no_backend_on_an_unsupported_platform(config_root, monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "freebsd14")
    forced = []
    monkeypatch.setattr(cli.hostsetup, "cmd_install", forced.append)

    assert cli.main(["install", "--force"]) == 0
    assert forced == [True]


def test_doctor_reports_an_unsupported_platform_instead_of_erroring(config_root, monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "platform", "freebsd14")

    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr()
    assert "FAIL no sandbox backend for platform freebsd14" in out.out
    assert out.err == ""


# ---------------------------------------------------------------------------
# Incus-only commands
# ---------------------------------------------------------------------------

def test_ingress_commands_are_refused_on_the_tart_backend(config_root, monkeypatch, capsys):
    (config_root.parent / "config.yaml").write_text("backend: tart\n")
    monkeypatch.setattr(cli, "make_backend", lambda cfg: TartBackend(FakeTart().tart()))

    assert cli.main(["client-config", "laptop"]) == 1
    assert "'asbx client-config' is not supported on the tart backend" in capsys.readouterr().err
    assert cli.main(["ingress", "setup", "--print"]) == 1
    assert "'asbx ingress setup' is not supported on the tart backend" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# ssh-proxy
# ---------------------------------------------------------------------------

def test_ssh_proxy_refuses_a_sandbox_with_no_address(config_root, monkeypatch, capsys):
    (config_root.parent / "config.yaml").write_text("backend: tart\n")
    fake = FakeTart()
    fake.stub("ip", "sandbox-x", rc=1)
    monkeypatch.setattr(cli, "make_backend", lambda cfg: TartBackend(fake.tart()))

    assert cli.main(["ssh-proxy", "sandbox-x"]) == 1
    assert "sandbox-x has no address yet" in capsys.readouterr().err
    assert fake.calls == [["ip", "sandbox-x", "--resolver", "dhcp"]]


def test_relay_tcp_copies_both_ways_and_passes_on_the_half_close():
    server = socket.create_server(("127.0.0.1", 0))
    port = server.getsockname()[1]
    received = []

    def echo_once():
        conn, _ = server.accept()
        with conn:
            data = b""
            while chunk := conn.recv(65536):   # ends only when the relay shuts down its write side
                data += chunk
            received.append(data)
            conn.sendall(data[::-1])

    thread = threading.Thread(target=echo_once, daemon=True)
    thread.start()
    in_r, in_w = os.pipe()
    out_r, out_w = os.pipe()
    os.write(in_w, b"ping")
    os.close(in_w)

    host.relay_tcp("127.0.0.1", port, stdin_fd=in_r, stdout_fd=out_w)

    os.close(out_w)
    with os.fdopen(out_r, "rb") as out:
        assert out.read() == b"gnip"
    thread.join(timeout=5)
    assert received == [b"ping"]
    os.close(in_r)
    server.close()


def test_relay_tcp_reports_an_unreachable_port():
    with socket.create_server(("127.0.0.1", 0)) as s:
        port = s.getsockname()[1]
    with pytest.raises(AsbxError, match=f"cannot connect to 127.0.0.1:{port}"):
        host.relay_tcp("127.0.0.1", port)


def _serve_once(handler) -> int:
    server = socket.create_server(("127.0.0.1", 0))

    def run():
        conn, _ = server.accept()
        with conn:
            handler(conn)
        server.close()

    threading.Thread(target=run, daemon=True).start()
    return server.getsockname()[1]


def test_relay_tcp_ends_quietly_on_a_connection_reset():
    def reset(conn):
        conn.recv(1)
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")

    port = _serve_once(reset)
    in_r, in_w = os.pipe()
    out_r, out_w = os.pipe()
    os.write(in_w, b"x")

    host.relay_tcp("127.0.0.1", port, stdin_fd=in_r, stdout_fd=out_w)

    for fd in (in_r, in_w, out_r, out_w):
        os.close(fd)


def test_relay_tcp_ends_quietly_when_stdout_is_closed():
    port = _serve_once(lambda conn: conn.sendall(b"hello"))
    in_r, in_w = os.pipe()
    out_r, out_w = os.pipe()
    os.close(in_w)
    os.close(out_r)

    host.relay_tcp("127.0.0.1", port, stdin_fd=in_r, stdout_fd=out_w)

    os.close(in_r)
    os.close(out_w)


def test_relay_tcp_retries_a_short_write(monkeypatch):
    port = _serve_once(lambda conn: conn.sendall(b"hello"))
    in_r, in_w = os.pipe()
    out_r, out_w = os.pipe()
    os.close(in_w)
    real_write = os.write
    monkeypatch.setattr(host.os, "write", lambda fd, data: real_write(fd, bytes(data[:1])))

    host.relay_tcp("127.0.0.1", port, stdin_fd=in_r, stdout_fd=out_w)

    os.close(out_w)
    with os.fdopen(out_r, "rb") as out:
        assert out.read() == b"hello"
    os.close(in_r)