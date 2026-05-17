"""
Regression tests for R5-M7 — DI for rate-limit + JWT-revocation
(2026-05-18).

Audit flagged 7+ test monkeypatches on private module attributes
(`signup_rate_limit._redis`, `signup_rate_limit._incr_with_ttl`,
`jwt_auth._redis_client`, `jwt_auth._INMEM_REVOKED_MAX`). These are
DI gaps — production functions reached for an in-module factory
they couldn't get pointed at, so tests had to reach in and swap the
factory by name. That broke as soon as anyone renamed or wrapped
the factory; it also crossed the public/private line.

This commit:
  • Adds `*, redis=_USE_DEFAULT` keyword-only to every public
    function in `signup_rate_limit.py` (8 functions) and
    `jwt_auth.py` (3 functions). Implicit `_USE_DEFAULT` = use the
    canonical pool factory; `None` = explicitly simulate an offline
    Redis (the fail-open code path); a fake Redis-protocol object =
    inject this for the test.
  • Drops the `_INMEM_REVOKED_MAX = int(os.environ.get(...))` module
    constant and reads `settings.jwt_inmem_revoked_max` instead
    (M4 follow-up). Tests override via the public Settings attr.
  • Rewrites 3 test files (`test_r3_phase9_cluster_d.py`,
    `test_r2_phase5_misc.py`, `test_r3_phase8a.py`) to use the DI
    keyword, dropping every monkeypatch of a `_xxx` private name.

These tests pin the invariants so the next contributor can't
accidentally regress the DI shape.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path


_BACKEND = Path(__file__).resolve().parents[2]


def test_signup_rate_limit_public_functions_accept_redis_kwarg():
    """Every public check_*/record_*/assert_* function must take a
    keyword-only `redis` parameter — that's the DI surface."""
    from src.auth import signup_rate_limit as mod

    for name in (
        "check_signup_attempt",
        "check_predict_attempt",
        "check_token_attempt",
        "check_login_attempt",
        "check_otp_verify_attempt",
        "check_rotate_attempt",
        "record_signup_success",
        "assert_signup_allowed",
    ):
        fn = getattr(mod, name)
        sig = inspect.signature(fn)
        assert "redis" in sig.parameters, (
            f"{name} must accept keyword-only `redis` (R5-M7 DI)"
        )
        assert sig.parameters["redis"].kind == inspect.Parameter.KEYWORD_ONLY, (
            f"{name}'s `redis` must be keyword-only — positional would "
            "make production call sites ambiguous"
        )


def test_jwt_auth_revocation_functions_accept_redis_kwarg():
    """Same invariant for jwt_auth — public revoke / is-revoked /
    reset-for-tests must accept `*, redis=...`."""
    from src.auth import jwt_auth

    for name in ("revoke_token", "is_token_revoked", "reset_revocation_set_for_tests"):
        fn = getattr(jwt_auth, name)
        sig = inspect.signature(fn)
        assert "redis" in sig.parameters, (
            f"{name} must accept keyword-only `redis` (R5-M7 DI)"
        )
        assert sig.parameters["redis"].kind == inspect.Parameter.KEYWORD_ONLY


def test_di_passes_through_fake_redis():
    """End-to-end: a fake Redis injected via the public `redis=`
    keyword reaches the rate-limit Lua path. If this fails, the DI
    wiring is broken upstream."""
    from src.auth.signup_rate_limit import check_predict_attempt, RateLimited

    class FakeRedis:
        def __init__(self):
            self.n = 0

        def eval(self, *_a, **_k):
            self.n += 1
            return self.n

        def ttl(self, _key):
            return 1800

    fake = FakeRedis()
    # Limit = 2 — third call must trip RateLimited via the fake.
    check_predict_attempt("acme", 2, redis=fake)
    check_predict_attempt("acme", 2, redis=fake)
    try:
        check_predict_attempt("acme", 2, redis=fake)
        raise AssertionError("third call must raise RateLimited")
    except RateLimited:
        pass
    # Three round-trips, three eval calls — proves the fake was used.
    assert fake.n == 3


def test_di_redis_none_triggers_fail_open():
    """`redis=None` (explicit) must hit the fail-open branch — not
    the default pool. The function returns without raising, leaving
    the test free of Redis state."""
    from src.auth.signup_rate_limit import check_signup_attempt
    # Must not raise — no Redis means no rate-limit enforcement.
    check_signup_attempt("203.0.113.5", redis=None)


def test_jwt_auth_no_inmem_revoked_max_constant():
    """The previous `_INMEM_REVOKED_MAX = int(os.environ.get(...))`
    module constant was removed in M7 — the ceiling is now
    `settings.jwt_inmem_revoked_max` (M4 migration). If the constant
    came back, tests would start monkeypatching it again."""
    from src.auth import jwt_auth
    assert not hasattr(jwt_auth, "_INMEM_REVOKED_MAX"), (
        "_INMEM_REVOKED_MAX was migrated to settings.jwt_inmem_revoked_max "
        "in R5-M7 — re-introducing the module constant would resurrect "
        "the test-monkeypatch-private-attr antipattern"
    )


def test_no_test_monkeypatches_private_redis_factory():
    """The three test files we cleaned up must not reach back into
    private module attrs. Anyone adding a new test that monkeypatches
    `_redis` / `_redis_client` / `_incr_with_ttl` / `_INMEM_REVOKED_MAX`
    will trip this assertion and be guided toward the `redis=` DI."""
    forbidden_patterns = (
        re.compile(r'monkeypatch\.setattr\([^)]*"_redis"'),
        re.compile(r'monkeypatch\.setattr\([^)]*"_redis_client"'),
        re.compile(r'monkeypatch\.setattr\([^)]*"_incr_with_ttl"'),
        re.compile(r'monkeypatch\.setattr\([^)]*"_INMEM_REVOKED_MAX"'),
    )
    test_root = _BACKEND / "tests" / "unit"
    offenders: list[tuple[str, int, str]] = []
    for f in test_root.rglob("*.py"):
        for ln_no, line in enumerate(f.read_text().splitlines(), start=1):
            for pat in forbidden_patterns:
                if pat.search(line):
                    offenders.append((str(f.relative_to(_BACKEND)), ln_no, line.strip()))
    assert not offenders, (
        "R5-M7 forbids monkeypatching private rate-limit/jwt module "
        f"attributes — use the public `redis=` keyword. Offenders:\n"
        + "\n".join(f"  {f}:{ln} — {src}" for f, ln, src in offenders)
    )
