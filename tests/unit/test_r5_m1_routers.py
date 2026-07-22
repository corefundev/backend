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
_EXPECTED_ROUTE_COUNT = 121  # +2 SUP-5 #508 (GET /admin/support/metrics, POST /admin/support/reingest); +2 SUP #507 (GET /support/health, POST /support/chat); +1 AZ-1 #465 (GET order-recommendations); 115 +1 DS-1 slice B (datasets train button); 109 +6 DS-1 #466 (datasets CRUD/attach/detach); 111 (после AUTH-1/2) −2 AUTH-4 #448:
# снесён OTP-вход (/auth/login, /auth/login/verify) — вход только паролем,
# подтверждение регистрации кодом, сброс ссылкой.
# /auth/login/password, /auth/password/reset-request, /auth/password/reset(+peek),
# /auth/password/change. Подтверждение почты — КОДОМ через существующий
# /auth/signup/verify (решение владельца: ссылки только для сброса).
# +1 ADM-v3-1 #386: GET /admin/alerts (Prometheus alert state for the console).
# +2 DP-4b #324: POST /clients/{id}/uploads/{id}/prepare + GET /clients/{id}/uploads/{id}/prep («Подготовка данных» trigger + read-only preview). +3 B4 #156: GET /clients/{id}/export + DELETE /clients/{id} + POST /internal/pii-purge (self-service PII lifecycle). (58 was ADM-6 #259 admin-uploads in src/api/uploads.py — outside this main+routers/ inventory)


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


def test_ops_domain_lives_in_router():
    """The `ops` domain (R5-M1 slice 10 — FINAL slice) — 7 routes —
    lives in `src/api/routers/ops.py`. After this slice, main.py
    contains zero `@app.<method>` route decorators."""
    o = _ROUTERS_DIR / "ops.py"
    assert o.is_file(), "src/api/routers/ops.py must exist after R5-M1 slice 10"
    text = o.read_text()
    assert "router = APIRouter" in text
    for method, path in (
        ('get',  '/health'),
        ('get',  '/healthz'),
        ('get',  '/internal/audit/verify'),
        ('get',  '/internal/state'),
        ('get',  '/readyz'),
        ('get',  '/metrics'),
        ('post', '/clients/{client_id}/reload'),
    ):
        assert f'@router.{method}("{path}"' in text, (
            f"ops.py must own {method.upper()} {path}"
        )

    # POST-SPLIT INVARIANT: main.py has NO route decorators.
    main_text = _MAIN.read_text()
    leftover = re.findall(r'^@app\.(get|post|put|delete|patch)', main_text, re.M)
    assert not leftover, (
        f"M1 complete: main.py must contain ZERO @app.<method> decorators. "
        f"Found: {leftover}"
    )
    assert "ops_router" in main_text


def test_auth_domain_lives_in_router():
    """The `auth` domain (R5-M1 slice 9) — 9 routes + 9 schemas —
    lives in `src/api/routers/auth.py`. Largest slice; preserves
    R2-4 / R3-9 / R4-3 / R4-4 / R5-9 / R6-2 invariants verbatim.
    """
    a = _ROUTERS_DIR / "auth.py"
    assert a.is_file(), "src/api/routers/auth.py must exist after R5-M1 slice 9"
    text = a.read_text()
    assert "router = APIRouter" in text
    for method, path in (
        ('post', '/auth/token'),
        ('post', '/auth/signup'),
        ('post', '/auth/signup/verify'),
        ('post', '/auth/logout'),
        ('get',  '/auth/oauth/providers'),
        ('get',  '/auth/oauth/{provider}/start'),
        ('get',  '/auth/oauth/{provider}/callback'),
    ):
        assert f'@router.{method}("{path}"' in text, (
            f"auth.py must own {method.upper()} {path}"
        )
    # Critical security invariants preserved (text-level pins).
    assert "hmac.compare_digest" in text or "_hmac.compare_digest" in text  # R5-9
    assert "R3-9" in text   # silent 202 on duplicate-email signup
    assert "R4-4" in text   # OTP verify rate-limit
    assert "R6-2" in text   # pydantic protected_namespaces=()
    # M6 hoist preserved.
    assert "from src.audit import" in text
    assert "from src.auth.signup_rate_limit import" in text

    main_text = _MAIN.read_text()
    for path in (
        "/auth/token", "/auth/signup", "/auth/signup/verify",
        "/auth/logout",
        "/auth/oauth/providers",
    ):
        for method in ('get', 'post'):
            assert f'@app.{method}("{path}"' not in main_text, (
                f"main.py still has @app.{method.upper()} for {path}"
            )
    assert "auth_router" in main_text


