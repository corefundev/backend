"""
src/notifications/failure_reason.py

#272 (NC-9) — training-failure reason classifier shared by the email
(training_email.py) and Telegram (telegram.py) notifiers, so both
channels key their remediation wording off ONE marker list and cannot
drift apart.

Two wording classes:
  • infra-interrupted — the run was killed by a deploy/crash of the
    training worker (#265 abandoned-run heal). The client's data is
    fine; the single honest instruction is "запустите ещё раз,
    ограничение снято автоматически". CSV/data advice here is noise.
  • everything else — the legacy data-validation template (CSV columns,
    30-day period, retry) stays byte-for-byte unchanged.

Marker sources (writers of the error text):
  • src/pipeline/reconcile_runs.py (#265): notifier call-sites pass
    error="прервано обновлением сервиса"; the run row gets
    _ERROR_TEXT = "abandoned: training worker restarted mid-run …".
  • RQ's AbandonedJobError message contains "abandoned".
Matching is case-insensitive substring, so any of those surfaces —
including str(e) of an AbandonedJobError — classifies as infra.
"""
from __future__ import annotations

INFRA_INTERRUPTED_MARKERS: tuple[str, ...] = (
    "abandoned",
    "прервано обновлением сервиса",
)


def is_infra_interrupted(error: str | None) -> bool:
    """True when the failure text marks an infra interruption
    (worker deploy/crash), NOT a problem with the client's data."""
    low = (error or "").lower()
    return any(marker in low for marker in INFRA_INTERRUPTED_MARKERS)
