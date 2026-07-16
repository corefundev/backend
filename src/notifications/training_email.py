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
    client_id:     str,
    duration_sec:  float | None,
    n_skus:        int | None,
    wmape:         float | None,
    mase:          float | None,
    mase_seasonal: float | None = None,
    wmape_order_14: float | None = None,
    gate_passed:   bool | None  = None,
    gate_blocked:  bool | None  = None,
) -> tuple[str, str]:
    """Returns (subject, body) for a successful training run.

    QW2-4 #227 / #268: wording keys on gate_blocked (артефакт не сохранён —
    предыдущая модель продолжает работать), NOT on the verdict alone: a
    promoted-first-model with a FAIL verdict SERVES (нет предыдущей) и
    получает честное «качество ниже эталона»."""
    base = _frontend_base_url()
    if gate_blocked:
        subject = "⚠ Обучение завершено — новая модель не прошла контроль качества"
    elif gate_passed is False:
        subject = "⚠ Обучение завершено — качество ниже эталона"
    else:
        subject = "✓ Обучение модели завершено"
    body_lines = [
        f"Здравствуйте,",
        "",
        (f"Обучение для аккаунта «{client_id}» завершено, но новая модель не "
         f"прошла автоматический контроль качества (точность ниже действующей "
         f"модели или базового прогноза). Продолжает работать предыдущая "
         f"модель — ваши прогнозы не ухудшились.")
        if gate_blocked else
        (f"Обучение для аккаунта «{client_id}» завершено. Модель работает, но "
         f"её точность пока ниже простого сезонного прогноза — проверьте "
         f"полноту истории продаж и переобучите модель позже.")
        if gate_passed is False else
        f"Обучение модели для аккаунта «{client_id}» успешно завершено.",
        "",
        f"  • Длительность:       {_format_duration(duration_sec)}",
        f"  • Товаров (SKU):      {n_skus if n_skus is not None else '—'}",
        (f"  • Точность для заказа (14 дн): {max(0.0, (1 - wmape_order_14)) * 100:.1f}%  — совпадение суммы заказа за 2 недели"
         if isinstance(wmape_order_14, float) else
         "  • Точность для заказа (14 дн): —"),
        f"  • WMAPE (точность):   {_format_pct(wmape)}  — чем меньше, тем точнее",
        f"  • MASE:               {_format_num(mase)}  — < 1: лучше наивного «как вчера»",
        f"  • MASE сезонный:      {_format_num(mase_seasonal)}  — < 1: лучше «как в этот день неделю назад» (базовая модель)",
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
    # NC-9 #272 — reason-keyed remediation: an infra interruption (#265
    # abandoned-run heal) is not the client's fault, so the CSV/период
    # advice below would be noise; the honest instruction is re-trigger.
    from src.notifications.failure_reason import is_infra_interrupted
    if is_infra_interrupted(error):
        subject = "⚠ Обучение модели было прервано"
        body_lines = [
            f"Здравствуйте,",
            "",
            f"Обучение модели для аккаунта «{client_id}» было прервано "
            f"обновлением сервиса и не завершилось.",
            "",
            "Ваши данные в порядке — ошибка не связана с загруженным файлом.",
            "",
            "Что сделать:",
            f"  • Запустите обучение ещё раз: {base}/app/training",
            "    Ограничение на повторный запуск снято автоматически.",
            "",
            "Если запуск снова прерывается — напишите в поддержку.",
            "",
            "— SKU Forecasting",
        ]
        return subject, "\n".join(body_lines)
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
    client_id:     str,
    duration_sec:  float | None = None,
    n_skus:        int | None   = None,
    wmape:         float | None = None,
    mase:          float | None = None,
    mase_seasonal: float | None = None,
    wmape_order_14: float | None = None,
    gate_passed:   bool | None  = None,
    gate_blocked:  bool | None  = None,
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
        subject, body = _render_finished(
            client_id, duration_sec, n_skus, wmape, mase, mase_seasonal,
            wmape_order_14,
            gate_passed,
            gate_blocked,
        )
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
