"""
Regression tests for Round-2 audit Phase 4 (CRITICAL) fixes shipped
2026-05-15.

Pure-stdlib + leaf-module imports only. Tests that need
`from src.api.main import ...` were removed in the post-deploy fix
2026-05-15: importing main.py from tests triggers FastAPI app build +
Prometheus counter registration, which collides with prior imports
within the same pytest session ("Duplicated timeseries"). The
functionality that USED to be tested here through main.py is now
covered by:
  • _email_hash / _assert_json_depth     → tested via _helpers below
  • Pydantic model max_length / depth    → enforced by Pydantic at
    runtime, exercised in integration tests that hit the real routes
  • OTP claim_active                     → tested via LocalFileOtpStore
  • ConsoleEmailSender prod-guard        → standalone module import
"""
from __future__ import annotations

import pytest


# ── R2-1: email_hash helper is deterministic + 12-char hex ───────────────

def test_email_hash_is_deterministic_12char_hex():
    from src.api._helpers import email_hash
    a = email_hash("user@example.com")
    b = email_hash("user@example.com")
    assert a == b
    assert len(a) == 12
    int(a, 16)  # raises if not hex


def test_email_hash_different_inputs_different_hashes():
    from src.api._helpers import email_hash
    assert email_hash("a@x.com") != email_hash("b@x.com")


# ── R2-2: ConsoleEmailSender refuses prod ─────────────────────────────────

def test_console_email_sender_refuses_production(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    from src.auth.email_sender import ConsoleEmailSender
    with pytest.raises(RuntimeError, match="production"):
        ConsoleEmailSender()


def test_console_email_sender_accepts_dev(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    from src.auth.email_sender import ConsoleEmailSender
    sender = ConsoleEmailSender()
    assert sender is not None


def test_console_email_sender_accepts_unset_env(monkeypatch):
    monkeypatch.delenv("APP_ENV", raising=False)
    from src.auth.email_sender import ConsoleEmailSender
    ConsoleEmailSender()


def test_console_email_sender_case_insensitive_production(monkeypatch):
    monkeypatch.setenv("APP_ENV", "  PRODUCTION  ")
    from src.auth.email_sender import ConsoleEmailSender
    with pytest.raises(RuntimeError, match="production"):
        ConsoleEmailSender()


# ── R2-3: OTP claim_active is single-use ─────────────────────────────────

def test_otp_claim_active_returns_row_once(tmp_path):
    from src.auth.otp_store import LocalFileOtpStore
    store = LocalFileOtpStore(str(tmp_path / "otp.json"))
    store.create(email="alice@example.com", purpose="login", code_hash="h")

    first  = store.claim_active("alice@example.com", "login")
    second = store.claim_active("alice@example.com", "login")
    third  = store.claim_active("alice@example.com", "login")

    assert first is not None
    assert second is None
    assert third is None


def test_otp_claim_active_no_active_returns_none(tmp_path):
    from src.auth.otp_store import LocalFileOtpStore
    store = LocalFileOtpStore(str(tmp_path / "otp.json"))
    assert store.claim_active("alice@example.com", "login") is None


def test_otp_claim_active_isolated_by_purpose(tmp_path):
    from src.auth.otp_store import LocalFileOtpStore
    store = LocalFileOtpStore(str(tmp_path / "otp.json"))
    store.create(email="alice@example.com", purpose="signup", code_hash="h_s")
    store.create(email="alice@example.com", purpose="login",  code_hash="h_l")

    s = store.claim_active("alice@example.com", "signup")
    l = store.claim_active("alice@example.com", "login")
    assert s is not None
    assert l is not None
    assert s.code_hash == "h_s"
    assert l.code_hash == "h_l"


def test_otp_claim_active_returns_pre_mark_snapshot(tmp_path):
    from src.auth.otp_store import LocalFileOtpStore
    store = LocalFileOtpStore(str(tmp_path / "otp.json"))
    store.create(email="bob@example.com", purpose="login", code_hash="h")
    snap = store.claim_active("bob@example.com", "login")
    assert snap is not None
    assert snap.used_at is None


# ── R2-6: _assert_json_depth helper directly ──────────────────────────────

def test_assert_json_depth_accepts_shallow():
    from src.api._helpers import assert_json_depth
    assert_json_depth({"k": {"nested": "value"}}, max_depth=10)
    assert_json_depth({"a": 1, "b": [1, 2, {"c": 3}]}, max_depth=10)


def test_assert_json_depth_rejects_deep_dict():
    from src.api._helpers import assert_json_depth
    cfg: dict = {"a": "leaf"}
    for _ in range(15):
        cfg = {"k": cfg}
    with pytest.raises(ValueError, match="nesting"):
        assert_json_depth(cfg, max_depth=10)


def test_assert_json_depth_rejects_deep_list():
    from src.api._helpers import assert_json_depth
    payload: object = "leaf"
    for _ in range(15):
        payload = [payload]
    with pytest.raises(ValueError, match="nesting"):
        assert_json_depth(payload, max_depth=10)


def test_assert_json_depth_handles_mixed():
    from src.api._helpers import assert_json_depth
    mixed = {"a": [{"b": [{"c": "deep"}]}]}
    assert_json_depth(mixed, max_depth=10)


def test_assert_json_depth_scalar_is_noop():
    from src.api._helpers import assert_json_depth
    assert_json_depth("just a string")
    assert_json_depth(42)
    assert_json_depth(None)
