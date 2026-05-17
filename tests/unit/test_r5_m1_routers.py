"""
Regression tests for R5-M1 — god-module `src/api/main.py` split into
per-domain routers (2026-05-18).

Incremental: each commit extracts one domain (legal → audit →
notifications → plans → config → clients → training → inference →
auth → ops) into `src/api/routers/<domain>.py`. main.py keeps only
lifespan, middleware, Prometheus counters, and `app.include_router`
calls.

These tests pin two invariants that protect the split:
  1. **Inventory invariant** — the total route-path inventory
     (every `(method, path)` decorated by `@app.<method>` or
     `@router.<method>` anywhere) must equal 44 — the number FastAPI
     exposed before the split started. If a route is dropped or
     duplicated mid-split, this catches it before merge.
  2. **No re-introduction** — once a domain is extracted, main.py
     must NOT contain `@app.<method>(...)` for any path in that
     domain. The router-file owns it.
"""
from __future__ import annotations

import re
from pathlib import Path


_BACKEND = Path(__file__).resolve().parents[2]
_MAIN = _BACKEND / "src" / "api" / "main.py"
_ROUTERS_DIR = _BACKEND / "src" / "api" / "routers"

_ROUTE_DECORATOR = re.compile(
    r'^@(app|router)\.(get|post|put|delete|patch|websocket)\("([^"]+)"',
    re.MULTILINE,
)

# Pre-split inventory: 44 routes (verified by `grep -c '^@app\.' `
# on commit c00b0d8). Every extraction commit must preserve this
# count exactly — no drops, no phantom additions.
_EXPECTED_ROUTE_COUNT = 44


def _collect_routes() -> list[tuple[str, str, str]]:
    """Return [(file_relative, method, path)] across main.py + all
    routers/*.py."""
    found: list[tuple[str, str, str]] = []
    for f in (_MAIN, *_ROUTERS_DIR.glob("*.py")):
        if f.name == "__init__.py":
            continue
        for m in _ROUTE_DECORATOR.finditer(f.read_text()):
            method, path = m.group(2), m.group(3)
            found.append((str(f.relative_to(_BACKEND)), method.upper(), path))
    return found


def test_route_inventory_unchanged():
    """Total (method, path) count across main.py + routers/ must
    equal the pre-split count. Drops or duplicates fail here before
    merge — much louder than discovering at runtime in prod that
    /clients/{id}/predict returns 404."""
    routes = _collect_routes()
    paths = [(m, p) for _, m, p in routes]
    assert len(paths) == _EXPECTED_ROUTE_COUNT, (
        f"route count drifted from {_EXPECTED_ROUTE_COUNT} to "
        f"{len(paths)} during R5-M1 split"
    )
    # No duplicates — a route either lives in main.py OR in one
    # router file, never both.
    assert len(set(paths)) == len(paths), (
        f"duplicate routes detected (router-file double-registration?): "
        f"{[p for p in paths if paths.count(p) > 1]}"
    )


def test_routers_package_exists():
    """Domain-router package must exist as the home for extracted
    handlers."""
    assert _ROUTERS_DIR.is_dir(), "src/api/routers/ must exist (R5-M1)"
    assert (_ROUTERS_DIR / "__init__.py").is_file(), (
        "src/api/routers/__init__.py must exist"
    )


def test_legal_domain_lives_in_router():
    """The `legal` domain (R5-M1 slice 1) must live entirely in
    `src/api/routers/legal.py`, not main.py."""
    legal_router = _ROUTERS_DIR / "legal.py"
    assert legal_router.is_file(), (
        "src/api/routers/legal.py must exist after R5-M1 slice 1"
    )
    text = legal_router.read_text()
    assert "router = APIRouter" in text, (
        "legal.py must declare `router = APIRouter(...)`"
    )
    for path_pattern in (r'/legal/\{doc_id\}', r'/admin/legal/\{doc_id\}'):
        assert re.search(
            r'@router\.(get|put)\("' + path_pattern.replace("\\", "\\"),
            text,
        ), f"{path_pattern} must be registered in legal.py"

    # And main.py must NOT re-register them.
    main_text = _MAIN.read_text()
    for path in ("/legal/{doc_id}", "/admin/legal/{doc_id}"):
        assert f'@app.get("{path}"' not in main_text, (
            f"main.py still has @app.get for {path} — must be in legal.py"
        )
        assert f'@app.put("{path}"' not in main_text, (
            f"main.py still has @app.put for {path} — must be in legal.py"
        )


def test_legal_router_registered_in_main():
    """main.py must `include_router` the legal router."""
    text = _MAIN.read_text()
    assert "from src.api.routers.legal import router as legal_router" in text, (
        "main.py must import legal_router"
    )
    assert "app.include_router(legal_router)" in text, (
        "main.py must register legal_router via include_router"
    )


