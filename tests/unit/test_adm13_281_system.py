"""ADM-13 (#281) — «Система»: component pings + cron freshness from
pushgateway's push_time_seconds (the same source Prometheus scrapes —
the page can never disagree with alerting)."""
from __future__ import annotations

import inspect
import io
from types import SimpleNamespace

import src.api.routers.ops as ops


def test_guarded_and_no_writes():
    src = inspect.getsource(ops.admin_system)
    assert 'auth.require_role("admin")' in src
    for verb in ("INSERT", "UPDATE", "DELETE"):
        assert verb not in src


def test_parses_push_time_and_flags_stale(monkeypatch):
    metrics = (
        'push_time_seconds{instance="i",job="backup"} 0\n'          # ancient → stale
        'push_time_seconds{instance="i",job="model_age_check"} {now}\n'
        'other_metric{job="x"} 1\n'
    )
    import time
    metrics = metrics.replace("{now}", str(time.time()))
    import urllib.request as rq

    class _Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(rq, "urlopen",
                        lambda url, timeout=5: _Resp(metrics.encode()))
    monkeypatch.setattr(ops, "cached_client_ids", lambda: [])
    import psycopg2
    monkeypatch.setattr(psycopg2, "connect",
                        lambda *a, **k: SimpleNamespace(close=lambda: None))
    import src.redis_pool as rp
    monkeypatch.setattr(rp, "get_redis_or_none", lambda: None)
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")

    out = ops.admin_system(auth=SimpleNamespace(require_role=lambda r: None))
    jobs = {j["job"]: j for j in out["jobs"]}
    assert jobs["backup"]["stale"] is True
    assert jobs["model_age_check"]["stale"] is False
    assert out["jobs"][0]["stale"] is True            # stale sorted first
    assert out["components"]["postgres"] == "ok"
    assert out["components"]["redis"] == "unavailable"


def test_pushgateway_down_degrades_not_500(monkeypatch):
    import urllib.request as rq
    monkeypatch.setattr(rq, "urlopen",
                        lambda url, timeout=5: (_ for _ in ()).throw(OSError("net")))
    monkeypatch.setattr(ops, "cached_client_ids", lambda: [])
    import psycopg2
    monkeypatch.setattr(psycopg2, "connect",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("pg")))
    import src.redis_pool as rp
    monkeypatch.setattr(rp, "get_redis_or_none", lambda: None)
    out = ops.admin_system(auth=SimpleNamespace(require_role=lambda r: None))
    assert out["components"]["pushgateway"].startswith("error")
    assert out["components"]["postgres"].startswith("error")
    assert out["jobs"] == []                          # degraded, not crashed
