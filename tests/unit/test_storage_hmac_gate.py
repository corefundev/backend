"""
C2 / #153 — HMAC-pickle gate tamper rejection (storage/backend._sign/_verify).

The gate is the anti-pickle-RCE defense: every artifact is HMAC-SHA256 signed
(SKUSIG1 envelope) and load refuses anything that doesn't verify. The happy
round-trip and the unsigned-in-signed-mode case are covered in test_training_smoke
— but that module imports the ML stack (lightgbm), so it only runs on Linux CI.
These lock in the NEGATIVE cases — the actual security invariant — in a
lightgbm-free unit so they run on EVERY unit pass, including the ones the prior
coverage missed (tampered payload / tampered signature / wrong key / truncated).
"""
from __future__ import annotations

import pytest

from src.storage import backend as b

_KEY  = "ab" * 32   # 64 hex chars → 32-byte key
_KEY2 = "cd" * 32


def _no_key(monkeypatch):
    # _signing_key() falls back to JWT_SECRET_KEY, so both must be cleared.
    monkeypatch.delenv("MODEL_SIGNING_KEY", raising=False)
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)


# ── happy path (locality + sanity) ──────────────────────────────────────────

def test_sign_verify_roundtrip(monkeypatch):
    monkeypatch.setenv("MODEL_SIGNING_KEY", _KEY)
    payload = b"\x80\x04 model-ish bytes"
    assert b._verify(b._sign(payload)) == payload


def test_signed_envelope_shape(monkeypatch):
    monkeypatch.setenv("MODEL_SIGNING_KEY", _KEY)
    blob = b._sign(b"x")
    assert blob.startswith(b._SIG_MAGIC)
    assert len(blob) == len(b._SIG_MAGIC) + b._SIG_LEN + 1   # magic + digest + 1-byte payload


# ── the security invariant: tamper / wrong-key / mode mismatches rejected ───

def test_tampered_payload_rejected(monkeypatch):
    monkeypatch.setenv("MODEL_SIGNING_KEY", _KEY)
    blob = bytearray(b._sign(b"trusted-model-bytes"))
    blob[-1] ^= 0x01     # flip a payload byte
    with pytest.raises(ValueError, match="HMAC mismatch"):
        b._verify(bytes(blob))


def test_tampered_signature_rejected(monkeypatch):
    monkeypatch.setenv("MODEL_SIGNING_KEY", _KEY)
    blob = bytearray(b._sign(b"payload"))
    blob[len(b._SIG_MAGIC)] ^= 0x01     # flip a signature byte
    with pytest.raises(ValueError, match="HMAC mismatch"):
        b._verify(bytes(blob))


def test_wrong_key_rejected(monkeypatch):
    monkeypatch.setenv("MODEL_SIGNING_KEY", _KEY)
    blob = b._sign(b"payload")
    monkeypatch.setenv("MODEL_SIGNING_KEY", _KEY2)   # verify with a different key
    with pytest.raises(ValueError, match="HMAC mismatch"):
        b._verify(blob)


def test_unsigned_blob_rejected_when_key_set(monkeypatch):
    monkeypatch.setenv("MODEL_SIGNING_KEY", _KEY)
    with pytest.raises(ValueError, match="unsigned"):
        b._verify(b"raw-unsigned-pickle-bytes")


def test_signed_blob_rejected_when_no_key(monkeypatch):
    monkeypatch.setenv("MODEL_SIGNING_KEY", _KEY)
    blob = b._sign(b"payload")
    _no_key(monkeypatch)
    with pytest.raises(ValueError, match="not configured"):
        b._verify(blob)


def test_truncated_signed_blob_rejected(monkeypatch):
    monkeypatch.setenv("MODEL_SIGNING_KEY", _KEY)
    blob = b._sign(b"payload")
    truncated = blob[: len(b._SIG_MAGIC) + 4]   # magic + 4 sig bytes, rest gone
    with pytest.raises(ValueError, match="HMAC mismatch"):
        b._verify(truncated)


# ── dev/legacy escape hatch: unsigned allowed only when no key is configured ─

def test_unsigned_blob_allowed_when_no_key(monkeypatch):
    _no_key(monkeypatch)
    payload = b"legacy-dev-pickle"
    assert b._verify(payload) == payload
