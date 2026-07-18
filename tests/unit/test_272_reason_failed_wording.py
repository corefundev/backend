"""
tests/unit/test_272_reason_failed_wording.py

NC-9 #272 — reason-keyed training-failure wording in BOTH channels:
  • infra-interrupted class (#265 abandoned-run heal: "abandoned",
    "прервано обновлением сервиса") → NO CSV/data advice, re-trigger
    instruction only ("ограничение … снято автоматически");
  • every other error → the legacy data-validation template, pinned
    byte-for-byte so this change provably did NOT touch that path.

Also pins the notify_training_failed(client_id, error) signature in
both modules: call-sites (task_queue.py, reconcile_runs.py) and any
fixed-arity test stubs depend on it staying two-arg.
"""
from __future__ import annotations

import inspect

import pytest

import src.notifications.telegram as tg
from src.notifications.failure_reason import is_infra_interrupted
from src.notifications.training_email import _render_failed


# ── fixtures ────────────────────────────────────────────────────────

class _Rec:
    email = "user@example.com"
    config = {"notifications": {
        "email":    {"training_complete": True},
        "telegram": {"chat_id": 42, "training_complete": True},
    }}


class _Reg:
    def get(self, client_id):
        return _Rec()


@pytest.fixture
def wired(monkeypatch):
    """Fake registry (public get_registry) + pinned FRONTEND_URL."""
    import src.clients.registry as registry
    monkeypatch.setattr(registry, "get_registry", lambda: _Reg())
    monkeypatch.setenv("FRONTEND_URL", "https://sprosly.com")


_ABANDONED_RECONCILE = "прервано обновлением сервиса"
_ABANDONED_RUN_ROW = ("abandoned: training worker restarted mid-run "
                      "(deploy/crash) — run released automatically (#265)")
_VALIDATION_ERROR = "ValueError: колонка sales отсутствует"


# ── 1. classifier ───────────────────────────────────────────────────

@pytest.mark.parametrize("error", [
    _ABANDONED_RECONCILE,
    _ABANDONED_RUN_ROW,
    "Abandoned job 0fe548ee",                      # case-insensitive
    "rq.exceptions.AbandonedJobError: …",          # str(e) of the RQ class
])
def test_infra_markers_classify_as_infra(error):
    assert is_infra_interrupted(error) is True


@pytest.mark.parametrize("error", [
    _VALIDATION_ERROR,
    "CSV validation failed: missing column date",
    "период данных меньше 30 дней",
    "",
    None,
])
def test_data_errors_classify_as_validation(error):
    assert is_infra_interrupted(error) is False


def test_markers_match_the_actual_reconcile_writers():
    # The #265 heal is the writer of both infra texts — if its wording
    # ever drifts off the marker list, this pins the coupling.
    import src.pipeline.reconcile_runs as rr
    assert is_infra_interrupted(rr._ERROR_TEXT) is True
    from pathlib import Path
    src = Path("src/pipeline/reconcile_runs.py").read_text()
    assert 'error="прервано обновлением сервиса"' in src


# ── 2. email wording ────────────────────────────────────────────────

def test_email_abandoned_has_retrigger_and_no_csv_advice(monkeypatch):
    monkeypatch.setenv("FRONTEND_URL", "https://sprosly.com")
    subject, body = _render_failed("acme", _ABANDONED_RECONCILE)
    assert subject == "⚠ Обучение модели было прервано"
    assert "снято автоматически" in body
    assert "запустите обучение ещё раз" in body.lower()
    assert "https://sprosly.com/app/training" in body
    # the data-validation advice must be absent — it is noise here
    assert "CSV" not in body
    assert "30 дней" not in body
    assert "date / sales" not in body


def test_email_validation_template_unchanged(monkeypatch):
    # PIN: the non-infra path renders the exact pre-#272 template.
    monkeypatch.setenv("FRONTEND_URL", "https://sprosly.com")
    subject, body = _render_failed("acme", _VALIDATION_ERROR)
    assert subject == "⚠ Обучение модели не удалось"
    assert body == "\n".join([
        "Здравствуйте,",
        "",
        "Обучение модели для аккаунта «acme» завершилось с ошибкой.",
        "",
        "Сообщение системы:",
        f"  {_VALIDATION_ERROR}",
        "",
        "Что попробовать:",
        "  • Проверить, что в загруженном CSV нет пропусков в колонках date / sales",
        "  • Убедиться что период данных не меньше 30 дней",
        "  • Запустить обучение ещё раз: https://sprosly.com/app/training",
        "",
        "Если ошибка повторяется — напишите в поддержку, приложив текст выше.",
        "",
        "— Sprosly",
    ])


def test_email_full_path_sends_infra_wording(wired, monkeypatch):
    import src.auth.email_sender as email_sender
    sent = []
    monkeypatch.setattr(
        email_sender, "get_email_sender",
        lambda: type("S", (), {"send": lambda self, to, subject, body:
                               sent.append((to, subject, body))})(),
    )
    from src.notifications.training_email import notify_training_failed
    notify_training_failed(client_id="acme", error=_ABANDONED_RECONCILE)
    assert len(sent) == 1
    to, subject, body = sent[0]
    assert to == "user@example.com"
    assert subject == "⚠ Обучение модели было прервано"
    assert "CSV" not in body


# ── 3. telegram wording ─────────────────────────────────────────────

def _tg_capture(monkeypatch):
    sent = []
    monkeypatch.setattr(tg, "send_message",
                        lambda chat_id, text: sent.append((chat_id, text)) or True)
    return sent


def test_telegram_abandoned_has_retrigger_and_no_error_snippet(wired, monkeypatch):
    sent = _tg_capture(monkeypatch)
    tg.notify_training_failed(client_id="acme", error=_ABANDONED_RUN_ROW)
    assert len(sent) == 1
    chat_id, text = sent[0]
    assert chat_id == 42
    assert "прервано обновлением сервиса" in text
    assert "снято автоматически" in text
    assert "https://sprosly.com/app/training" in text
    # raw infra error is internal noise — must not reach the tenant
    assert "abandoned" not in text
    assert "Ошибка обучения" not in text


def test_telegram_validation_template_unchanged(wired, monkeypatch):
    # PIN: the non-infra path renders the exact pre-#272 message.
    sent = _tg_capture(monkeypatch)
    tg.notify_training_failed(client_id="acme", error=_VALIDATION_ERROR)
    assert len(sent) == 1
    assert sent[0] == (42, (
        "<b>⚠ Ошибка обучения</b>\n\n"
        "Аккаунт: <code>acme</code>\n"
        f"<code>{_VALIDATION_ERROR}</code>\n\n"
        "Подробности и повтор: https://sprosly.com/app/training"
    ))


# ── 4. signature pin (call-sites + fixed-arity stub protection) ─────

def test_notify_training_failed_signatures_unchanged():
    from src.notifications.training_email import notify_training_failed as e
    for fn in (e, tg.notify_training_failed):
        assert list(inspect.signature(fn).parameters) == ["client_id", "error"]
