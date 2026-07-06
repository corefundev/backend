"""
src/notifications/staleness_nudge.py

NC-3 (#243): model-staleness nudges to the USER — the owner of «your model
is old» is the client, not the ops channel (the per-client ops alert
retires in NC-4 once this ships).

Called daily by the backup container's cron via POST /internal/staleness-
nudge (the R7-2 cron→internal-endpoint pattern — senders live in the app
image, the cron image has only psql/curl).

Semantics:
  - age = newest PROMOTED run (model_path IS NOT NULL — a gate-blocked
    retrain must not look fresh, #248/#268 semantics);
  - thresholds 45d («model_stale_45») and 90d («model_stale_90»), each
    fires ONCE per client — the inbox dedup key is the idempotency: a
    fresh insert (create() -> True) is the only trigger for push;
  - escalation stops after 90d — no infinite nagging;
  - ACTIVITY GATING: email/telegram go only to clients with a successful
    login within ACTIVITY_WINDOW_DAYS (30) — abandoned accounts get the
    silent inbox row only (visible on return, zero push spend);
  - suspended clients are skipped entirely (their access is blocked;
    nudging them to retrain would be absurd);
  - push failures never fail the run (best-effort, NC-1 discipline).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

NUDGE_DAYS = int(os.environ.get("STALE_NUDGE_DAYS", "45"))
ESCALATION_DAYS = int(os.environ.get("STALE_NUDGE_ESCALATION_DAYS", "90"))
ACTIVITY_WINDOW_DAYS = int(os.environ.get("STALE_NUDGE_ACTIVITY_DAYS", "30"))

_WORDING = {
    "model_stale_45": (
        "Прогнозы устаревают",
        "Модель обучена более {days} дней назад — точность прогнозов со "
        "временем снижается. Запустите обучение на свежих данных в разделе "
        "«Обучение».",
    ),
    "model_stale_90": (
        "Модель сильно устарела",
        "Модель не обновлялась более {days} дней. Прогнозы могут заметно "
        "расходиться с реальностью — рекомендуем запустить обучение на "
        "свежих данных.",
    ),
}


def _stale_clients() -> list[dict]:
    """[{client_id, email, suspended, age_days, last_login_days}] for every
    client with a PROMOTED model. Primary reads (decisions, not dashboards)."""
    import psycopg2
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH ages AS (
                    SELECT client_id,
                           EXTRACT(EPOCH FROM (now() - max(ended_at))) / 86400
                               AS age_days
                    FROM sku_training_runs
                    WHERE status = 'finished' AND ended_at IS NOT NULL
                      AND model_path IS NOT NULL
                    GROUP BY client_id
                ), logins AS (
                    SELECT client_id,
                           EXTRACT(EPOCH FROM (now() - max(ts))) / 86400
                               AS last_login_days
                    FROM audit_log
                    WHERE event_type = 'login' AND success
                      AND (event_subtype IS NULL
                           OR event_subtype != 'admin_token_issued')
                    GROUP BY client_id
                )
                SELECT c.client_id, c.email,
                       c.suspended_at IS NOT NULL,
                       a.age_days, l.last_login_days
                FROM sku_clients c
                JOIN ages a USING (client_id)
                LEFT JOIN logins l USING (client_id)
                """
            )
            return [
                {"client_id": r[0], "email": r[1], "suspended": r[2],
                 "age_days": float(r[3]), "last_login_days":
                     float(r[4]) if r[4] is not None else None}
                for r in cur.fetchall()
            ]
    finally:
        conn.close()


def _push(client_id: str, email: str | None, title: str, body: str) -> bool:
    """Best-effort email + telegram. Returns True if anything went out."""
    sent = False
    if email:
        try:
            from src.auth.email_sender import get_email_sender
            get_email_sender().send(
                to=email, subject=f"SKU Forecasting — {title}", body=body)
            sent = True
        except Exception as e:    # noqa: BLE001 — push is best-effort
            logger.warning("staleness email to %s skipped: %s", client_id, e)
    try:
        from src.notifications.telegram import _client_telegram, send_message
        chat_id, opted_in = _client_telegram(client_id)
        if chat_id is not None and opted_in:
            if send_message(chat_id, f"⏳ {title}\n\n{body}"):
                sent = True
    except Exception as e:    # noqa: BLE001
        logger.warning("staleness telegram to %s skipped: %s", client_id, e)
    return sent


def run_staleness_nudge() -> dict:
    from src.storage.notifications import get_notifications_registry

    reg = get_notifications_registry()
    summary = {"checked": 0, "stale": 0, "emitted": 0,
               "pushed": 0, "inactive_silent": 0, "suspended_skipped": 0}

    for c in _stale_clients():
        summary["checked"] += 1
        if c["age_days"] < NUDGE_DAYS:
            continue
        summary["stale"] += 1
        if c["suspended"]:
            summary["suspended_skipped"] += 1
            continue

        key = ("model_stale_90" if c["age_days"] >= ESCALATION_DAYS
               else "model_stale_45")
        title, body_tpl = _WORDING[key]
        body = body_tpl.format(days=NUDGE_DAYS if key.endswith("45")
                               else ESCALATION_DAYS)

        fresh = reg.create(
            c["client_id"], type="model_stale", severity="warning",
            title=title, body=body, dedup_key=key,
        )
        if not fresh:
            continue                      # already nudged at this threshold
        summary["emitted"] += 1

        active = (c["last_login_days"] is not None
                  and c["last_login_days"] <= ACTIVITY_WINDOW_DAYS)
        if not active:
            summary["inactive_silent"] += 1
            logger.info("staleness nudge %s: inactive (login %s d ago) — "
                        "inbox only", c["client_id"], c["last_login_days"])
            continue
        if _push(c["client_id"], c["email"], title, body):
            summary["pushed"] += 1

    logger.info("staleness nudge: %s", summary)
    return {**summary, "at": datetime.now(timezone.utc).isoformat()}
