"""Guest-side network reachability probes.

These run entirely inside a live sandbox through its `Guest` and know nothing
about *why* the guest might be unreachable — no `ufw`/`nft`/`iptables`/
`firewalld` awareness, no host-side rule inspection, no root required. A dead
guest network looks the same from in here whether the cause is a missing
DHCP lease, a host firewall's default-deny FORWARD/routed policy, or a
misconfigured egress filter on the host, so `doctor` diagnoses the symptom (the
guest can't reach the network) rather than any one tool's rule set. Adding
support for a *host*-side firewall-specific check (e.g. reading `ufw`'s
rules to name the exact missing rule) is a separate, additive module: it
would live next to this one, not replace it.
"""
from __future__ import annotations

from .backend import Guest


def guest_has_ipv4_default_route(g: Guest) -> bool:
    """True when the guest has a live IPv4 default route. Reading the route
    table rather than a specific NIC's address is interface-name-agnostic
    (enp5s0 vs eth0 vs ens3) and implies the guest actually holds a DHCP
    lease with a gateway, not just a link-local address."""
    proc = g.exec(["ip", "-4", "route", "show", "default"], user="root", check=False)
    return proc.returncode == 0 and bool(proc.stdout.strip())


def guest_has_ipv4_egress(g: Guest, host: str = "api.github.com",
                          port: int = 443, timeout: float = 5.0) -> bool:
    """True when the guest completes a TCP handshake to `host:port` over
    IPv4 within `timeout` seconds. Uses bash's `/dev/tcp` pseudo-device
    rather than curl/wget, so it needs nothing beyond bash + coreutils
    (always present) and also exercises guest-side DNS resolution."""
    script = f"exec 3<>/dev/tcp/{host}/{port}"
    proc = g.exec(["timeout", str(int(timeout)), "bash", "-c", script], user="root", check=False)
    return proc.returncode == 0
