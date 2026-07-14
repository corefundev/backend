"""
ARCH-2 (#206) — the two cross-cutting OTP-verify guards, previously
VERBATIM-DUPLICATED between /auth/signup/verify and the login path, are now
single-source shared helpers:

  _assert_otp_verify_rate_ok(http_req)          — R4-4 per-IP (/24) OTP cap
  _assert_not_locked_out(canonical, http_req…)  — per-email brute-force lockout

Why this matters (not cosmetic): the lockout window MUST be identical across the
two endpoints, or an attacker dodges the cap by alternating them against one
email. A shared helper enforces that invariant structurally; two hand-synced
copies could silently drift.

These tests pin: (1) every branch of each guard; (2) byte-identical detail
messages / audit metadata for BOTH endpoints (behaviour-preserving extraction);
(3) the handlers actually delegate and no longer inline the blocks.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import src.api.routers.auth as auth
from src.auth.signup_rate_limit import RateLimited


def _req(ua="agent/1.0"):
    return SimpleNamespace(headers={"user-agent": ua})


# ── _assert_otp_verify_rate_ok ────────────────────────────────────────────────

def test_rate_ok_passes_when_under_cap(monkeypatch):
    monkeypatch.setattr(auth, "client_ip", lambda r: "1.2.3.0")
    monkeypatch.setattr(auth, "check_otp_verify_attempt", lambda ip: None)
    # no raise
    assert auth._assert_otp_verify_rate_ok(_req()) is None


def test_rate_ok_raises_429_with_retry_after(monkeypatch):
    monkeypatch.setattr(auth, "client_ip", lambda r: "1.2.3.0")

    def _blow(ip):
        raise RateLimited("slow down", retry_after_sec=42)
    monkeypatch.setattr(auth, "check_otp_verify_attempt", _blow)

    with pytest.raises(HTTPException) as ei:
        auth._assert_otp_verify_rate_ok(_req())
    assert ei.value.status_code == 429
    assert ei.value.headers["Retry-After"] == "42"


def test_rate_ok_retry_after_falls_back_to_60(monkeypatch):
    monkeypatch.setattr(auth, "client_ip", lambda r: "1.2.3.0")

    def _blow(ip):
        raise RateLimited("slow down", retry_after_sec=0)   # 0 → falsy → 60
    monkeypatch.setattr(auth, "check_otp_verify_attempt", _blow)

    with pytest.raises(HTTPException) as ei:
        auth._assert_otp_verify_rate_ok(_req())
    assert ei.value.headers["Retry-After"] == "60"


# ── _assert_not_locked_out ────────────────────────────────────────────────────

def _wire_lockout(monkeypatch, *, recent, threshold="10", window="15"):
    events: list = []
    monkeypatch.setenv("LOGIN_LOCKOUT_THRESHOLD", threshold)
    monkeypatch.setenv("LOGIN_LOCKOUT_WINDOW_MIN", window)
    monkeypatch.setattr(auth, "client_ip", lambda r: "9.9.9.0")
    monkeypatch.setattr(auth, "recent_failed_logins",
                        lambda canonical, window_minutes: recent)
    monkeypatch.setattr(auth, "record_event", lambda **kw: events.append(kw))
    return events


def test_lockout_disabled_when_threshold_zero(monkeypatch):
    events = _wire_lockout(monkeypatch, recent=999, threshold="0")
    # threshold<=0 → no lookup, no event, no raise
    assert auth._assert_not_locked_out(
        "a@x.io", _req(), event_type="EVT", event_subtype="s",
        attempt_label="attempts") is None
    assert events == []


def test_lockout_passes_under_threshold(monkeypatch):
    events = _wire_lockout(monkeypatch, recent=9, threshold="10")
    assert auth._assert_not_locked_out(
        "a@x.io", _req(), event_type="EVT", event_subtype="s",
        attempt_label="attempts") is None
    assert events == []


def test_lockout_trips_at_threshold_records_event_and_429(monkeypatch):
    events = _wire_lockout(monkeypatch, recent=10, threshold="10", window="15")
    with pytest.raises(HTTPException) as ei:
        auth._assert_not_locked_out(
            "a@x.io", _req(), event_type="EVT_LOGIN", event_subtype="locked_out",
            attempt_label="login attempts")
    assert ei.value.status_code == 429
    assert ei.value.headers["Retry-After"] == str(15 * 60)
    # audit event carries the canonical email + counters
    assert len(events) == 1
    ev = events[0]
    assert ev["event_type"] == "EVT_LOGIN"
    assert ev["event_subtype"] == "locked_out"
    assert ev["actor_email"] == "a@x.io"
    assert ev["success"] is False
    assert ev["metadata"] == {"recent_fails": 10, "window_min": 15, "threshold": 10}


def test_lockout_detail_messages_match_both_endpoints(monkeypatch):
    # Behaviour-preserving extraction: the two endpoints must keep their exact
    # historical wording — only the noun differs.
    _wire_lockout(monkeypatch, recent=10, threshold="10", window="15")

    with pytest.raises(HTTPException) as signup:
        auth._assert_not_locked_out(
            "a@x.io", _req(), event_type="EVT_OTP_VERIFY",
            event_subtype="locked_out_signup", attempt_label="attempts")
    assert signup.value.detail == "Too many failed attempts; try again in 15 minutes."

    with pytest.raises(HTTPException) as login:
        auth._assert_not_locked_out(
            "a@x.io", _req(), event_type="EVT_LOGIN",
            event_subtype="locked_out", attempt_label="login attempts")
    assert login.value.detail == "Too many failed login attempts; try again in 15 minutes."


# ── structural: handlers delegate, no more inline duplication ─────────────────

def test_handlers_delegate_to_shared_guards():
    # AUTH-4 #448: OTP-вход снесён — второй потребитель хелпера теперь
    # парольный вход (тот же lockout-инвариант, та же дедупликация).
    # per-fn rate-guard: у verify — общий OTP-cap, у пароля — R4-3 bucket
    expected_rate_guard = {
        "auth_signup_verify": "_assert_otp_verify_rate_ok(http_req)",
        "auth_login_password": "check_login_attempt(",
    }
    for fn in (auth.auth_signup_verify, auth.auth_login_password):
        src = inspect.getsource(fn)
        assert expected_rate_guard[fn.__name__] in src, f"{fn.__name__} must call the rate guard"
        assert "_assert_not_locked_out(" in src, f"{fn.__name__} must call the lockout guard"
        # the inline copies are gone — the raw call now lives ONLY in the helper
        assert "check_otp_verify_attempt(client_ip(" not in src, (
            f"{fn.__name__} still inlines the per-IP guard (dedup incomplete)"
        )
        assert "recent_failed_logins(" not in src, (
            f"{fn.__name__} still inlines the lockout lookup (dedup incomplete)"
        )