def test_config_domain_lives_in_router():
    """The `config` domain (R5-M1 slice 5) — 5 routes — lives in
    `src/api/routers/config.py`. Slice-5 also requires the
    service-cache prep (src/api/service_cache.py) to be present so
    that the router can invalidate via the public API."""
    cfg_router = _ROUTERS_DIR / "config.py"
    assert cfg_router.is_file(), (
        "src/api/routers/config.py must exist after R5-M1 slice 5"
    )
    text = cfg_router.read_text()
    assert "router = APIRouter" in text
    for method, path in (
        ('get',    '/clients/{client_id}/config'),
        ('put',    '/clients/{client_id}/config'),
        ('patch',  '/clients/{client_id}/config'),
        ('delete', '/clients/{client_id}/config'),
        ('get',    '/system/config'),
    ):
        assert f'@router.{method}("{path}"' in text, (
            f"config.py must own {method.upper()} {path}"
        )
    # Cache-invalidation goes through the public API (slice-5 prep).
    assert "from src.api.service_cache import invalidate" in text, (
        "config.py must use service_cache.invalidate, not reach into "
        "main.py for `_services.pop`"
    )

    main_text = _MAIN.read_text()
    for path in ("/clients/{client_id}/config", "/system/config"):
        for method in ('get', 'put', 'patch', 'delete'):
            assert f'@app.{method}("{path}"' not in main_text, (
                f"main.py still has @app.{method.upper()} for {path} — "
                "must be in config.py"
            )
    assert "config_router" in main_text
    assert (_BACKEND / "src" / "api" / "service_cache.py").is_file(), (
        "src/api/service_cache.py must exist (slice-5 prep)"
    )


def test_plans_domain_lives_in_router():
    """The `plans` domain (R5-M1 slice 4) — 3 routes — lives in
    `src/api/routers/plans.py`."""
    plans_router = _ROUTERS_DIR / "plans.py"
    assert plans_router.is_file(), (
        "src/api/routers/plans.py must exist after R5-M1 slice 4"
    )
    text = plans_router.read_text()
    assert "router = APIRouter" in text
    for method, path in (
        ('get',  '/plans'),
        ('get',  '/clients/{client_id}/usage'),
        ('post', '/clients/{client_id}/upgrade'),
    ):
        assert f'@router.{method}("{path}"' in text, (
            f"plans.py must own {method.upper()} {path}"
        )

    main_text = _MAIN.read_text()
    for path in ("/plans", "/clients/{client_id}/usage", "/clients/{client_id}/upgrade"):
        assert f'@app.get("{path}"' not in main_text and \
               f'@app.post("{path}"' not in main_text, (
            f"main.py still has a @app handler for {path} — must be in plans.py"
        )
    assert "plans_router" in main_text


def test_notifications_domain_lives_in_router():
    """The `notifications` domain (R5-M1 slice 3) — 4 routes — must
    live in `src/api/routers/notifications.py`, not main.py."""
    notif_router = _ROUTERS_DIR / "notifications.py"
    assert notif_router.is_file(), (
        "src/api/routers/notifications.py must exist after R5-M1 slice 3"
    )
    text = notif_router.read_text()
    assert "router = APIRouter" in text
    for path_method in (
        ('post', '/clients/{client_id}/telegram/link-token'),
        ('delete', '/clients/{client_id}/telegram'),
        ('get', '/clients/{client_id}/telegram'),
        ('post', '/telegram/webhook'),
    ):
        method, path = path_method
        assert f'@router.{method}("{path}"' in text, (
            f"notifications.py must own {method.upper()} {path}"
        )

    main_text = _MAIN.read_text()
    for path in (
        "/clients/{client_id}/telegram/link-token",
        "/telegram/webhook",
    ):
        assert f'"{path}"' not in main_text, (
            f"main.py still references {path} — must be in notifications.py"
        )
    assert "notifications_router" in main_text


def test_audit_domain_lives_in_router():
    """The `audit` domain (R5-M1 slice 2) must live in
    `src/api/routers/audit.py`, not main.py."""
    audit_router = _ROUTERS_DIR / "audit.py"
    assert audit_router.is_file(), (
        "src/api/routers/audit.py must exist after R5-M1 slice 2"
    )
    text = audit_router.read_text()
    assert "router = APIRouter" in text, "audit.py must declare APIRouter"
    assert '@router.get("/clients/{client_id}/audit"' in text, (
        "audit.py must own GET /clients/{client_id}/audit"
    )

    main_text = _MAIN.read_text()
    assert '@app.get("/clients/{client_id}/audit"' not in main_text, (
        "main.py still has @app.get for /clients/{client_id}/audit — "
        "must be in audit.py"
    )
    assert "from src.api.routers.audit import router as audit_router" in main_text
    assert "app.include_router(audit_router)" in main_text


def test_main_py_compiles():
    """main.py must still compile after the extraction — incomplete
    decorator removal or dangling import leaves a SyntaxError that
    only the next deploy would catch."""
    import py_compile
    try:
        py_compile.compile(str(_MAIN), doraise=True)
    except py_compile.PyCompileError as e:
        raise AssertionError(f"src/api/main.py fails to compile: {e}")
