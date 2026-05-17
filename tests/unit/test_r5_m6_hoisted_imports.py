"""
Regression tests for R5-M6 — lazy-import hoisting in src/api/main.py
(2026-05-18).

The audit flagged 74 function-local `from src.xxx import` statements
inside route handlers. They hid the dependency graph from static
analysis, forced each route call to re-resolve the import, and made
mypy / IDE rename tools unable to follow the references.

This commit hoists the top two most-imported targets:
  - src.audit (15+ lazy imports → 1 top-level block)
  - src.auth.signup_rate_limit (16 lazy imports → 1 top-level block)

Why these two: leaf modules in the import graph (verified to not
import src.api.main back — no circular-import risk), and they
together account for 31 of the 74 audit-flagged lazy imports.

The remaining lazy imports are deliberately kept lazy:
  - src.pipeline.train.run_training_pipeline (worker-side only, not
    on /health code path)
  - src.models.* (ML heavy-imports kept off liveness probe)
  - src.notifications.training_email (optional dep, may not be
    installed in dev)
"""
from __future__ import annotations

import re
from pathlib import Path


_BACKEND = Path(__file__).resolve().parents[2]
_MAIN = _BACKEND / "src" / "api" / "main.py"


def _top_section(text: str) -> str:
    """Return everything before the first FastAPI route declaration
    `@app.` so we examine module-level imports / setup only."""
    idx = text.find("\n@app.")
    return text[:idx] if idx > 0 else text


def test_audit_hoisted_to_module_top():
    """Every audit symbol used inside route handlers must come from a
    single top-level import block. No `from src.audit import` is
    allowed inside function bodies."""
    text = _MAIN.read_text()
    # Must have at least one top-level `from src.audit import` line
    # in the pre-route section.
    top = _top_section(text)
    assert "from src.audit import" in top, (
        "src.api.main.py must hoist `from src.audit import ...` to "
        "module-level (R5-M6)"
    )
    # No function-local `from src.audit import` (indented) anywhere.
    indented = re.findall(r"^[ \t]+from src\.audit import", text, re.M)
    assert not indented, (
        f"src.api.main.py must not have any function-local "
        f"`from src.audit import` after R5-M6 — found {len(indented)}"
    )


def test_signup_rate_limit_hoisted_to_module_top():
    """Same invariant for signup_rate_limit — hoist to top, no
    function-local re-imports."""
    text = _MAIN.read_text()
    top = _top_section(text)
    assert "from src.auth.signup_rate_limit import" in top, (
        "src.api.main.py must hoist `from src.auth.signup_rate_limit "
        "import ...` to module-level (R5-M6)"
    )
    indented = re.findall(
        r"^[ \t]+from src\.auth\.signup_rate_limit import", text, re.M
    )
    assert not indented, (
        f"src.api.main.py must not have any function-local "
        f"`from src.auth.signup_rate_limit import` after R5-M6 — "
        f"found {len(indented)}"
    )


def test_hoisted_imports_cover_all_used_symbols():
    """The top-level audit + signup_rate_limit blocks must export
    every symbol the route handlers reference."""
    text = _MAIN.read_text()
    top = _top_section(text)
    body = text[len(top):]
    # Symbols referenced from src.audit:
    for sym in (
        "record_event", "EVT_LOGIN", "EVT_SIGNUP", "EVT_OTP_SEND",
        "EVT_OTP_VERIFY", "EVT_OAUTH_CALLBACK", "EVT_ADMIN_ACTION",
        "EVT_PLAN_CHANGE", "EVT_PASSWORD_CHANGE", "EVT_SECRET_ROTATION",
        "EVT_MODEL_TRAIN",
    ):
        if sym in body:
            assert sym in top, (
                f"symbol {sym!r} is used in routes but not imported "
                "at module top (R5-M6)"
            )
    # signup_rate_limit symbols:
    for sym in (
        "check_signup_attempt", "check_token_attempt", "check_login_attempt",
        "check_otp_verify_attempt", "check_rotate_attempt",
        "record_signup_success", "RateLimited",
    ):
        if sym in body:
            assert sym in top, (
                f"symbol {sym!r} is used in routes but not imported "
                "at module top (R5-M6)"
            )


def test_main_py_compiles():
    """Sanity: main.py must compile post-hoist. A broken
    indent / dangling `, name,` from incomplete lazy-import removal
    would crash here."""
    import py_compile
    try:
        py_compile.compile(str(_MAIN), doraise=True)
    except py_compile.PyCompileError as e:
        raise AssertionError(f"src/api/main.py fails to compile: {e}")
