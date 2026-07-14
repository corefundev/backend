"""
Regression tests for R5 Tier C compose batch (2026-05-17):

R5-M2: `signup_rate_limit.py` collapsed 6 duplicated `check_*_attempt`
       functions into a single `_hour_bucket_check(...)` primitive +
       6 thin wrappers (~100 LOC removed, behaviour unchanged).
R5-M3: `signup_rate_limit._redis` now imports from `src.redis_pool`
       (canonical abstraction) instead of `src.pipeline.task_queue`
       (worker-side concern reaching into HTTP-side auth code).
R5-M9: every monitoring/exporter service now has `mem_limit` + `cpus`
       so an OOM in one container can't kill its host neighbours.
R5-M10: every service has `logging: *log-rotation` so the docker
       json-file driver can't fill the host disk on a chatty
       service.
"""
from __future__ import annotations

from pathlib import Path

import yaml


_BACKEND = Path(__file__).resolve().parents[2]
_COMPOSE = _BACKEND / "docker" / "docker-compose.yml"


def _load_compose():
    # Custom YAML loader that's permissive about !reset / !override
    # tags (compose-only constructs used by minimal.yml overlay).
    class _Loader(yaml.SafeLoader):
        pass
    _Loader.add_constructor("!reset", lambda l, n: None)
    _Loader.add_constructor("!override", lambda l, n: None)
    return yaml.load(_COMPOSE.read_text(), Loader=_Loader)


# ── R5-M2: helper collapse ────────────────────────────────────────────────

def test_signup_rate_limit_has_hour_bucket_helper():
    """The six `check_*_attempt` functions must delegate to a single
    `_hour_bucket_check` private helper (no per-function copy of the
    redis-or-fail-open / incr / raise shape)."""
    text = (_BACKEND / "src" / "auth" / "signup_rate_limit.py").read_text()
    assert "def _hour_bucket_check(" in text, (
        "must declare _hour_bucket_check primitive (R5-M2)"
    )
    # Each public check_*_attempt must call it.
    for fn in ("check_signup_attempt", "check_predict_attempt",
               "check_token_attempt", "check_login_attempt",
               "check_otp_verify_attempt", "check_rotate_attempt"):
        body_start = text.find(f"def {fn}(")
        assert body_start > 0, f"{fn} must still exist"
        # Find function body extent (next `def ` or end-of-file).
        next_def = text.find("\ndef ", body_start + 1)
        body = text[body_start:next_def] if next_def > 0 else text[body_start:]
        assert "_hour_bucket_check(" in body, (
            f"{fn} must delegate to _hour_bucket_check (R5-M2)"
        )


def test_signup_rate_limit_no_duplicate_incr_with_ttl_calls():
    """After M2 collapse, the six 1-hour-TTL `check_*_attempt`
    wrappers must not call `_incr_with_ttl` directly — that logic
    moved into `_hour_bucket_check`. Allow ≤3 callsites: one in the
    helper itself, one in `record_signup_success` (24-hour
    daily-success counter) and one in `record_login_failure`
    (AUTH-5 #454 — LOGIN_LOCKOUT_WINDOW_MIN-scoped failure counter
    for the captcha wall). Both are genuinely different TTL shapes,
    not part of the hourly-attempt collapse."""
    text = (_BACKEND / "src" / "auth" / "signup_rate_limit.py").read_text()
    callsites = sum(
        1 for line in text.splitlines()
        if "_incr_with_ttl(" in line and not line.lstrip().startswith(("def ", "#"))
    )
    assert callsites <= 3, (
        f"_incr_with_ttl callsites should be ≤3 (helper + daily-success "
        f"counter + login-failure counter). Got {callsites} — M2 "
        "hourly-attempt duplication re-emerging?"
    )


# ── R5-M3: redis abstraction via redis_pool, not task_queue ──────────────

def test_signup_rate_limit_uses_redis_pool_not_task_queue():
    """`_redis()` must source the connection via `src.redis_pool`, not
    by reaching into `src.pipeline.task_queue` (cross-layer coupling
    flagged by R5-M3)."""
    text = (_BACKEND / "src" / "auth" / "signup_rate_limit.py").read_text()
    # Find executable lines only (skip comments).
    executable = "\n".join(
        line for line in text.splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "from src.redis_pool import get_redis_or_none" in executable, (
        "_redis() must import from src.redis_pool (R5-M3)"
    )
    assert "from src.pipeline.task_queue import get_redis_connection" not in executable, (
        "_redis() must not reach into src.pipeline.task_queue (R5-M3)"
    )


# ── R5-M9: mem_limit + cpus on monitoring services ───────────────────────

def test_monitoring_services_have_mem_limit():
    """Each monitoring/exporter service must declare mem_limit + cpus
    so an OOM in one container can't kill its host neighbours."""
    compose = _load_compose()
    services = compose.get("services", {})
    required = (
        "postgres-exporter", "pushgateway", "prometheus", "tempo", "loki",
        "promtail", "grafana", "backup", "alertmanager", "ssl-exporter",
        "rq-exporter",
    )
    missing_mem = []
    missing_cpus = []
    for svc in required:
        cfg = services.get(svc) or {}
        if "mem_limit" not in cfg:
            missing_mem.append(svc)
        if "cpus" not in cfg:
            missing_cpus.append(svc)
    assert not missing_mem, (
        f"these monitoring services lack mem_limit (R5-M9): {missing_mem}"
    )
    assert not missing_cpus, (
        f"these monitoring services lack cpus (R5-M9): {missing_cpus}"
    )


# ── R5-M10: logging anchor on all services ────────────────────────────────

def test_all_services_have_logging_anchor():
    """Every service in the prod compose stack must have `logging:
    *log-rotation` (or its own logging block) so docker's json-file
    driver can't fill the host disk on a chatty service."""
    compose = _load_compose()
    services = compose.get("services", {})
    # Services excluded from the check:
    #   - sandbox-image used to be a one-shot builder; removed in R4-1.
    #   - airflow stack is profile-gated; if it ever runs, that's a
    #     follow-up.
    EXCLUDE = {"sandbox-image", "airflow-init", "airflow-scheduler",
               "airflow-webserver", "minio", "minio-init"}
    missing = []
    for name, cfg in services.items():
        if name in EXCLUDE:
            continue
        if not isinstance(cfg, dict):
            continue
        if "logging" not in cfg:
            missing.append(name)
    assert not missing, (
        f"these services lack a `logging:` block (R5-M10): {missing}"
    )
