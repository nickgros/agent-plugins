"""Tests for asbxlib.netcheck. No real Incus required — FakeIncus scripts exec."""
from __future__ import annotations

from fake_incus import FakeIncus

from asbxlib import netcheck


def test_guest_has_ipv4_default_route_true_when_route_table_nonempty():
    fake = FakeIncus()
    fake.stub("ip", "-4", "route", "show", "default", stdout="default via 10.210.86.1 dev enp5s0\n")

    assert netcheck.guest_has_ipv4_default_route(fake, "sandbox-demo") is True


def test_guest_has_ipv4_default_route_false_when_route_table_empty():
    fake = FakeIncus()
    fake.stub("ip", "-4", "route", "show", "default", stdout="")

    assert netcheck.guest_has_ipv4_default_route(fake, "sandbox-demo") is False


def test_guest_has_ipv4_default_route_false_when_command_fails():
    fake = FakeIncus()
    fake.stub("ip", "-4", "route", "show", "default", rc=1)

    assert netcheck.guest_has_ipv4_default_route(fake, "sandbox-demo") is False


def test_guest_has_ipv4_egress_true_when_handshake_succeeds():
    fake = FakeIncus()
    fake.stub("dev/tcp", rc=0)

    assert netcheck.guest_has_ipv4_egress(fake, "sandbox-demo") is True


def test_guest_has_ipv4_egress_false_when_handshake_times_out():
    fake = FakeIncus()
    fake.stub("dev/tcp", rc=124)  # coreutils `timeout`'s exit code on timeout

    assert netcheck.guest_has_ipv4_egress(fake, "sandbox-demo") is False


def test_guest_has_ipv4_egress_uses_requested_host_and_port():
    fake = FakeIncus()
    fake.stub("dev/tcp/example.com/8443", rc=0)

    assert netcheck.guest_has_ipv4_egress(fake, "sandbox-demo", host="example.com", port=8443) is True
