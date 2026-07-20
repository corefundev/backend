"""
#265 — deploy gate + abandoned-run self-heal.

A worker recreate kills an in-flight training (~10s SIGTERM grace vs a
40-70 min job) and the run row sticks in 'running', locking the client out
(R11-H4). Two defences: cd_deploy.sh WAITS for the queue to drain before
recreating workers (timeout = loud deploy failure, nothing recreated), and
the worker heals orphaned rows at startup BEFORE taking the queue.
No auto-requeue by design of record: a silent re-train spends client
resources without a client action; the notification asks to re-trigger.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import src.pipeline.reconcile_runs as rr


def _rec(run_id="r1", job_id="j1", client_id="acme"):
    return SimpleNamespace(run_id=run_id, job_id=job_id, client_id=client_id)


def _registry(monkeypatch, running, updates):
    import src.storage.training_runs as truns
    monkeypatch.setattr(truns, "get_training_runs_registry", lambda: SimpleNamespace(
        list_running=lambda: running,
        update=lambda run_id, **f: updates.append((run_id, f)),
    ))


def test_dead_job_is_healed_with_notification(monkeypatch):
    updates, emitted = [], []
    _registry(monkeypatch, [_rec()], updates)
    monkeypatch.setattr(rr, "_job_is_dead", lambda *a, **k: True)
    import src.storage.notifications as ns
    monkeypatch.setattr(ns, "emit_notification",
                        lambda *a, **k: emitted.append(k.get("type") or a[1]))
    healed = rr.reconcile_abandoned_runs()
    assert healed == 1
    run_id, fields = updates[0]
    assert run_id == "r1" and fields["status"] == "failed"
    assert "#265" in fields["error"]
    assert emitted and emitted[0] == "training_failed"


def test_live_job_is_skipped(monkeypatch):
    updates = []
    _registry(monkeypatch, [_rec()], updates)
    monkeypatch.setattr(rr, "_job_is_dead", lambda *a, **k: False)
    assert rr.reconcile_abandoned_runs() == 0
    assert updates == []


def test_missing_job_id_counts_as_dead():
    assert rr._job_is_dead(None) is True
    assert rr._job_is_dead("") is True


def test_unfetchable_job_is_not_dead_anymore(monkeypatch):
    # #557 инвертировал прежнюю семантику: сбой Redis = смерть НЕ доказана
    # → не убивать (старое «unfetchable → dead» убило живой ран кнопкой).
    import src.pipeline.task_queue as tq
    monkeypatch.setattr(tq, "get_redis_connection",
                        lambda: (_ for _ in ()).throw(ConnectionError("redis down")))
    assert rr._job_is_dead("some-job") is False


def test_557_no_such_job_is_dead(monkeypatch):
    import src.pipeline.task_queue as tq
    from rq.exceptions import NoSuchJobError
    monkeypatch.setattr(tq, "get_redis_connection", lambda: object())
    monkeypatch.setattr("rq.job.Job.fetch",
                        classmethod(lambda cls, jid, connection=None:
                                    (_ for _ in ()).throw(NoSuchJobError(jid))))
    assert rr._job_is_dead("gone-job") is True


def test_main_never_raises(monkeypatch):
    monkeypatch.setattr(rr, "reconcile_abandoned_runs",
                        lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    rr.main()   # must not raise — a worker that can't reconcile still serves


def test_notification_failure_does_not_stop_healing(monkeypatch):
    updates = []
    _registry(monkeypatch, [_rec("r1"), _rec("r2", job_id=None)], updates)
    monkeypatch.setattr(rr, "_job_is_dead", lambda *a, **k: True)
    import src.storage.notifications as ns
    monkeypatch.setattr(ns, "emit_notification",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("pg down")))
    assert rr.reconcile_abandoned_runs() == 2
    assert len(updates) == 2


# ── wiring pins ───────────────────────────────────────────────────────────────

def test_cd_deploy_gate_before_worker_recreate():
    sh = Path("scripts/cd_deploy.sh").read_text()
    i_gate = sh.index("#265 deploy gate")
    i_prod = sh.index('rolling: workers first')
    assert i_gate < i_prod, "the gate must run BEFORE any worker recreate"
    assert "TRAINING_WAIT_MAX" in sh and "exit 1" in sh[i_gate:i_prod]


def test_worker_command_runs_reconcile_first():
    import yaml
    d = yaml.safe_load(Path("docker/docker-compose.yml").read_text())
    cmd = d["services"]["worker"]["command"]
    cmd_s = cmd if isinstance(cmd, str) else " ".join(cmd)
    assert "reconcile_runs" in cmd_s and "exec rq worker sku-training" in cmd_s
    assert cmd_s.index("reconcile_runs") < cmd_s.index("rq worker")


def test_registry_list_running_reads_primary():
    import inspect
    from src.storage.training_runs import PostgresTrainingRunsRegistry
    src = inspect.getsource(PostgresTrainingRunsRegistry.list_running)
    assert "self._conn()" in src and "_conn_read" not in src, (
        "healing decisions must not act on replica lag"
    )


def test_main_bootstraps_secrets_before_reconcile(monkeypatch):
    # Lockbox runtime-injection: DATABASE_URL is absent from the container
    # env at startup — reconcile must hydrate secrets first (drill finding,
    # 2026-07-05: registry init failed with "DATABASE_URL not set").
    order = []
    import src.auth.vault_agent as va
    monkeypatch.setattr(va, "bootstrap_secrets", lambda: order.append("boot"))
    monkeypatch.setattr(rr, "reconcile_abandoned_runs",
                        lambda: order.append("reconcile") or 0)
    rr.main()
    assert order == ["boot", "reconcile"]


def test_main_survives_bootstrap_failure(monkeypatch):
    import src.auth.vault_agent as va
    monkeypatch.setattr(va, "bootstrap_secrets",
                        lambda: (_ for _ in ()).throw(RuntimeError("lockbox down")))
    rr.main()   # loud skip, no raise — worker must still start


# ── #557: доказанная смерть, боевой случай кнопки Reconcile ────────────────

def test_557_admin_context_skips_rows_without_job_id(monkeypatch):
    # Строка running БЕЗ job_id: на boot — мёртвая; в admin — непроверяемая,
    # НЕ убивать (владелец нажал кнопку через 30с после старта обучения —
    # живой ран был помечен abandoned).
    from types import SimpleNamespace
    import src.pipeline.reconcile_runs as rr
    rec = SimpleNamespace(run_id="r1", job_id=None, client_id="c")
    updates = []
    monkeypatch.setattr(
        "src.storage.training_runs.get_training_runs_registry",
        lambda: SimpleNamespace(list_running=lambda: [rec],
                                update=lambda rid, **kw: updates.append(rid)))
    monkeypatch.setattr(rr, "_notify_abandoned", lambda *a, **k: None, raising=False)
    assert rr.reconcile_abandoned_runs(context="admin") == 0
    assert updates == []
    assert rr.reconcile_abandoned_runs(context="boot") == 1


def test_557_unprovable_liveness_is_not_dead(monkeypatch):
    # Redis недоступен → «не докажешь смерть — не убивай» в ОБОИХ контекстах.
    import src.pipeline.reconcile_runs as rr
    monkeypatch.setattr(
        "src.pipeline.task_queue.get_redis_connection",
        lambda: (_ for _ in ()).throw(ConnectionError("redis down")))
    assert rr._job_is_dead("job-1", context="admin") is False
    assert rr._job_is_dead("job-1", context="boot") is False


def test_557_enqueue_persists_job_id():
    # Персист handle — предпосылка проверяемости; статический гвард.
    from pathlib import Path
    src = Path("src/pipeline/task_queue.py").read_text()
    assert "get_training_runs_registry().update(run_id, job_id=job.id)" in src


def test_557_admin_endpoint_passes_context():
    from pathlib import Path
    src = Path("src/api/routers/training.py").read_text()
    assert 'reconcile_abandoned_runs(context="admin")' in src
