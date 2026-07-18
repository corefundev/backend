"""Модель-аудит H1 — план-дефолты обязаны применяться одинаково в train
и во ВСЕХ serve-путях: платная модель обучается с FX-фичами, и serve
обязан собрать те же фичи (боевой инцидент: KeyError byn_rub_lag_1 →
тихий SeasonalNaive-фолбэк)."""
from types import SimpleNamespace

from src.clients.config_manager import (
    apply_plan_defaults,
    plan_default_entries,
)
from src.plans.plans import get_plan_spec


def _rec(plan, config=None):
    return SimpleNamespace(plan=plan, config=config or {})


def test_paid_plan_enables_fx_and_objective():
    cfg = {"model": {}, "features": {}}
    apply_plan_defaults(cfg, _rec("business"))
    assert cfg["features"]["external_regressors_ru"]["enabled"] is True
    assert cfg["model"]["objective"] == get_plan_spec("business").default_objective
    assert cfg["hpo"]["n_trials"] == get_plan_spec("business").hpo_n_trials


def test_free_plan_keeps_fx_off():
    cfg = {"model": {}, "features": {}}
    apply_plan_defaults(cfg, _rec("free"))
    assert cfg["features"].get("external_regressors_ru", {}).get("enabled") is False


def test_explicit_client_override_wins():
    # клиент явно выключил FX — тарифный дефолт НЕ перекрывает
    cfg = {"model": {}, "features": {"external_regressors_ru": {"enabled": False}}}
    apply_plan_defaults(cfg, _rec("business",
        config={"features": {"external_regressors_ru": {"enabled": False}}}))
    assert cfg["features"]["external_regressors_ru"]["enabled"] is False


def test_none_record_is_free_semantics():
    cfg = {"model": {}}
    apply_plan_defaults(cfg, None)
    assert cfg["features"]["external_regressors_ru"]["enabled"] is False


def test_single_source_of_truth():
    # train.py больше не несёт собственной копии списка дефолтов
    src = open("src/pipeline/train.py").read()
    assert "plan_defaults = [" not in src
    assert "apply_plan_defaults" in src
    # и все словарные ключи серв-паритета живут в одном месте
    spec = get_plan_spec("business")
    targets = {t for _, t, _ in plan_default_entries(spec, "business")}
    assert "features.external_regressors_ru.enabled" in targets
