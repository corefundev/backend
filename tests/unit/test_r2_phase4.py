"""
Regression tests for Round-2 audit Phase 4 (CRITICAL) fixes shipped
2026-05-15. Each test name documents the property it locks in.

R2-1 (email hashing in logs) — covered indirectly: callers use
`_email_hash()` instead of raw email; the helper itself is trivial
(SHA-256[:12]) and tested via property assertion below.

R2-2 (ConsoleEmailSender prod-guard) — explicit refusal to instantiate
when APP_ENV=production.

R2-3 (OTP replay race fix) — `claim_active` returns the OTP exactly
once across concurrent callers; the file-backed implementation in this
unit-test runs serially but proves the contract: after `claim_active`
returns an OTP, a second call returns `None`.

R2-6 (Pydantic max_length + depth) — request models reject oversized
strings and deeply-nested dicts before they reach handlers.
"""
from __future__ import annotations

import json
import pytest

# Tests below import from `src.api.main`, which triggers the FastAPI
# app construction + Lockbox bootstrap at module-import time. If any
# of the heavy deps (fastapi, pydantic, prometheus_client) is missing
# the import fails — skip in that case rather than reporting a test
# failure for an environment issue.
pytest.importorskip("fastapi")
pytest.importorskip("pydantic")
pytest.importorskip("prometheus_client")


# ── R2-1: _email_hash helper is deterministic + 12-char hex ──────────────

def test_email_hash_is_deterministic_12char_hex():
    from src.api.main import _email_hash
    a = _email_hash("user@example.com")
    b = _email_hash("user@example.com")
    assert a == b
    assert len(a) == 12
    int(a, 16)  # raises if not hex


def test_email_hash_different_inputs_different_hashes():
    from src.api.main import _email_hash
    assert _email_hash("a@x.com") != _email_hash("b@x.com")


# ── R2-2: ConsoleEmailSender refuses prod ─────────────────────────────────

def test_console_email_sender_refuses_production(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    from src.auth.email_sender import ConsoleEmailSender
    with pytest.raises(RuntimeError, match="production"):
        ConsoleEmailSender()


def test_console_email_sender_accepts_dev(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    from src.auth.email_sender import ConsoleEmailSender
    sender = ConsoleEmailSender()  # no raise
    assert sender is not None


def test_console_email_sender_accepts_unset_env(monkeypatch):
    # Default (no APP_ENV) is treated as dev for ConsoleEmailSender
    # — this preserves the existing pytest workflow.
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
    # No row created.
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
    # The snapshot returned should report used_at=None (so the caller can
    # still see attempts/code_hash as they were pre-claim). DB state is
    # already marked used.
    from src.auth.otp_store import LocalFileOtpStore
    store = LocalFileOtpStore(str(tmp_path / "otp.json"))
    store.create(email="bob@example.com", purpose="login", code_hash="h")
    snap = store.claim_active("bob@example.com", "login")
    assert snap is not None
    # The OtpCode dataclass: used_at None on the returned snapshot.
    assert snap.used_at is None


# ── R2-6: Pydantic max_length + depth guards ─────────────────────────────

def test_token_request_rejects_huge_secret():
    from src.api.main import TokenRequest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        TokenRequest(client_id="acme", secret="x" * 10_000)


def test_token_request_rejects_empty_client_id():
    from src.api.main import TokenRequest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        TokenRequest(client_id="", secret="ok-secret")


def test_token_request_accepts_normal_values():
    from src.api.main import TokenRequest
    req = TokenRequest(client_id="acme", secret="sku_live_abc123def456")
    assert req.client_id == "acme"


def test_register_client_request_rejects_huge_notes():
    from src.api.main import RegisterClientRequest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        RegisterClientRequest(client_id="acme", notes="x" * 10_000)


def test_register_client_request_rejects_deeply_nested_config():
    from src.api.main import RegisterClientRequest
    from pydantic import ValidationError
    # Build a 15-deep nested dict — exceeds max_depth=10.
    cfg: dict = {"a": "leaf"}
    for _ in range(15):
        cfg = {"k": cfg}
    with pytest.raises(ValidationError):
        RegisterClientRequest(client_id="acme", config=cfg)


def test_register_client_request_accepts_moderate_nesting():
    from src.api.main import RegisterClientRequest
    # 5-deep is well within the 10 max.
    cfg: dict = {"a": "leaf"}
    for _ in range(5):
        cfg = {"k": cfg}
    req = RegisterClientRequest(client_id="acme", config=cfg)
    assert req.client_id == "acme"


def test_predict_request_rejects_huge_sku():
    from src.api.main import PredictRequest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        # No history-shape worth setting up — sku validation fires first.
        PredictRequest(sku="x" * 1000, upload_id="u-123")


def test_predict_request_accepts_normal_sku():
    from src.api.main import PredictRequest
    req = PredictRequest(sku="SKU-001", upload_id="u-123")
    assert req.sku == "SKU-001"
