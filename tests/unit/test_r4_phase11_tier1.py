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


def test_client_safe_fields_whitelist_is_explicit():
    """The whitelist must be a frozenset (immutable) and must contain
    no sensitive field names — guards against a future commit that
    accidentally adds api_key_hash etc back. Source-level (no import,
    survives py3.9 dev box)."""
    text = (_BACKEND / "src" / "api" / "main.py").read_text()
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
    text = (_BACKEND / "src" / "api" / "main.py").read_text()
    # Find handler block.
    start = text.find('async def list_clients')
    assert start > 0
    end = text.find('\n@app.', start + 1)
    block = text[start:end]
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
    text = (_BACKEND / "src" / "api" / "main.py").read_text()
    start = text.find('async def get_client(client_id')
    assert start > 0
    end = text.find('\n@app.', start + 1)
    block = text[start:end]
    assert "_client_to_safe_dict" in block, (
        "get_client must use _client_to_safe_dict (R4-2)"
    )
    assert "rec.__dict__" not in block, (
        "get_client still exposing rec.__dict__ — leaks api_key_hash"
    )


# ── R4-6: /upgrade admin gate ─────────────────────────────────────────────

def test_upgrade_route_requires_admin():
    """The upgrade_client_plan handler must call
    `auth.require_role("admin")` BEFORE proceeding to the registry
    write — until payment integration ships, this prevents self-service
    plan elevation."""
    text = (_BACKEND / "src" / "api" / "main.py").read_text()
    start = text.find("async def upgrade_client_plan")
    assert start > 0
    end = text.find('\n@app.', start + 1)
    block = text[start:end]
    # Must require admin role.
    assert 'require_role("admin")' in block, (
        'upgrade_client_plan must call auth.require_role("admin") (R4-6)'
    )
    # Must come BEFORE the registry.update — protect the audit-log row
    # from firing on a denied call.
    role_idx = block.find('require_role("admin")')
    update_idx = block.find("registry.update(")
    assert update_idx == -1 or role_idx < update_idx, (
        "require_role must precede registry.update — gate before mutate"
    )


def test_upgrade_route_docstring_mentions_r4_6():
    """The handler docstring should reference R4-6 + the planned
    payment-integration follow-up so a future maintainer knows the
    gate is transitional, not permanent."""
    text = (_BACKEND / "src" / "api" / "main.py").read_text()
    start = text.find("async def upgrade_client_plan")
    end = text.find('require_role("admin")', start)
    docstring = text[start:end]
    assert "R4-6" in docstring, "upgrade handler docstring must reference R4-6"
    assert "payment" in docstring.lower(), (
        "docstring should mention payment-integration as the follow-up"
    )


# ── R4-6 frontend: UpgradePage handles 403 with sales-contact toast ──────

def test_frontend_upgrade_page_handles_403():
    """UpgradePage onError must branch on HTTP 403 and surface a
    sales-contact message instead of the generic 'Не удалось сменить
    тариф' toast. Pairs with the backend admin-gate so the customer
    knows where to go."""
    page = _BACKEND.parent / "frontend" / "src" / "pages" / "client" / "UpgradePage.tsx"
    if not page.exists():
        pytest.skip("frontend repo not co-located (CI runner only)")
    text = page.read_text()
    assert "status === 403" in text, (
        "UpgradePage must catch 403 to surface sales-contact UX"
    )
    assert "support@testcore.ru" in text or "поддержки" in text, (
        "UpgradePage 403 branch must include a sales-contact hint"
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
