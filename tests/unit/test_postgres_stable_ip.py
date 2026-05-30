"""
tests/unit/test_postgres_stable_ip.py

R10 fix 2026-05-30 — eliminates the pgbouncer c-ares DNS-wedge root.

pgbouncer's resolver wedges when postgres is recreated with a NEW IP
under a running pgbouncer (persistent "DNS lookup failed: postgres:
result=0", non-self-healing). PR #50 mitigated the CD path
(`--wait postgres` + health-gate); this is the COMPLETE root fix:
pin postgres to a static IP that survives recreate, so pgbouncer's
cached resolution stays valid on EVERY path (CD / manual / crash /
reboot).

The tests pin the invariants the wedge-elimination relies on:
  • the default network is declared explicitly with an IPAM config
    (a network with no IPAM = dynamic IP that changes on recreate),
  • postgres has an ipv4_address,
  • the static IP is OUTSIDE the dynamic `ip_range` so no dynamic
    container can take .0.10 before postgres is created (the IPAM
    collision risk that makes a "plain static IP" naive),
  • subnet/gateway match the current VPS topology (a drift would
    force an unintended network recreate / connectivity break).
"""
from __future__ import annotations

import ipaddress
from pathlib import Path

import yaml


_BACKEND = Path(__file__).resolve().parents[2]
_COMPOSE = _BACKEND / "docker" / "docker-compose.yml"


def _compose() -> dict:
    return yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))


def test_default_network_declared_with_ipam():
    """An implicit (auto-created) network has no IPAM control — Docker
    assigns from the whole subnet, and postgres's IP changes on
    recreate. The default network must be DECLARED with explicit IPAM
    so we can pin statics."""
    d = _compose()
    n = d.get("networks", {}).get("default")
    assert n, "compose must declare a top-level `networks: default:` block"
    ipam = n.get("ipam", {}).get("config")
    assert ipam and isinstance(ipam, list) and len(ipam) == 1, (
        "default network must have an explicit ipam.config entry"
    )
    cfg = ipam[0]
    assert "subnet" in cfg and "ip_range" in cfg and "gateway" in cfg, (
        "ipam.config must declare subnet + ip_range + gateway (ip_range is "
        "what reserves the static half from the dynamic pool — see test below)"
    )


def test_postgres_has_static_ipv4_address():
    """The whole point. Without ipv4_address postgres gets a dynamic IP
    that changes on recreate → pgbouncer's resolver wedges."""
    d = _compose()
    addr = (
        d["services"]["postgres"]
         .get("networks", {})
         .get("default", {})
         .get("ipv4_address")
    )
    assert addr, (
        "postgres must pin `networks: default: ipv4_address: …` — "
        "without it, recreate gives a new IP and pgbouncer wedges"
    )
    # Sanity: must be a valid IPv4 in the subnet.
    ipaddress.IPv4Address(addr)


def test_postgres_static_ip_outside_dynamic_ip_range():
    """A bare static IP without an `ip_range` carve-out is naive: Docker
    allocates dynamic IPs sequentially from the subnet's start, and if
    enough containers come up before postgres, a dynamic one could grab
    .0.10 — postgres create then fails "address already in use". The
    enterprise pattern: declare an `ip_range` that excludes the static
    addresses, so dynamic allocation NEVER touches them."""
    d = _compose()
    cfg = d["networks"]["default"]["ipam"]["config"][0]
    subnet = ipaddress.IPv4Network(cfg["subnet"])
    ip_range = ipaddress.IPv4Network(cfg["ip_range"])
    static = ipaddress.IPv4Address(
        d["services"]["postgres"]["networks"]["default"]["ipv4_address"]
    )
    # The static is in the subnet…
    assert static in subnet, (
        f"postgres static IP {static} must be inside subnet {subnet}"
    )
    # …but NOT in the dynamic ip_range — that's what prevents collision.
    assert static not in ip_range, (
        f"postgres static IP {static} must be OUTSIDE the dynamic "
        f"ip_range {ip_range} — otherwise a dynamic container could grab "
        f"the address before postgres is created (race-condition fail)"
    )
    # And the ip_range must be a proper subset of the subnet (catches a
    # bad copy-paste where ip_range ≠ inside subnet).
    assert ip_range.subnet_of(subnet), (
        f"ip_range {ip_range} must be inside subnet {subnet}"
    )


def test_subnet_gateway_match_current_vps_topology():
    """Both VPS already use 172.18.0.0/16 / gw 172.18.0.1 (verified on
    staging + prod 2026-05-30). Keeping the subnet/gateway IDENTICAL is
    what makes the migration low-risk: only the IPAM strategy changes
    (no IP renumbering for the other containers — they still come from
    the same /16). Drift here would force an unintended renumber + a
    connectivity break."""
    d = _compose()
    cfg = d["networks"]["default"]["ipam"]["config"][0]
    assert cfg["subnet"] == "172.18.0.0/16", (
        "subnet must stay 172.18.0.0/16 to match what Docker already "
        "auto-assigned on both VPS (no unintended renumber on migration)"
    )
    assert cfg["gateway"] == "172.18.0.1", (
        "gateway must stay 172.18.0.1 to match the existing default-net "
        "gateway on both VPS"
    )