def test_inference_domain_lives_in_router():
    """The `inference` domain (R5-M1 slice 8) — 5 routes + 3 schemas
    + 1 helper — lives in `src/api/routers/inference.py`. Slice 8
    is the most-prep-dependent slice; verify all supporting modules:
      * src/api/metrics.py — Prometheus counters extraction
      * src/api/loaders.py — load factory extraction
      * src/api/service_cache.py — get_or_load orchestration (slice-8 prep)
    """
    inf_router = _ROUTERS_DIR / "inference.py"
    assert inf_router.is_file(), (
        "src/api/routers/inference.py must exist after R5-M1 slice 8"
    )
    text = inf_router.read_text()
    assert "router = APIRouter" in text
    for method, path in (
        ('get',  '/clients/{client_id}/forecasts'),
        ('get',  '/clients/{client_id}/anomalies'),
        ('get',  '/clients/{client_id}/uploads/{upload_id}/skus'),
        ('post', '/clients/{client_id}/predict'),
        ('post', '/clients/{client_id}/predict/batch'),
    ):
        assert f'@router.{method}("{path}"' in text, (
            f"inference.py must own {method.upper()} {path}"
        )
    # Cache uses public DI surface (not internal _services dict).
    assert "from src.api.service_cache import get_or_load" in text
    assert "from src.api.loaders import" in text
    assert "from src.api.metrics import" in text
    # R5-2 deadlock fix preserved.
    assert "R5-2" in text and "asyncio.gather" in text

    # Supporting modules exist.
    for name in ("metrics.py", "loaders.py", "service_cache.py"):
        assert (_BACKEND / "src" / "api" / name).is_file(), (
            f"src/api/{name} must exist as slice-8 prep"
        )

    main_text = _MAIN.read_text()
    for path in (
        "/clients/{client_id}/predict",
        "/clients/{client_id}/predict/batch",
        "/clients/{client_id}/forecasts",
        "/clients/{client_id}/anomalies",
    ):
        for method in ('get', 'post'):
            assert f'@app.{method}("{path}"' not in main_text, (
                f"main.py still has @app.{method.upper()} for {path}"
            )
    assert "class PredictRequest" not in main_text, (
        "PredictRequest schema must move with the routes"
    )
    assert "_load_processed_for_sku" not in main_text, (
        "_load_processed_for_sku helper must move with the routes"
    )
    assert "inference_router" in main_text


def test_training_domain_lives_in_router():
    """The `training` domain (R5-M1 slice 7) — 3 routes — lives in
    `src/api/routers/training.py`. Also pins:
      * R5-4 quota refund-on-enqueue-fail preserved verbatim.
      * R3-1 job-ownership 404 (not 403) preserved.
      * R4-7 cross-tenant upload_id → 404 (not 403) preserved.
    """
    tr_router = _ROUTERS_DIR / "training.py"
    assert tr_router.is_file(), (
        "src/api/routers/training.py must exist after R5-M1 slice 7"
    )
    text = tr_router.read_text()
    assert "router = APIRouter" in text
    for method, path in (
        ('post', '/clients/{client_id}/train'),
        ('get',  '/jobs/{job_id}'),
        ('get',  '/clients/{client_id}/training-runs'),
    ):
        assert f'@router.{method}("{path}"' in text, (
            f"training.py must own {method.upper()} {path}"
        )
    # R5-4 refund pattern
    assert "R5-4" in text and "refund" in text.lower()
    # R3-1 job ownership
    assert "R3-1" in text and 'detail="job not found"' in text
    # R4-7 anti-enumeration
    assert "R4-7" in text and "404" in text
    # service-cache invalidation goes through public API
    assert "from src.api.service_cache import invalidate" in text

    main_text = _MAIN.read_text()
    for path in (
        "/clients/{client_id}/train",
        "/jobs/{job_id}",
        "/clients/{client_id}/training-runs",
    ):
        for method in ('get', 'post', 'put', 'delete', 'patch'):
            assert f'@app.{method}("{path}"' not in main_text, (
                f"main.py still has @app.{method.upper()} for {path}"
            )
    assert "training_router" in main_text


def test_clients_domain_lives_in_router():
    """The `clients` domain (R5-M1 slice 6) — 5 routes — lives in
    `src/api/routers/clients.py`. Also pins:
      * `_client_to_safe_dict` + `_CLIENT_SAFE_FIELDS` (audit R4-2)
        moved alongside the routes, not left behind in main.py.
      * api-key rotation path keeps the R3-24 rate-limit + audit.
    """
    cl_router = _ROUTERS_DIR / "clients.py"
    assert cl_router.is_file(), (
        "src/api/routers/clients.py must exist after R5-M1 slice 6"
    )
    text = cl_router.read_text()
    assert "router = APIRouter" in text
    for method, path in (
        ('post', '/clients'),
        ('post', '/clients/{client_id}/api-key/rotate'),
        ('get',  '/clients'),
        ('get',  '/clients/{client_id}'),
        ('put',  '/clients/{client_id}'),
    ):
        assert f'@router.{method}("{path}"' in text, (
            f"clients.py must own {method.upper()} {path}"
        )
    # R4-2 safe-fields projection moved alongside (not left in main.py).
    assert "_CLIENT_SAFE_FIELDS" in text and "_client_to_safe_dict" in text
    # R3-24 rate-limit on rotate.
    assert "check_rotate_attempt" in text
    # M6 hoist symbols used from the public surface.
    assert "from src.audit import" in text
    assert "from src.auth.signup_rate_limit import" in text

    main_text = _MAIN.read_text()
    for path in ("/clients", "/clients/{client_id}", "/clients/{client_id}/api-key/rotate"):
        for method in ('get', 'post', 'put', 'delete', 'patch'):
            assert f'@app.{method}("{path}"' not in main_text, (
                f"main.py still has @app.{method.upper()} for {path}"
            )
    assert "_CLIENT_SAFE_FIELDS" not in main_text, (
        "_CLIENT_SAFE_FIELDS must move out of main.py with the routes"
    )
    assert "clients_router" in main_text


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
        assert (
            f'@app.get("{path}"' not in main_text
            and f'@app.post("{path}"' not in main_text
        ), (
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
