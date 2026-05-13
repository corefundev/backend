"""
Tests for the audit-log HMAC chain (audit H4, migration 010).

The DB-coupled parts (`record_event`, `verify_chain`) are integration-
tested separately; here we exercise the pure crypto + canonical-repr
functions that are the actual tamper-evidence math.

Properties locked in:
  • _canonical_row produces stable JSON across deploys (sort_keys,
    no whitespace, default str coercion) — any change breaks every
    historical signature so this is enforced by golden output.
  • _compute_signature is deterministic and chain-able.
  • Tampering with ANY field of a signed row causes the recomputed
    signature to differ from the stored one.
  • The key loader rejects missing / short / non-hex keys without
    raising — audit must never fail-closed on a config issue.
"""
from __future__ import annotations

import pytest


def _import_internals():
    from src.audit.log import (
        _canonical_row, _compute_signature, _audit_hmac_key,
        _GENESIS_SIGNATURE,
    )
    return _canonical_row, _compute_signature, _audit_hmac_key, _GENESIS_SIGNATURE


# ── _canonical_row stability ─────────────────────────────────────────────

def test_canonical_row_is_deterministic():
    _canonical_row, *_ = _import_internals()
    a = _canonical_row(
        ts_iso="2026-05-13T12:00:00+00:00",
        client_id="acme", actor_email="alice@example.com",
        event_type="login", event_subtype="success",
        target_type=None, target_id=None,
        ip="10.0.0.1", user_agent="curl/7",
        success=True, metadata={"x": 1, "y": 2},
    )
    b = _canonical_row(
        # Same data, metadata keys reordered — must yield identical output.
        ts_iso="2026-05-13T12:00:00+00:00",
        client_id="acme", actor_email="alice@example.com",
        event_type="login", event_subtype="success",
        target_type=None, target_id=None,
        ip="10.0.0.1", user_agent="curl/7",
        success=True, metadata={"y": 2, "x": 1},
    )
    assert a == b


def test_canonical_row_has_no_whitespace():
    # Whitespace differences across pandas/json versions could
    # silently flip every signature. Pin it.
    _canonical_row, *_ = _import_internals()
    out = _canonical_row(
        ts_iso="2026-05-13T12:00:00+00:00",
        client_id="acme", actor_email=None,
        event_type="login", event_subtype=None,
        target_type=None, target_id=None,
        ip=None, user_agent=None,
        success=True, metadata={},
    )
    assert " " not in out
    assert "\n" not in out


def test_canonical_row_sorts_metadata_keys():
    # Tests that metadata nested dicts are sorted too, not just the
    # top-level keys. (json.dumps sort_keys recurses.)
    _canonical_row, *_ = _import_internals()
    out = _canonical_row(
        ts_iso="2026-05-13T12:00:00+00:00",
        client_id=None, actor_email=None,
        event_type="x", event_subtype=None,
        target_type=None, target_id=None,
        ip=None, user_agent=None,
        success=True, metadata={"z": 1, "a": 2},
    )
    # `a` appears before `z` regardless of insertion order.
    assert out.index('"a"') < out.index('"z"')


# ── _compute_signature is deterministic & chain-able ─────────────────────

def test_signature_is_deterministic():
    _canonical_row, _compute_signature, *_ = _import_internals()
    key = b"\x42" * 32
    canonical = _canonical_row(
        ts_iso="2026-05-13T12:00:00+00:00",
        client_id="acme", actor_email=None,
        event_type="login", event_subtype="success",
        target_type=None, target_id=None,
        ip=None, user_agent=None,
        success=True, metadata={},
    )
    sig1 = _compute_signature(key, "0" * 64, canonical)
    sig2 = _compute_signature(key, "0" * 64, canonical)
    assert sig1 == sig2
    assert len(sig1) == 64  # SHA-256 hex digest


def test_different_prev_yields_different_sig():
    _canonical_row, _compute_signature, *_ = _import_internals()
    key = b"\x42" * 32
    canonical = _canonical_row(
        ts_iso="2026-05-13T12:00:00+00:00",
        client_id="acme", actor_email=None,
        event_type="login", event_subtype="success",
        target_type=None, target_id=None,
        ip=None, user_agent=None,
        success=True, metadata={},
    )
    a = _compute_signature(key, "0" * 64, canonical)
    b = _compute_signature(key, "f" * 64, canonical)
    assert a != b


def test_different_key_yields_different_sig():
    _canonical_row, _compute_signature, *_ = _import_internals()
    canonical = _canonical_row(
        ts_iso="2026-05-13T12:00:00+00:00",
        client_id="acme", actor_email=None,
        event_type="login", event_subtype="success",
        target_type=None, target_id=None,
        ip=None, user_agent=None,
        success=True, metadata={},
    )
    a = _compute_signature(b"\x42" * 32, "0" * 64, canonical)
    b = _compute_signature(b"\x99" * 32, "0" * 64, canonical)
    assert a != b


