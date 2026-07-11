"""ADM-v3-4 (#389) — застрявшие тренировки: порог для FE-бейджа +
ручной reconcile из консоли.

Контракты:
  • /admin/training-runs несёт stuck_threshold_min (единый источник
    порога для FE — никакой рассинхрон бейджа с backend'ом);
  • POST /admin/training/reconcile дергает РОВНО reconcile_abandoned_runs
    (#265 — общий путь со startup-hook'ом воркера, живые джобы скипаются
    внутри), возвращает счётчик, аудируется с актором (AUD-7);
  • fail-CLOSED: сбой reconcile → 503 (операторское действие обязано
    показывать ошибку, в отличие от never-block startup-пути).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import src.api.routers.training as tr


def _http():
    return SimpleNamespace(client=SimpleNamespace(host="1.2.3.4"),
                           headers={"user-agent": "t"})


def _auth():
    return SimpleNamespace(require_role=lambda r: None, client_id="admin-ops",
                           auth_method="jwt", jti="j" * 32)


def test_threshold_surfaced_and_sane():
    assert isinstance(tr.STUCK_THRESHOLD_MIN, int)
    # тренировки легитимно идут 40-70 мин (ensemble) — порог обязан быть выше
    assert 70 < tr.STUCK_THRESHOLD_MIN <= 240
    import inspect
    src = inspect.getsource(tr.admin_training_runs)
    assert "stuck_threshold_min" in src, (
        "порог обязан приезжать в ответе /admin/training-runs — FE не должен "
        "хардкодить свой"
    )


def test_reconcile_heals_and_audits_actor(monkeypatch):
    events = []
    monkeypatch.setattr(tr, "record_event", lambda **kw: events.append(kw))
    monkeypatch.setattr(
        "src.pipeline.reconcile_runs.reconcile_abandoned_runs", lambda: 3)
    out = tr.admin_training_reconcile(_http(), auth=_auth())
    assert out == {"healed": 3}
    e = events[0]
    assert e["event_subtype"] == "training_reconcile"
    assert e["metadata"]["healed"] == 3
    assert e["metadata"]["actor_client_id"] == "admin-ops"
    assert e["metadata"]["actor_jti"] == "j" * 32


def test_reconcile_failure_is_503_and_no_audit(monkeypatch):
    events = []
    monkeypatch.setattr(tr, "record_event", lambda **kw: events.append(kw))

    def boom():
        raise RuntimeError("redis down")
    monkeypatch.setattr(
        "src.pipeline.reconcile_runs.reconcile_abandoned_runs", boom)
    with pytest.raises(HTTPException) as ei:
        tr.admin_training_reconcile(_http(), auth=_auth())
    assert ei.value.status_code == 503
    assert events == []  # не аудируем то, чего не случилось


def test_manual_path_is_the_shared_265_mechanism():
    import inspect
    src = inspect.getsource(tr.admin_training_reconcile)
    assert "reconcile_abandoned_runs" in src, (
        "ручной reconcile обязан идти через #265-механизм — иначе пути "
        "startup-hook'а и кнопки разойдутся"
    )
