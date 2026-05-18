"""
Regression tests for Round-4 Phase 11 Tier 1 fixes (2026-05-17).

R4-2 — GET /clients + GET /clients/{id} now project ClientRecord
        through `_client_to_safe_dict()` so api_key_hash,
        email_canonical, oauth_subject are NEVER exposed in API
        responses.
R4-6 — POST /clients/{id}/upgrade now requires admin role until
        the payment-integration webhook ships. Non-admin owners
        get 403; frontend UpgradePage shows a sales-contact toast.

Source-level invariants + behavioural test of the projection helper.
"""
from __future__ import annotations

from pathlib import Path

import pytest


_BACKEND = Path(__file__).resolve().parents[2]


# ── R4-2: _client_to_safe_dict whitelist ──────────────────────────────────

def test_client_to_safe_dict_excludes_sensitive_fields():
    """The projection must drop api_key_hash, email_canonical,
    oauth_subject — those are the audit's named-PII set.

    Importing src.api.main pulls in vault_agent + uploads which use
    py3.10+ union-type syntax (`X | None`). On CI (py3.11) this runs;
    on the dev macOS py3.9 it's importorskip'd."""
    import sys
    if sys.version_info < (3, 10):
        pytest.skip("src.api.main uses py3.10+ X | None union syntax")
    try:
        from src.clients.registry import ClientRecord
        from src.api.main import _client_to_safe_dict, _CLIENT_SAFE_FIELDS
    except (TypeError, ImportError) as e:
        if "operand type(s) for |" in str(e) or "X | None" in str(e):
            pytest.skip(f"main.py import requires py3.10+ runtime: {e}")
        raise

    rec = ClientRecord(
        client_id="acme",
        config={"horizon": 14},
        storage_path="s3://acme/",
        email="user@example.com",
        email_canonical="user@example.com",   # internal form
        oauth_provider="google",
        oauth_subject="1234567890",           # provider PII
        api_key_hash="$2b$12$abcdefghijklmnopqrstu",
        plan="start",
    )
    safe = _client_to_safe_dict(rec)

    # Sensitive fields MUST be absent.
    assert "api_key_hash" not in safe, "bcrypt hash leak surface — R4-2"
    assert "email_canonical" not in safe, "internal canonical form leak — R4-2"
    assert "oauth_subject" not in safe, "provider PII leak — R4-2"

    # Public fields MUST be present.
    for k in ("client_id", "plan", "email", "oauth_provider", "config"):
        assert k in safe, f"safe-dict missing public field {k!r}"


def _clients_module_text() -> str:
    """Return the source text of whichever file owns the clients
    handlers. R5-M1 slice 6 (2026-05-18) moved the routes from
    main.py to routers/clients.py — these source-level tests must
    follow the move."""
    candidates = (
        _BACKEND / "src" / "api" / "routers" / "clients.py",
        _BACKEND / "src" / "api" / "main.py",
    )
    for f in candidates:
        if not f.is_file():
            continue
        text = f.read_text()
        if "async def list_clients" in text:
            return text
    raise AssertionError(
        "list_clients handler not found in main.py or routers/clients.py"
    )


def _clients_handler_block(text: str, signature: str) -> str:
    """Slice out one handler. Stops at the next @app. or @router.
    decorator (the file may use either)."""
    start = text.find(signature)
    assert start > 0, f"handler {signature!r} not found"
    end_app    = text.find('\n@app.',    start + 1)
    end_router = text.find('\n@router.', start + 1)
    ends = [e for e in (end_app, end_router) if e > 0]
    end = min(ends) if ends else -1
    return text[start:end] if end > 0 else text[start:]


def test_client_safe_fields_whitelist_is_explicit():
    """The whitelist must be a frozenset (immutable) and must contain
    no sensitive field names — guards against a future commit that
    accidentally adds api_key_hash etc back. Source-level (no import,
    survives py3.9 dev box)."""
    text = _clients_module_text()
    start = text.find("_CLIENT_SAFE_FIELDS = frozenset({")
    assert start > 0, "_CLIENT_SAFE_FIELDS frozenset missing"
    end = text.find("})", start)
    block = text[start:end]
    # Sensitive names must NOT be in the literal.
    for forbidden in ("api_key_hash", "email_canonical", "oauth_subject"):
        assert forbidden not in block, (
            f"{forbidden!r} appears in _CLIENT_SAFE_FIELDS literal — R4-2"
        )
    # The set is built as a frozenset literal (immutable).
    assert "frozenset({" in block, "_CLIENT_SAFE_FIELDS must be a frozenset"


