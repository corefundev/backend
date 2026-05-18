"""
Regression tests for R5-M6 — lazy-import hoisting (2026-05-18).

Originally pinned imports in `src/api/main.py`. After R5-M1 sliced
the routes out into per-domain routers, the M6 invariant applies
to whatever file CURRENTLY contains route handlers using the
hoisted symbols:

  * `from src.audit import ...`              → audit/record_event/EVT_*
  * `from src.auth.signup_rate_limit import` → rate-limit/RateLimited

Files that use these symbols today (post-slice-9): routers/auth.py,
routers/clients.py, routers/training.py, routers/inference.py,
routers/plans.py, routers/config.py, routers/audit.py. main.py
still uses `from src.audit import` for the lifespan secret-rotation
hook (record_event, verify_chain, EVT_SECRET_ROTATION) — and that's
the only one left.

Invariants pinned:
  1. EVERY file that imports from `src.audit` or
     `src.auth.signup_rate_limit` does so at MODULE level (not
     inside a function body).
  2. main.py still compiles (smoke).
"""
from __future__ import annotations

import re
from pathlib import Path


_BACKEND = Path(__file__).resolve().parents[2]
_MAIN = _BACKEND / "src" / "api" / "main.py"


def _route_files() -> list[Path]:
    """Files that may contain hoisted symbols: main.py + every
    file in src/api/routers/ (excluding __init__.py)."""
    files = [_MAIN]
    routers_dir = _BACKEND / "src" / "api" / "routers"
    if routers_dir.is_dir():
        for f in routers_dir.glob("*.py"):
            if f.name != "__init__.py":
                files.append(f)
    return files


def test_audit_imports_are_module_level():
    """No file under src/api/ may have a function-local
    `from src.audit import ...` — they all must be hoisted."""
    offenders: list[tuple[str, int]] = []
    for f in _route_files():
        text = f.read_text()
        # Find indented (=function-local) `from src.audit import` lines.
        for m in re.finditer(r"^[ \t]+from src\.audit import", text, re.M):
            line_no = text[: m.start()].count("\n") + 1
            offenders.append((str(f.relative_to(_BACKEND)), line_no))
    assert not offenders, (
        f"R5-M6: function-local `from src.audit import` must not appear "
        f"in any router/main file. Offenders:\n"
        + "\n".join(f"  {f}:{ln}" for f, ln in offenders)
    )


def test_signup_rate_limit_imports_are_module_level():
    """Same invariant for src.auth.signup_rate_limit."""
    offenders: list[tuple[str, int]] = []
    for f in _route_files():
        text = f.read_text()
        for m in re.finditer(
            r"^[ \t]+from src\.auth\.signup_rate_limit import", text, re.M
        ):
            line_no = text[: m.start()].count("\n") + 1
            offenders.append((str(f.relative_to(_BACKEND)), line_no))
    assert not offenders, (
        f"R5-M6: function-local `from src.auth.signup_rate_limit import` "
        f"must not appear. Offenders:\n"
        + "\n".join(f"  {f}:{ln}" for f, ln in offenders)
    )


def test_hoisted_imports_cover_all_used_symbols():
    """For each file that REFERENCES a hoisted symbol in its body,
    there must be a module-level `from src.audit import ...` or
    `from src.auth.signup_rate_limit import ...` that pulls it in."""
    AUDIT_SYMBOLS = {
        "record_event", "EVT_LOGIN", "EVT_SIGNUP", "EVT_OTP_SEND",
        "EVT_OTP_VERIFY", "EVT_OAUTH_CALLBACK", "EVT_ADMIN_ACTION",
        "EVT_PLAN_CHANGE", "EVT_PASSWORD_CHANGE", "EVT_SECRET_ROTATION",
        "EVT_MODEL_TRAIN", "recent_failed_logins", "verify_chain",
        "list_for_client",
    }
    SRL_SYMBOLS = {
        "check_signup_attempt", "check_token_attempt", "check_login_attempt",
        "check_otp_verify_attempt", "check_rotate_attempt",
        "check_predict_attempt", "record_signup_success", "RateLimited",
        "client_ip", "assert_signup_allowed",
    }
    for f in _route_files():
        text = f.read_text()
        # Module-level import block lookup (read up to first `def`/`class`).
        # We just check that any referenced symbol has a matching
        # `from src.<module> import` line ANYWHERE (module-level by the
        # invariants above).
        for sym in AUDIT_SYMBOLS:
            # Match the symbol as a whole-word reference, EXCLUDING
            # method-access (`.list_for_client(...)` on a registry
            # object is a different symbol from `src.audit.list_for_client`).
            if re.search(rf"(?<!\.)\b{sym}\b", text):
                if not re.search(rf"from src\.audit import[^\n]*\b{sym}\b", text):
                    # Allow multi-line `from src.audit import (\n  sym, ...)`
                    multiline_block = re.search(
                        r"from src\.audit import \(([^)]+)\)", text
                    )
                    if not (multiline_block and sym in multiline_block.group(1)):
                        raise AssertionError(
                            f"{f.relative_to(_BACKEND)}: symbol {sym!r} is "
                            "referenced but not imported at module top (R5-M6)"
                        )
        for sym in SRL_SYMBOLS:
            if re.search(rf"(?<!\.)\b{sym}\b", text):
                if not re.search(
                    rf"from src\.auth\.signup_rate_limit import[^\n]*\b{sym}\b",
                    text,
                ):
                    multiline_block = re.search(
                        r"from src\.auth\.signup_rate_limit import \(([^)]+)\)",
                        text,
                    )
                    if not (multiline_block and sym in multiline_block.group(1)):
                        raise AssertionError(
                            f"{f.relative_to(_BACKEND)}: symbol {sym!r} is "
                            "referenced but not imported at module top (R5-M6)"
                        )


def test_main_py_compiles():
    """Sanity: main.py must compile."""
    import py_compile
    try:
        py_compile.compile(str(_MAIN), doraise=True)
    except py_compile.PyCompileError as e:
        raise AssertionError(f"src/api/main.py fails to compile: {e}")
