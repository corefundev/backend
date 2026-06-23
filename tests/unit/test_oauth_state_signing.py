"""
R13 LOW — OAuth state signing-key length floor.

`oauth/state._signing_key` reuses MODEL_SIGNING_KEY / JWT_SECRET_KEY. Those are
floored at 32 bytes in prod startup (R4-12), but the OAuth path had no floor of
its own; these lock in a defensive ≥32-byte check so state can't be signed with
a too-short key if the startup gate is ever bypassed.
"""
from __future__ import annotations

import pytest

from src.auth.oauth.state import StateError, _signing_key


def test_signing_key_rejects_short_key(monkeypatch):
    monkeypatch.delenv("MODEL_SIGNING_KEY", raising=False)
    monkeypatch.setenv("JWT_SECRET_KEY", "tooshort")   # 8 bytes < 32
    with pytest.raises(StateError, match="too short"):
        _signing_key()


def test_signing_key_accepts_32_raw_bytes(monkeypatch):
    monkeypatch.delenv("MODEL_SIGNING_KEY", raising=False)
    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 32)     # non-hex → utf-8, 32 bytes
    assert len(_signing_key()) >= 32


def test_signing_key_accepts_64_hex_chars(monkeypatch):
    monkeypatch.delenv("MODEL_SIGNING_KEY", raising=False)
    monkeypatch.setenv("JWT_SECRET_KEY", "ab" * 32)    # 64 hex chars → 32 bytes
    assert len(_signing_key()) == 32


def test_signing_key_missing_raises(monkeypatch):
    monkeypatch.delenv("MODEL_SIGNING_KEY", raising=False)
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    with pytest.raises(StateError, match="no signing key"):
        _signing_key()
