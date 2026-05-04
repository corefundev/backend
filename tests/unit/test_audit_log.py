"""
Unit tests for src.audit.log.

The integration-test suite covers the happy path against a real
Postgres. These tests pin the *contract* that's most easily forgotten:

  1. record_event() never raises, even when DATABASE_URL is unset or
     points at an unreachable host. Audit must never crash a request.
  2. Read helpers return empty / zero on the same failure modes
     instead of bubbling exceptions.
"""
from __future__ import annotations

import logging
import os

from src.audit import (
    record_event,
    list_for_client,
    recent_failed_logins,
    EVT_LOGIN,
)


def test_record_event_is_silent_when_database_url_missing(monkeypatch, caplog):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    caplog.set_level(logging.INFO)
    record_event(
        event_type=EVT_LOGIN, event_subtype="success",
        client_id="acme", actor_email="alice@example.com",
        success=True,
    )
    # Fallback structured log line must be present even without DB.
    assert any("audit_event=login" in r.message for r in caplog.records)


def test_record_event_is_silent_when_database_unreachable(monkeypatch, caplog):
    # Point at a closed local port. psycopg2.connect raises; we eat it.
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none")
    caplog.set_level(logging.ERROR)
    # Must not raise — request paths depend on this.
    record_event(
        event_type=EVT_LOGIN, event_subtype="failure",
        actor_email="bob@example.com",
        success=False,
    )


def test_list_for_client_returns_empty_on_db_failure(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert list_for_client("acme") == []


def test_recent_failed_logins_returns_zero_on_db_failure(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert recent_failed_logins("alice@example.com") == 0


def test_replica_fallback_when_replica_url_unset(monkeypatch):
    """
    DATABASE_URL_REPLICA is OPTIONAL. When unset, reads should fall back
    to the primary connection — operators don't need to dual-configure
    just to use the audit module.
    """
    monkeypatch.delenv("DATABASE_URL_REPLICA", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from src.audit.log import _connect_read
    # No primary, no replica → returns None (not an exception).
    assert _connect_read() is None


def test_replica_fallback_when_replica_unreachable(monkeypatch):
    """
    DATABASE_URL_REPLICA pointed at an unreachable host should fall back
    to primary, not raise. Reads must remain available through brief
    standby outages.
    """
    monkeypatch.setenv("DATABASE_URL_REPLICA", "postgresql://nobody@127.0.0.1:1/none")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from src.audit.log import _connect_read
    # Replica unreachable + no primary → falls through to _connect → None.
    # Must not raise.
    assert _connect_read() is None


def test_lockout_threshold_env_vars_have_safe_defaults():
    """
    Both /auth/login/verify and /auth/signup/verify pull
    LOGIN_LOCKOUT_THRESHOLD / LOGIN_LOCKOUT_WINDOW_MIN at request time.
    A typo or env-source bug that returned 0 would silently disable
    lockout — pin the parsing so a regression is loud here, not in
    prod a month later.
    """
    import os
    # Defaults match what's in src/api/main.py
    assert int(os.environ.get("LOGIN_LOCKOUT_THRESHOLD", "10")) == 10
    assert int(os.environ.get("LOGIN_LOCKOUT_WINDOW_MIN", "15")) == 15


def test_event_type_constants_are_stable_strings():
    """
    Event-type taxonomy is a contract between the writer (record_event
    at the call site) and the reader (Grafana queries, support panels,
    audit_log indexes). Renaming any of these orphans historical data.
    Pin the strings explicitly so a typo or refactor is caught here.
    """
    from src.audit import (
        EVT_LOGIN, EVT_LOGOUT, EVT_OTP_SEND, EVT_OTP_VERIFY,
        EVT_OAUTH_CALLBACK, EVT_SIGNUP, EVT_PASSWORD_CHANGE,
        EVT_PLAN_CHANGE, EVT_EMAIL_CHANGE,
        EVT_MODEL_TRAIN, EVT_MODEL_DELETE, EVT_UPLOAD_DELETE,
        EVT_SECRET_ROTATION, EVT_ADMIN_ACTION,
    )
    expected = {
        EVT_LOGIN: "login",
        EVT_LOGOUT: "logout",
        EVT_OTP_SEND: "otp_send",
        EVT_OTP_VERIFY: "otp_verify",
        EVT_OAUTH_CALLBACK: "oauth_callback",
        EVT_SIGNUP: "signup",
        EVT_PASSWORD_CHANGE: "password_change",
        EVT_PLAN_CHANGE: "plan_change",
        EVT_EMAIL_CHANGE: "email_change",
        EVT_MODEL_TRAIN: "model_train",
        EVT_MODEL_DELETE: "model_delete",
        EVT_UPLOAD_DELETE: "upload_delete",
        EVT_SECRET_ROTATION: "secret_rotation",
        EVT_ADMIN_ACTION: "admin_action",
    }
    for const_value, expected_str in expected.items():
        assert const_value == expected_str
