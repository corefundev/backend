"""
Tests for the minimal anti-multi-account stack:
  • email canonicalization
  • disposable-domain blacklist
  • subnet rate-limit (logic only — Redis is mocked)
"""
from __future__ import annotations

import pytest

from src.auth.email_normalize import canonical_email
from src.auth.disposable_domains import (
    is_disposable, get_block_list, reset_cache_for_tests,
)
from src.auth.signup_rate_limit import subnet


# ── Email canonicalization ───────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    # Gmail: dots ignored, +alias stripped, alias domain collapsed
    ("Vasya@Gmail.com",                 "vasya@gmail.com"),
    ("vasya+spam@gmail.com",            "vasya@gmail.com"),
    ("v.a.s.y.a@gmail.com",             "vasya@gmail.com"),
    ("vasya@googlemail.com",            "vasya@gmail.com"),
    ("Vasya+Promo@GoogleMail.com",      "vasya@gmail.com"),
    # Yandex: domain alias collapse, dots NOT stripped, +alias trimmed
    ("vasya@yandex.com",                "vasya@yandex.ru"),
    ("vasya@ya.ru",                     "vasya@yandex.ru"),
    ("vasya+x@yandex.ru",               "vasya@yandex.ru"),
    ("vasya.r@yandex.ru",               "vasya.r@yandex.ru"),     # dot kept
    # Outlook family: domain NOT collapsed (different inboxes possible)
    ("vasya+x@outlook.com",             "vasya@outlook.com"),
    ("vasya+x@hotmail.com",             "vasya@hotmail.com"),
    # Generic: +alias stripped only
    ("vasya+marketing@acme.ru",         "vasya@acme.ru"),
    ("VASYA+x@ACME.RU",                 "vasya@acme.ru"),
    # ProtonMail / others: untouched (other than lowercasing)
    ("vasya@protonmail.com",            "vasya@protonmail.com"),
    ("vasya.r@protonmail.com",          "vasya.r@protonmail.com"),
    # Whitespace
    ("   vasya@gmail.com   ",           "vasya@gmail.com"),
])
def test_canonical(raw, expected):
    assert canonical_email(raw) == expected


def test_canonical_rejects_garbage():
    for bad in ["", "no-at-sign", None, 123]:
        with pytest.raises((ValueError, TypeError)):
            canonical_email(bad)


def test_canonical_idempotent():
    """Applying twice = applying once."""
    raw = "Vasya+x@GoogleMail.com"
    once = canonical_email(raw)
    twice = canonical_email(once)
    assert once == twice == "vasya@gmail.com"


# ── Disposable domains ───────────────────────────────────────────────────

def test_disposable_known_temp_services(tmp_path, monkeypatch):
    # Ensure we use the real bundled list
    reset_cache_for_tests()
    assert is_disposable("anyone@mailinator.com")
    assert is_disposable("hey@10minutemail.com")
    assert is_disposable("foo@yopmail.com")
    assert is_disposable("foo@guerrillamail.com")


def test_disposable_real_email_passes():
    reset_cache_for_tests()
    assert not is_disposable("vasya@gmail.com")
    assert not is_disposable("ceo@acme.ru")
    # ProtonMail must NOT be flagged — it's privacy, not disposable
    assert not is_disposable("user@protonmail.com")
    assert not is_disposable("user@tutanota.com")


def test_disposable_subdomain_blocked():
    """
    Some throwaway services hand out per-user subdomains, e.g.
    `random123.mailinator.com` — must still be blocked when the
    parent domain is in the list.
    """
    reset_cache_for_tests()
    assert is_disposable("hey@anything.mailinator.com")
    assert is_disposable("hey@deep.nested.mailinator.com")


def test_disposable_handles_garbage():
    reset_cache_for_tests()
    assert not is_disposable("")
    assert not is_disposable("no-at-sign")
    assert not is_disposable(None)            # type: ignore[arg-type]


def test_disposable_custom_file(tmp_path, monkeypatch):
    """User can ship their own block list via env var."""
    custom = tmp_path / "custom_block.txt"
    custom.write_text("# header\nbadexample.com\nspamhaus.test\n")
    monkeypatch.setenv("DISPOSABLE_DOMAINS_FILE", str(custom))
    reset_cache_for_tests()
    assert is_disposable("foo@badexample.com")
    assert is_disposable("foo@spamhaus.test")
    assert not is_disposable("foo@gmail.com")


# ── Subnet bucketing ─────────────────────────────────────────────────────

def test_subnet_ipv4_to_24():
    assert subnet("192.168.5.42")  == "192.168.5.0/24"
    assert subnet("10.0.0.1")      == "10.0.0.0/24"
    assert subnet("203.0.113.99")  == "203.0.113.0/24"


def test_subnet_ipv6_to_48():
    assert subnet("2001:db8:abcd:1::1") == "2001:db8:abcd::/48"


def test_subnet_garbage_passthrough():
    """Bad input shouldn't crash — return as-is so caller logs but passes."""
    assert subnet("not-an-ip") == "not-an-ip"


# ── Integration: canonical-vs-display split ───────────────────────────

def test_signup_uniqueness_via_canonical():
    """
    Two raw emails that canonicalize to the same value MUST be
    treated as duplicates by registry.get_by_email.
    """
    from src.clients.registry import LocalFileRegistry, ClientRecord
    import tempfile

    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        f.write("{}")
        registry = LocalFileRegistry(path=f.name)

    rec = ClientRecord(
        client_id="vasya-shop",
        config={},
        storage_path="/x",
        email="Vasya@gmail.com",
        email_canonical="vasya@gmail.com",
    )
    registry.register(rec)

    # Same canonical from a different raw form
    found = registry.get_by_email("vasya+spam@gmail.com")
    assert found is not None
    assert found.client_id == "vasya-shop"

    found2 = registry.get_by_email("v.a.s.y.a@googlemail.com")
    assert found2 is not None
    assert found2.client_id == "vasya-shop"

    # Different canonical → not found
    assert registry.get_by_email("someone@yandex.ru") is None