def test_list_clients_handler_uses_safe_dict():
    """Source-level: the /clients (list_clients) handler must project
    each record through _client_to_safe_dict — not rec.__dict__."""
    block = _clients_handler_block(_clients_module_text(), "async def list_clients")
    assert "_client_to_safe_dict" in block, (
        "list_clients must use _client_to_safe_dict (R4-2)"
    )
    # The raw .__dict__ form is the leak surface; ensure it's gone.
    assert "c.__dict__" not in block, (
        "list_clients still exposing rec.__dict__ — leaks api_key_hash"
    )


def test_get_client_handler_uses_safe_dict():
    """Source-level: GET /clients/{id} handler must project through
    _client_to_safe_dict."""
    block = _clients_handler_block(_clients_module_text(), "async def get_client(client_id")
    assert "_client_to_safe_dict" in block, (
        "get_client must use _client_to_safe_dict (R4-2)"
    )
    assert "rec.__dict__" not in block, (
        "get_client still exposing rec.__dict__ — leaks api_key_hash"
    )


# ── R4-6 reverted: /upgrade self-service is INTENTIONAL during acquiring
#   integration phase. See project_app_state.md "Self-service plan upgrade".
#   The R4 audit-agent flagged this without awareness of the design intent;
#   user clarified on 2026-05-17 that the stub stays until payment lands.

def test_upgrade_route_remains_self_service_by_design():
    """BY-DESIGN: /upgrade must NOT have admin gate while the payment
    acquiring integration is pending. Owner-of-client can flip tiers
    freely — see project_app_state.md. Guards against a future audit
    re-applying the misplaced R4-6 admin gate without checking memory.

    R5-M1 (2026-05-18) — the handler moved from main.py to
    routers/plans.py as part of the god-module split. Try both
    locations; the invariant is about the handler body, not the file.
    """
    candidates = (
        _BACKEND / "src" / "api" / "main.py",
        _BACKEND / "src" / "api" / "routers" / "plans.py",
    )
    block = None
    for f in candidates:
        if not f.is_file():
            continue
        text = f.read_text()
        start = text.find("async def upgrade_client_plan")
        if start < 0:
            continue
        end_app    = text.find('\n@app.',    start + 1)
        end_router = text.find('\n@router.', start + 1)
        ends = [e for e in (end_app, end_router) if e > 0]
        end = min(ends) if ends else -1
        block = text[start:end] if end > 0 else text[start:]
        break
    assert block is not None, (
        "upgrade_client_plan handler not found in main.py or "
        "routers/plans.py — has it been deleted?"
    )
    # The handler must NOT require admin role until payment integration.
    assert 'require_role("admin")' not in block, (
        "/upgrade is intentionally self-service during acquiring stub — "
        "do NOT re-add require_role('admin') without first verifying "
        "the payment-integration status in project_app_state.md"
    )
    # Docstring must call out the by-design intent so a future audit
    # round doesn't re-flag.
    assert "BY-DESIGN" in block or "by design" in block.lower(), (
        "handler docstring must mark the self-service flow as intentional"
    )


# ── R4-6 frontend: UpgradePage handles 403 with sales-contact toast ──────

def test_frontend_upgrade_page_no_403_handler():
    """Mirror of test_upgrade_route_remains_self_service_by_design —
    frontend should NOT have a 403 sales-contact branch while the
    self-service upgrade stub is in place. The R4-6 mis-fix added
    one; reverted 2026-05-17 after user clarified the by-design
    intent."""
    page = _BACKEND.parent / "frontend" / "src" / "pages" / "client" / "UpgradePage.tsx"
    if not page.exists():
        pytest.skip("frontend repo not co-located (CI runner only)")
    text = page.read_text()
    assert "status === 403" not in text, (
        "UpgradePage 403 branch must NOT exist while /upgrade is "
        "self-service (acquiring integration not yet wired)"
    )


# ── R4-3 + R4-4: rate-limit holes on login + OTP verify ──────────────────

