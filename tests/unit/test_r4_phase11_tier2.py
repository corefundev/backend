"""
Regression tests for Round-4 Phase 11 Tier 2 (2026-05-17).

R4-11 — monitoring.send_alert now routes through validate_webhook_url
        before urlopen, matching the R3-7 SSRF guard on
        send_webhook_sync. Closes the parallel SSRF path that the
        R3 audit missed.
R4-12 — jwt_auth._load_secret enforces HS256 minimum byte length
        (32) on JWT_SECRET_KEY and MODEL_SIGNING_KEY in production.
        startup_safety.py environment-variable name typo fixed
        (was `JWT_SECRET`, actual var is `JWT_SECRET_KEY`).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


_BACKEND = Path(__file__).resolve().parents[2]


# ── R4-11: SSRF guard on monitoring.send_alert ─────────────────────────────

def test_send_alert_validates_url_before_urlopen():
    """Source-level: the send_alert body must call validate_webhook_url
    BEFORE the urlopen invocation. Order matters — validation has to
    short-circuit before the network call."""
    text = (_BACKEND / "src" / "monitoring" / "advanced.py").read_text()
    start = text.find("def send_alert(")
    assert start > 0
    end = text.find("\n# ── ", start + 1)
    if end < 0:
        end = len(text)
    block = text[start:end]
    validate_idx = block.find("validate_webhook_url")
    urlopen_idx = block.find("urllib.request.urlopen")
    assert validate_idx > 0, "send_alert must import validate_webhook_url"
    assert urlopen_idx > validate_idx, (
        "validate_webhook_url must be called BEFORE urlopen (R4-11)"
    )


def test_send_alert_catches_rejected_url_and_returns_false():
    """The handler must catch WebhookUrlRejected and return False —
    fail-soft semantics matching send_webhook_sync (R3-7 lesson)."""
    text = (_BACKEND / "src" / "monitoring" / "advanced.py").read_text()
    start = text.find("def send_alert(")
    end = text.find("\n# ── ", start + 1)
    block = text[start:end] if end > 0 else text[start:]
    assert "WebhookUrlRejected" in block, (
        "send_alert must catch WebhookUrlRejected"
    )
    assert "return False" in block, "rejection must fail-soft (return False)"


# ── R4-12: JWT_SECRET_KEY min-length enforcement ──────────────────────────

def test_load_secret_min_length_enforced_in_prod():
    """_load_secret refuses HS256 keys shorter than 32 bytes when not
    in dev mode. Tests the actual production code path."""
    import sys
    if sys.version_info < (3, 10):
        pytest.skip("src.auth.jwt_auth uses py3.10+ union syntax")
    try:
        from src.auth import jwt_auth
    except (TypeError, ImportError) as e:
        if "operand type(s) for |" in str(e):
            pytest.skip("py3.10+ union syntax not parsable on py3.9")
        raise

    # Force production mode for the test.
    original_is_dev = jwt_auth._IS_DEV
    jwt_auth._IS_DEV = False
    try:
        os.environ["JWT_SECRET_KEY_TEST_R4_12"] = "x" * 16  # < 32 bytes
        with pytest.raises(RuntimeError, match="HS256 floor"):
            jwt_auth._load_secret("JWT_SECRET_KEY",
                                  None)  # noqa: F841 — should raise
    finally:
        jwt_auth._IS_DEV = original_is_dev


def test_load_secret_accepts_long_keys():
    """A 32+ byte secret passes the gate."""
    import sys
    if sys.version_info < (3, 10):
        pytest.skip("py3.10+")
    try:
        from src.auth import jwt_auth
    except (TypeError, ImportError):
        pytest.skip("import gate")

    original_is_dev = jwt_auth._IS_DEV
    jwt_auth._IS_DEV = False
    long_key = "a" * 64
    try:
        # We patch _env to return the key directly, bypassing the
        # actual env-var read.
        original_env = jwt_auth._env
        jwt_auth._env = lambda k, default=None: long_key if k == "JWT_SECRET_KEY" else default
        result = jwt_auth._load_secret("JWT_SECRET_KEY", None)
        assert result == long_key
    finally:
        jwt_auth._IS_DEV = original_is_dev
        jwt_auth._env = original_env


def test_load_secret_source_has_hs256_floor():
    """Source-level: confirm _HS256_MIN_BYTES = 32 and the gate
    references JWT_SECRET_KEY + MODEL_SIGNING_KEY."""
    text = (_BACKEND / "src" / "auth" / "jwt_auth.py").read_text()
    assert "_HS256_MIN_BYTES = 32" in text, (
        "32-byte HS256 floor must be the documented constant"
    )
    assert "JWT_SECRET_KEY" in text and "MODEL_SIGNING_KEY" in text, (
        "the length gate must cover both signing keys"
    )


def test_startup_safety_uses_correct_jwt_var_name():
    """startup_safety previously checked the wrong env var
    `JWT_SECRET` instead of `JWT_SECRET_KEY`. R4-12 fixes the typo."""
    text = (_BACKEND / "src" / "api" / "startup_safety.py").read_text()
    # The legacy wrong name must not appear except in comments/legacy refs.
    # The new check uses the correct name.
    assert 'os.environ.get("JWT_SECRET_KEY")' in text, (
        "startup_safety must check JWT_SECRET_KEY (the actual env var)"
    )
    # The old buggy check pattern with the wrong-name single quoted form
    # must NOT appear as an active check.
    assert 'os.environ.get("JWT_SECRET")' not in text, (
        "the typoed `JWT_SECRET` check must be removed (R4-12)"
    )


def test_startup_safety_warns_on_short_jwt_secret():
    """startup_safety surfaces a soft warning when JWT_SECRET_KEY is
    set but shorter than 32 bytes — operator visibility before the
    hard-fail at jwt_auth._load_secret first read."""
    text = (_BACKEND / "src" / "api" / "startup_safety.py").read_text()
    start = text.find("R4-12")
    assert start > 0, "startup_safety must reference R4-12"
    end = text.find("MODEL_SIGNING_KEY is empty", start)
    block = text[start:end] if end > 0 else text[start:]
    assert "32" in block, "short-key warning must mention the 32-byte floor"
    assert "len(_jwt_secret_val" in block, (
        "must actually compute the length, not just check empty"
    )
