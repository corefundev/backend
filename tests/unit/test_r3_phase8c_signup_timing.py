"""
Regression tests for Round-3 Phase 8C (HIGH) — R3-9 close-out 2026-05-15.

R3-9 — signup vs login timing-enumeration.
The /auth/signup route used to return 409 on duplicate email and 202
on new email, giving an attacker a trivial enumeration oracle for
registered addresses (OWASP CWE-203). The fix:

  • email already registered → identical 202 response, no OTP created,
    no email sent, audit event "duplicate_email_ignored" emitted
  • client_id already taken → keeps 409 (workspace handle is public,
    not an enumeration concern; honest user needs the error)

Source-level assertions (no main.py import, dodging Prometheus
Counter "Duplicated timeseries" — same pattern as Phase 8B's R3-8
guard test). The actual code path is exercised in integration when
the route runs.
"""
from __future__ import annotations

from pathlib import Path


_MAIN = Path(__file__).resolve().parents[2] / "src" / "api" / "main.py"


def _signup_block() -> str:
    """Slice the /auth/signup handler body for source-level assertions."""
    text = _MAIN.read_text()
    start = text.find("async def auth_signup")
    assert start > 0, "auth_signup handler missing"
    # End at the next decorator/route definition.
    end = text.find('@app.post("/auth/signup/verify"', start)
    assert end > start, "auth_signup/verify route missing — handler boundary lost"
    return text[start:end]


def test_signup_duplicate_email_returns_accepted_not_409():
    """The duplicate-email branch must build and return SignupAcceptedResponse,
    NOT raise HTTPException(409). Pattern: the duplicate check sits before
    the OTP-create block and exits via `return` rather than `raise`."""
    block = _signup_block()
    # The marker comment must be present.
    assert "R3-9" in block, "R3-9 audit reference missing from signup handler"
    # There must be a SignupAcceptedResponse return inside the dup-email branch.
    # The `existing is not None` branch comes before the client_id check.
    existing_idx = block.find("existing = registry.get_by_email(email)")
    cid_check_idx = block.find("registry.get(client_id) is not None")
    assert existing_idx > 0, "registry.get_by_email lookup missing"
    assert cid_check_idx > existing_idx, "client_id check must come after email check"
    # Between them, a SignupAcceptedResponse return must exist (the silent-202).
    dup_branch = block[existing_idx:cid_check_idx]
    assert "return SignupAcceptedResponse" in dup_branch, (
        "duplicate-email branch must return 202, not raise 409"
    )
    assert "HTTPException(status_code=409" not in dup_branch, (
        "duplicate-email branch must NOT raise 409 (enumeration vector)"
    )


def test_signup_duplicate_email_does_not_create_otp():
    """No OTP create() in the dup-email branch — otherwise dup signups
    burn rows + email quota and pollute the OTP table."""
    block = _signup_block()
    existing_idx = block.find("existing = registry.get_by_email(email)")
    cid_check_idx = block.find("registry.get(client_id) is not None")
    dup_branch = block[existing_idx:cid_check_idx]
    assert "store.create(" not in dup_branch, (
        "duplicate-email branch must skip OTP store.create()"
    )
    assert "get_email_sender" not in dup_branch, (
        "duplicate-email branch must skip email send"
    )


def test_signup_duplicate_email_audits_event():
    """An audit event with subtype 'duplicate_email_ignored' must fire
    in the dup-email branch so operators can monitor abuse signals via
    the security-events Prometheus + Loki stack."""
    block = _signup_block()
    existing_idx = block.find("existing = registry.get_by_email(email)")
    cid_check_idx = block.find("registry.get(client_id) is not None")
    dup_branch = block[existing_idx:cid_check_idx]
    assert "record_event" in dup_branch, "audit record_event missing from dup-email branch"
    assert "duplicate_email_ignored" in dup_branch, (
        "audit event_subtype 'duplicate_email_ignored' missing"
    )


def test_signup_duplicate_client_id_still_returns_409():
    """client_id collision (with a new email) must still 409 — public
    workspace handles are NOT an enumeration concern, and silently
    accepting would create a verification dead-end for honest users."""
    block = _signup_block()
    cid_check_idx = block.find("registry.get(client_id) is not None")
    assert cid_check_idx > 0, "client_id collision check missing"
    # Window ends at the OTP generation step.
    otp_idx = block.find("from src.auth.otp import generate_otp")
    assert otp_idx > cid_check_idx, "OTP generation must follow client_id check"
    cid_branch = block[cid_check_idx:otp_idx]
    assert "HTTPException(status_code=409" in cid_branch, (
        "client_id collision must still raise 409"
    )


def test_signup_handler_canonicalizes_before_lookup():
    """Pre-existing R1 invariant — canonical_email MUST normalize before
    the dup-email lookup, otherwise gmail dot/+alias tricks bypass
    enumeration suppression too. Defensive regression check."""
    block = _signup_block()
    canonical_idx = block.find("canonical = canonical_email(email)")
    existing_idx = block.find("existing = registry.get_by_email(email)")
    assert canonical_idx > 0 and existing_idx > 0
    assert canonical_idx < existing_idx, (
        "canonical_email must run before the dup-email lookup"
    )
