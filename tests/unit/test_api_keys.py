"""
Unit tests for src/auth/api_keys.py.
"""
from __future__ import annotations

import time

import pytest

from src.auth.api_keys import (
    KEY_PREFIX,
    generate_api_key,
    hash_api_key,
    is_well_formed,
    verify_api_key,
)


# ── Generation ───────────────────────────────────────────────────────────

def test_generate_returns_string_with_prefix():
    k = generate_api_key()
    assert isinstance(k, str)
    assert k.startswith(KEY_PREFIX)


def test_generate_is_unique():
    keys = {generate_api_key() for _ in range(50)}
    assert len(keys) == 50, "generated keys must be unique"


def test_generate_long_enough_to_be_unguessable():
    # urlsafe(32) → 43 chars + 4 prefix → ≥ 47
    k = generate_api_key()
    assert len(k) >= 45


def test_generate_no_whitespace():
    for _ in range(20):
        k = generate_api_key()
        assert not any(c.isspace() for c in k)


# ── is_well_formed ───────────────────────────────────────────────────────

@pytest.mark.parametrize("good", [
    generate_api_key(),
    generate_api_key(),
    "sku_" + "A" * 43,
])
def test_well_formed_accepts_real_keys(good):
    assert is_well_formed(good)


@pytest.mark.parametrize("bad", [
    "",
    "not-prefixed",
    "sku_",                      # empty body
    "sku_short",                 # body too short
    "sku_" + "A" * 30 + " spc",  # whitespace
    "sku_" + "A" * 30 + "/",     # invalid char (slash, not urlsafe)
    None,
    123,
    "SKU_" + "A" * 40,           # case sensitive prefix
])
def test_well_formed_rejects_garbage(bad):
    assert not is_well_formed(bad)


# ── Hash + verify roundtrip ──────────────────────────────────────────────

def test_hash_returns_bcrypt_format():
    h = hash_api_key("anything")
    # bcrypt output: $2b$<rounds>$<22 char salt><31 char hash>
    assert h.startswith("$2") and "$" in h[3:]
    assert len(h) >= 60


def test_verify_accepts_correct_key():
    k = generate_api_key()
    h = hash_api_key(k)
    assert verify_api_key(k, h) is True


def test_verify_rejects_wrong_key():
    k1 = generate_api_key()
    k2 = generate_api_key()
    h = hash_api_key(k1)
    assert verify_api_key(k2, h) is False


def test_verify_rejects_tampered_hash():
    k = generate_api_key()
    h = hash_api_key(k)
    # flip last char
    tampered = h[:-1] + ("A" if h[-1] != "A" else "B")
    assert verify_api_key(k, tampered) is False


def test_verify_rejects_empty_or_none():
    h = hash_api_key("real-key")
    assert verify_api_key("", h) is False
    assert verify_api_key(None, h) is False                 # type: ignore[arg-type]
    assert verify_api_key("real-key", "") is False
    assert verify_api_key("real-key", "not-a-bcrypt-hash") is False


def test_hash_empty_raises():
    with pytest.raises(ValueError):
        hash_api_key("")


# ── Constant-time-ish behavior on missing client ─────────────────────────

def test_verify_with_none_hash_returns_false_and_takes_time():
    """
    When the client_id doesn't exist (hash is None), verify must still
    consume time comparable to a real check. Otherwise the response time
    leaks "this client_id is unknown" vs "this client_id exists with
    wrong key", enabling enumeration.
    """
    real_hash = hash_api_key(generate_api_key())

    t0 = time.perf_counter()
    verify_api_key("anything", None)
    t_none = time.perf_counter() - t0

    t0 = time.perf_counter()
    verify_api_key("wrong-key", real_hash)
    t_wrong = time.perf_counter() - t0

    # Must be in the same order of magnitude. Loose bound — bcrypt is
    # noisy by design, so don't tighten this further.
    if t_wrong > 0.001:           # only meaningful when bcrypt is slow
        assert 0.1 * t_wrong < t_none < 10 * t_wrong, (
            f"timing leak risk: t_none={t_none:.3f} t_wrong={t_wrong:.3f}"
        )


# ── Different keys produce different hashes (salt sanity) ─────────────────

def test_same_key_hashed_twice_different_hashes():
    """bcrypt salts each run — hashes must differ even for same input."""
    k = generate_api_key()
    h1 = hash_api_key(k)
    h2 = hash_api_key(k)
    assert h1 != h2
    # but BOTH must verify against the original
    assert verify_api_key(k, h1)
    assert verify_api_key(k, h2)
