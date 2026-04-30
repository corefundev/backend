"""
src/notifications/training_email.py

Send an email when a training run reaches a terminal state. Reuses
the EmailSender abstraction from src.auth.email_sender (already
configured for Beget SMTP via Lockbox secrets), so all transport
concerns are inherited.

Best-effort: any failure here is logged at WARNING and swallowed —
notifications must never block or fail a training run.

Per-client opt-out lives in client config under
  notifications.email.training_complete: bool (default true)
Future: per-status filter (notify on failed only / never / always).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


def _format_duration(sec: float | None) -> str:
    if sec is None or sec < 0:
        return "—"
    m = int(sec // 60)
    s = int(round(sec - m * 60))
    return f"{m} мин {s:02d} сек" if m > 0 else f"{s} сек"


def _format_pct(v: float | None, digits: int = 1) -> str:
    if v is None:
        return "—"
    return f"{v * 100:.{digits}f}%"


def _format_num(v: float | None, digits: int = 3) -> str:
    if v is None:
        return "—"
    return f"{v:.{digits}f}"


def _client_email(client_id: str) -> Optional[str]:
    """
    Return the contact email for the client, or None if not set.
    Lookup via client registry — single source of truth.
    """
    try:
        from src.clients.registry import get_registry
        rec = get_registry().get(client_id)
        if rec is None:
            return None
        return getattr(rec, "email", None) or None
    except Exception as e:    # noqa: BLE001
        logger.warning("client lookup failed for %s: %s", client_id, e)
        return None


def _opted_in(client_id: str) -> bool:
    """
    Per-client opt-in check. Default ON. Reads
    `notifications.email.training_complete` from client config.
    """
    try:
        from src.clients.registry import get_registry
        rec = get_registry().get(client_id)
        cfg = (rec.config if rec else None) or {}
        return bool(
            (cfg.get("notifications") or {})
            .get("email", {})
            .get("training_complete", True)
        )
    except Exception:    # noqa: BLE001
        return True


def _frontend_base_url() -> str:
    return os.environ.get("FRONTEND_URL", "https://skusystem.ru").rstrip("/")


def _render_finished(
    client_id:    str,
    duration_sec: float | None,
    n_skus:       int | None,
    wmape:        float | None,
    mase:         float | None,
) -> tuple[str, str]:
    """Returns (subject, body) for a successful training run."""
    base = _frontend_base_url()
    subject = "✓ Обучение модели завершено"
    body_lines = [
        f"Здравствуйте,",
        "",
        f"Обучение модели для аккаунта «{client_id}» успешно завершено.",
        "",
        f"  • Длительность:       {_format_duration(duration_sec)}",
        f"  • Товаров (SKU):      {n_skus if n_skus is not None else '—'}",
        f"  • WMAPE (точность):   {_format_pct(wmape)}  — чем меньше, тем точнее",
        f"  • MASE:               {_format_num(mase)}  — < 1 значит лучше базового",
        "",
        f"Прогноз для всех SKU уже посчитан и доступен:",
        f"  {base}/app/forecasts",
        "",
        f"История запусков и подробности обучения:",
        f"  {base}/app/training",
        "",
        "— SKU Forecasting",
    ]
    return subject, "\n".join(body_lines)


def _render_failed(client_id: str, error: str) -> tuple[str, str]:
    base = _frontend_base_url()
    subject = "⚠ Обучение модели не удалось"
    body_lines = [
        f"Здравствуйте,",
        "",
        f"Обучение модели для аккаунта «{client_id}» завершилось с ошибкой.",
        "",
        f"Сообщение системы:",
        f"  {error[:500]}",
        "",
        "Что попробовать:",
        f"  • Проверить, что в загруженном CSV нет пропусков в колонках date / sales",
        f"  • Убедиться что период данных не меньше 30 дней",
        f"  • Запустить обучение ещё раз: {base}/app/training",
        "",
        "Если ошибка повторяется — напишите в поддержку, приложив текст выше.",
        "",
        "— SKU Forecasting",
    ]
    return subject, "\n".join(body_lines)


def notify_training_finished(
    client_id:    str,
    duration_sec: float | None = None,
    n_skus:       int | None   = None,
    wmape:        float | None = None,
    mase:         float | None = None,
) -> None:
    """Best-effort email on successful training. Never raises."""
    try:
        if not _opted_in(client_id):
            logger.info("training notification skipped (opted out): %s", client_id)
            return
        to = _client_email(client_id)
        if not to:
            logger.info("training notification skipped (no email): %s", client_id)
            return
        subject, body = _render_finished(client_id, duration_sec, n_skus, wmape, mase)
        from src.auth.email_sender import get_email_sender
        get_email_sender().send(to=to, subject=subject, body=body)
        logger.info("training-complete email sent → %s (client=%s)", to, client_id)
    except Exception as e:    # noqa: BLE001
        logger.warning("training-complete email failed (client=%s): %s", client_id, e)


def notify_training_failed(client_id: str, error: str) -> None:
    """Best-effort email on failed training. Never raises."""
    try:
        if not _opted_in(client_id):
            return
        to = _client_email(client_id)
        if not to:
            return
        subject, body = _render_failed(client_id, error)
        from src.auth.email_sender import get_email_sender
        get_email_sender().send(to=to, subject=subject, body=body)
        logger.info("training-failed email sent → %s (client=%s)", to, client_id)
    except Exception as e:    # noqa: BLE001
        logger.warning("training-failed email failed (client=%s): %s", client_id, e)
