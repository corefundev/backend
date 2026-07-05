"""
src/pipeline/reconcile_runs.py

#265 — abandoned-training self-heal, run at training-worker startup.

A worker recreate (deploy, crash, OOM, host reboot) kills the in-flight RQ
job with ~10s of SIGTERM grace — nowhere near a 40-70 min training. The run
row then sticks in 'running' forever, which also trips the single-in-flight
guard (R11-H4) and locks the client out of retraining until manual psql
repair. Live case 2026-07-05: run 0fe548ee killed by a docs-only deploy.

At startup (before `rq worker` takes the queue) this scans 'running' rows
and heals every one whose RQ job is demonstrably dead:
  job missing from Redis            → healed (Redis lost it / janitor GC)
  job status failed/stopped/canceled → healed (AbandonedJobError landed)
  job status queued/started/deferred → SKIPPED (a live worker owns it —
                                       safe under future multi-worker)
Healing = status 'failed' + honest error + inbox row + email/telegram
(best-effort, NC-1 discipline). NO auto-requeue by design of record:
a silent re-train spends client resources without a client action, and a
crash-caused abandonment would likely just crash again — the notification
asks the client to re-trigger instead.

Never blocks worker startup: any failure here logs loudly and exits 0 —
a worker that can't reconcile must still serve the queue (the next restart
retries), otherwise a DB blip turns into a total training outage.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_DEAD_JOB_STATUSES = {"failed", "stopped", "canceled"}
_ERROR_TEXT = (
    "abandoned: training worker restarted mid-run (deploy/crash) — "
    "run released automatically (#265)"
)


def _job_is_dead(job_id: str | None) -> bool:
    """True when the RQ job for this run demonstrably no longer runs."""
    if not job_id:
        # No job handle at all (legacy/sync rows) — a 'running' row with no
        # job cannot be owned by any worker at OUR startup moment.
        return True
    try:
        from rq.job import Job
        from src.pipeline.task_queue import get_redis_connection
        job = Job.fetch(job_id, connection=get_redis_connection())
    except Exception as e:    # noqa: BLE001 — NoSuchJobError et al.
        logger.info("reconcile: job %s not fetchable (%s) — treating as dead",
                    job_id, e)
        return True
    status = str(job.get_status(refresh=True) or "").lower()
    return status in _DEAD_JOB_STATUSES


def reconcile_abandoned_runs() -> int:
    """Heal orphaned 'running' rows; returns how many were healed."""
    from datetime import datetime, timezone

    from src.storage.training_runs import get_training_runs_registry

    reg = get_training_runs_registry()
    stale = reg.list_running()
    healed = 0
    for rec in stale:
        if not _job_is_dead(rec.job_id):
            logger.info("reconcile: run %s job %s still alive — skipping",
                        rec.run_id, rec.job_id)
            continue
        reg.update(
            rec.run_id,
            status="failed",
            ended_at=datetime.now(timezone.utc).isoformat(),
            error=_ERROR_TEXT,
        )
        healed += 1
        logger.warning("reconcile: healed abandoned run %s (client=%s)",
                       rec.run_id, rec.client_id)
        try:
            from src.storage.notifications import emit_notification
            emit_notification(
                rec.client_id, type="training_failed", severity="error",
                title="Обучение модели было прервано",
                body="Обновление сервиса прервало обучение. Пожалуйста, "
                     "запустите обучение ещё раз — ограничение на повторный "
                     "запуск снято автоматически.",
                dedup_key=f"training_failed:{rec.run_id}",
            )
            for dotted in ("src.notifications.training_email",
                           "src.notifications.telegram"):
                try:
                    module = __import__(dotted, fromlist=["notify_training_failed"])
                    module.notify_training_failed(
                        client_id=rec.client_id,
                        error="прервано обновлением сервиса",
                    )
                except Exception as notif_err:    # noqa: BLE001
                    logger.info("reconcile notify (%s) skipped: %s",
                                dotted, notif_err)
        except Exception as e:    # noqa: BLE001 — void best-effort
            logger.info("reconcile notifications skipped: %s", e)
    return healed


def main() -> None:
    try:
        # Lockbox runtime-injection: DATABASE_URL / SMTP / telegram creds are
        # NOT in the container env at startup — every prod entrypoint script
        # hydrates them first (vault_agent pattern). The 2026-07-05 staged
        # drill caught reconcile skipping this: registry init failed with
        # "DATABASE_URL not set" and the heal was a loud no-op.
        from src.auth.vault_agent import bootstrap_secrets
        bootstrap_secrets()
        n = reconcile_abandoned_runs()
        logger.info("reconcile_runs: %d abandoned run(s) healed", n)
    except Exception as e:    # noqa: BLE001 — must NEVER block worker start
        logger.error("reconcile_runs FAILED (worker starts anyway; next "
                     "restart retries): %s", e)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    main()