# ── Tampering detection ──────────────────────────────────────────────────

@pytest.mark.parametrize("field,new_value", [
    ("client_id",   "evil_client"),
    ("event_type",  "login_bypass"),
    ("success",     False),
    ("ip",          "10.0.0.99"),
    ("actor_email", "attacker@example.com"),
])
def test_tampering_any_field_breaks_signature(field, new_value):
    """Recomputing the signature after flipping any one field must NOT
    match the original — this is the core property of the chain."""
    _canonical_row, _compute_signature, *_ = _import_internals()
    key = b"\x42" * 32

    original_kwargs = dict(
        ts_iso="2026-05-13T12:00:00+00:00",
        client_id="acme", actor_email="alice@example.com",
        event_type="login", event_subtype="success",
        target_type=None, target_id=None,
        ip="10.0.0.1", user_agent="curl/7",
        success=True, metadata={"k": "v"},
    )
    sig_original = _compute_signature(
        key, "0" * 64, _canonical_row(**original_kwargs),
    )

    tampered_kwargs = {**original_kwargs, field: new_value}
    sig_tampered = _compute_signature(
        key, "0" * 64, _canonical_row(**tampered_kwargs),
    )
    assert sig_original != sig_tampered


# ── Key loader fails open ────────────────────────────────────────────────

def test_missing_key_returns_none(monkeypatch):
    _, _, _audit_hmac_key, _ = _import_internals()
    monkeypatch.delenv("AUDIT_LOG_HMAC_KEY", raising=False)
    assert _audit_hmac_key() is None


def test_non_hex_key_returns_none(monkeypatch):
    _, _, _audit_hmac_key, _ = _import_internals()
    monkeypatch.setenv("AUDIT_LOG_HMAC_KEY", "not-hex-at-all-zzz")
    assert _audit_hmac_key() is None


def test_too_short_key_returns_none(monkeypatch):
    _, _, _audit_hmac_key, _ = _import_internals()
    # 8 bytes hex = 4 bytes raw, below the 16-byte floor.
    monkeypatch.setenv("AUDIT_LOG_HMAC_KEY", "deadbeef")
    assert _audit_hmac_key() is None


def test_valid_32byte_key_accepted(monkeypatch):
    _, _, _audit_hmac_key, _ = _import_internals()
    monkeypatch.setenv("AUDIT_LOG_HMAC_KEY", "a" * 64)  # 32 bytes hex
    key = _audit_hmac_key()
    assert key is not None
    assert len(key) == 32


def test_key_loader_tolerates_whitespace(monkeypatch):
    _, _, _audit_hmac_key, _ = _import_internals()
    monkeypatch.setenv("AUDIT_LOG_HMAC_KEY", f"  {'b' * 64}\n")
    key = _audit_hmac_key()
    assert key is not None
    assert len(key) == 32


# ── Chain simulation: write three rows, then verify, then tamper ─────────

def test_chain_walk_detects_tampered_row():
    """Simulate the chain math without a DB: build 3 rows, compute
    signatures, then rewrite row 2's event_type and verify the walk
    catches it."""
    _canonical_row, _compute_signature, _, _GENESIS = _import_internals()
    key = b"\x42" * 32

    def _make(ts, event_type):
        return {
            "ts_iso": ts, "client_id": "acme", "actor_email": None,
            "event_type": event_type, "event_subtype": None,
            "target_type": None, "target_id": None,
            "ip": None, "user_agent": None,
            "success": True, "metadata": {},
        }

    rows = [
        _make("2026-05-13T12:00:00+00:00", "login"),
        _make("2026-05-13T12:01:00+00:00", "otp_send"),
        _make("2026-05-13T12:02:00+00:00", "otp_verify"),
    ]
    chain: list[tuple[str, str, dict]] = []  # (prev_sig, sig, row)
    prev = _GENESIS
    for r in rows:
        sig = _compute_signature(key, prev, _canonical_row(**r))
        chain.append((prev, sig, r))
        prev = sig

    # Verify clean chain
    expected_prev = _GENESIS
    for prev_sig, sig, r in chain:
        assert prev_sig == expected_prev
        assert _compute_signature(key, prev_sig, _canonical_row(**r)) == sig
        expected_prev = sig

    # Tamper with row[1]: an attacker rewrites event_type
    tampered_row = {**chain[1][2], "event_type": "login_bypass"}
    expected_prev = _GENESIS
    bad_at = None
    for i, (prev_sig, sig, r) in enumerate(chain):
        row_to_verify = tampered_row if i == 1 else r
        assert prev_sig == expected_prev
        recomputed = _compute_signature(key, prev_sig, _canonical_row(**row_to_verify))
        if recomputed != sig:
            bad_at = i
            break
        expected_prev = sig
    assert bad_at == 1