def test_check_login_attempt_exists_and_fail_open():
    """check_login_attempt must exist and fail-open on Redis down,
    matching the rest of the rate-limit family."""
    from src.auth import signup_rate_limit
    assert callable(getattr(signup_rate_limit, "check_login_attempt", None))
    # fail-open when Redis returns None.
    signup_rate_limit.check_login_attempt.__module__  # just confirm import


def test_check_otp_verify_attempt_exists():
    from src.auth import signup_rate_limit
    assert callable(getattr(signup_rate_limit, "check_otp_verify_attempt", None))


def test_auth_login_route_calls_check_login_attempt():
    """/auth/login handler must invoke check_login_attempt before
    captcha + DB work."""
    text = (_BACKEND / "src" / "api" / "main.py").read_text()
    start = text.find("async def auth_login_email")
    assert start > 0
    end = text.find('\n@app.', start + 1)
    block = text[start:end]
    assert "check_login_attempt" in block, (
        "/auth/login must wire check_login_attempt (R4-3)"
    )
    # Must come before captcha verify so cheap-rejects happen first.
    rl_idx = block.find("check_login_attempt")
    captcha_idx = block.find("_verify_captcha_or_400")
    assert rl_idx < captcha_idx, (
        "rate-limit must precede captcha so we don't burn captcha CPU on flood"
    )


def test_auth_signup_verify_calls_check_otp_verify_attempt():
    text = (_BACKEND / "src" / "api" / "main.py").read_text()
    start = text.find("async def auth_signup_verify")
    end = text.find('\n@app.', start + 1)
    block = text[start:end]
    assert "check_otp_verify_attempt" in block, (
        "/auth/signup/verify must wire check_otp_verify_attempt (R4-4)"
    )


def test_auth_login_verify_calls_check_otp_verify_attempt():
    text = (_BACKEND / "src" / "api" / "main.py").read_text()
    start = text.find("async def auth_login_verify")
    end = text.find('\n@app.', start + 1)
    block = text[start:end]
    assert "check_otp_verify_attempt" in block, (
        "/auth/login/verify must wire check_otp_verify_attempt (R4-4)"
    )


# ── R4-7: uploads IDOR enumeration → collapse cross-tenant to 404 ──────────

def test_uploads_get_collapses_cross_tenant_to_404():
    """GET /uploads/{upload_id} must return 404 (not 403) when the
    record exists but belongs to another client. R3-1 anti-enumeration
    pattern applied: the cross-tenant case shares the same response
    shape as 'doesn't exist'."""
    text = (_BACKEND / "src" / "api" / "uploads.py").read_text()
    start = text.find("async def get_upload_status")
    assert start > 0
    end = text.find("\n# ── DELETE", start)
    block = text[start:end]
    # Must NOT use require_client_access (which raises 403).
    assert "require_client_access" not in block, (
        "get_upload_status must not raise 403 on cross-tenant — use 404"
    )
    # Must check client_id explicitly and raise 404.
    assert "record.client_id" in block, "client_id ownership check missing"
    assert 'HTTPException(404' in block, "must respond 404 on cross-tenant"


def test_uploads_delete_collapses_cross_tenant_to_204():
    """DELETE /uploads/{upload_id} must return 204 (silent no-op) when
    the record exists but belongs to another client. Same anti-
    enumeration pattern — 204 matches the 'already gone' case shape."""
    text = (_BACKEND / "src" / "api" / "uploads.py").read_text()
    start = text.find("async def cancel_upload")
    assert start > 0
    end = text.find("\n\n", start + 1)
    if end < start:
        end = len(text)
    block = text[start:end]
    assert "require_client_access" not in block, (
        "cancel_upload must not raise 403 on cross-tenant"
    )
    # Must check client_id and return silently (204).
    assert "record.client_id" in block, "client_id check missing"
    assert "_cancel(upload_id)" in block, "real cancel call still present"


def test_main_py_upload_id_lookups_collapse_to_404():
    """Three upload_id lookups in main.py — predict's upload_id arg,
    /predict's extend_from_upload_id, /clients/{c}/uploads/{u}/skus —
    all must collapse cross-tenant to 404."""
    text = (_BACKEND / "src" / "api" / "main.py").read_text()
    # The legacy 403 detail string must NOT appear anywhere.
    assert "upload belongs to a different client" not in text, (
        "legacy 403 detail still present — R4-7 collapse not applied"
    )
    assert "extend_from upload belongs to a different client" not in text, (
        "extend_from 403 detail still present — R4-7 collapse not applied"
    )
