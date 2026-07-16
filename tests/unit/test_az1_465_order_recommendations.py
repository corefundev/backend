"""
AZ-1 (#465) — «Автозаказ»: рекомендации к заказу по окну.

Endpoint агрегирует хранимые прогнозы по окну заказа: спрос = Σ point,
рекомендация = Σ order_quantity(p10, p90, τ) — ТА ЖЕ формула, которой
post_training считал сохранённый order_qty, поэтому τ меняется на лету
без переобучения. τ-override — платная ручка (зеркалит config-ключ
model.service_level из _START_CONFIG_KEYS); Free получает effective-τ.
Дни без калиброванной полосы честно выпадают (days_covered).
CSV — выгрузка для 1С: UTF-8 BOM, ';', десятичная запятая, целое «К заказу».
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import src.api.routers.inference as inf


def _rows():
    # 3 дня × 2 SKU; у B третий день без полосы (рекурсивный хвост)
    out = []
    for d, day in enumerate(["2026-07-17", "2026-07-18", "2026-07-19"]):
        out.append({"sku": "A", "forecast_date": day, "value": 10.0,
                    "p10": 8.0, "p90": 16.0, "generated_at": "2026-07-16T12:00:00"})
        out.append({"sku": "B", "forecast_date": day, "value": 5.0,
                    "p10": None if d == 2 else 2.0,
                    "p90": None if d == 2 else 6.0,
                    "generated_at": "2026-07-16T12:00:00"})
    return out


def _env(monkeypatch, rows=None, plan="business", cfg_tau=0.7):
    monkeypatch.setattr(inf, "require_client_access", lambda cid, auth: None)
    monkeypatch.setattr(inf, "_default_dataset_id", lambda cid: "ds1")
    monkeypatch.setattr(
        "src.clients.registry.get_registry",
        lambda: SimpleNamespace(get=lambda cid: SimpleNamespace(plan=plan)))
    monkeypatch.setattr(
        "src.clients.config_manager.get_config_manager",
        lambda: SimpleNamespace(get_effective=lambda cid, reg: {
            "model": {"service_level": cfg_tau}}))
    monkeypatch.setattr(
        "src.storage.forecasts.get_forecasts_registry",
        lambda: SimpleNamespace(
            list_for_client=lambda cid, dataset_id=None: rows if rows is not None else _rows()))
    monkeypatch.setattr(
        "src.storage.datasets.get_datasets_registry",
        lambda: SimpleNamespace(get=lambda ds_id: None))


def _call(**kw):
    kw.setdefault("auth", SimpleNamespace(roles=["forecast"], client_id="test"))
    return inf.order_recommendations("test", **kw)


# ── агрегация ────────────────────────────────────────────────────────────

def test_sums_demand_and_order_over_window(monkeypatch):
    _env(monkeypatch)
    out = _call(window=3)
    a = next(i for i in out["items"] if i["sku"] == "A")
    # τ=0.7 → q = p10 + (0.7−0.1)/(0.9−0.1)·(p90−p10) = 8 + 0.75·8 = 14/день
    assert a["demand_sum"] == 30.0
    assert a["order_sum"] == pytest.approx(42.0)
    assert a["order_qty"] == 42
    assert a["days"] == 3 and a["days_covered"] == 3
    assert out["service_level"] == 0.7
    assert out["first_date"] == "2026-07-17" and out["last_date"] == "2026-07-19"


def test_uncovered_days_are_visible_not_faked(monkeypatch):
    _env(monkeypatch)
    b = next(i for i in _call(window=3)["items"] if i["sku"] == "B")
    assert b["days_covered"] == 2            # третий день без полосы
    assert b["demand_sum"] == 15.0           # спрос — по всем дням
    # рекомендация только по покрытым: q/день = 2 + 0.75·4 = 5 → 10
    assert b["order_sum"] == pytest.approx(10.0)
    assert b["order_qty"] == 10


def test_window_truncates_dates(monkeypatch):
    _env(monkeypatch)
    out = _call(window=2)
    a = next(i for i in out["items"] if i["sku"] == "A")
    assert a["days"] == 2 and a["demand_sum"] == 20.0


def test_tau_recomputed_on_the_fly_matches_stored_formula(monkeypatch):
    """Смена τ двигает рекомендацию монотонно по той же формуле —
    переобучение не требуется."""
    _env(monkeypatch)
    q70 = next(i for i in _call(window=3)["items"] if i["sku"] == "A")["order_sum"]
    q90 = next(i for i in _call(window=3, service_level=0.9)["items"]
               if i["sku"] == "A")["order_sum"]
    assert q90 > q70
    from src.models.newsvendor import order_quantity
    assert q90 == pytest.approx(3 * order_quantity(8.0, 16.0, 0.9))


# ── гейты ────────────────────────────────────────────────────────────────

def test_tau_override_is_paid_only(monkeypatch):
    _env(monkeypatch, plan="free")
    with pytest.raises(HTTPException) as ei:
        _call(service_level=0.9)
    assert ei.value.status_code == 403
    # без override Free работает на конфигной τ
    assert _call()["service_level"] == 0.7


def test_validation_bounds(monkeypatch):
    _env(monkeypatch)
    for kw in ({"window": 0}, {"window": 61},
               {"service_level": 0.4}, {"service_level": 0.96},
               {"format": "xml"}):
        with pytest.raises(HTTPException) as ei:
            _call(**kw)
        assert ei.value.status_code == 422, kw


def test_no_forecasts_json_empty_csv_404(monkeypatch):
    _env(monkeypatch, rows=[])
    assert _call()["items"] == []
    with pytest.raises(HTTPException) as ei:
        _call(format="csv")
    assert ei.value.status_code == 404


# ── CSV для 1С ───────────────────────────────────────────────────────────

def test_csv_is_1c_friendly(monkeypatch):
    _env(monkeypatch)
    resp = _call(window=3, format="csv")
    raw = resp.body.decode("utf-8")
    assert raw.startswith("﻿")                      # BOM для Excel-RU/1С
    lines = raw.lstrip("﻿").split("\r\n")
    assert lines[0].startswith("sku;")
    row_a = next(ln for ln in lines if ln.startswith("A;"))
    cells = row_a.split(";")
    assert cells[1] == ""                                # остатков нет — пусто
    assert cells[2] == "30,00"                           # десятичная запятая
    assert cells[3] == "12,00"                           # страховой запас = 42−30
    assert cells[4] == "42"                              # целые штуки, ceil
    assert "attachment" in resp.headers["content-disposition"]
    assert "csv" in resp.media_type


def test_csv_ceil_rounds_up_partial_units(monkeypatch):
    rows = [{"sku": "A", "forecast_date": "2026-07-17", "value": 1.0,
             "p10": 1.0, "p90": 2.0, "generated_at": "t"}]
    _env(monkeypatch, rows=rows)
    # τ=0.7 → 1.75 → к заказу 2 (безопасный запас, не 1)
    resp = _call(window=1, format="csv")
    row = next(ln for ln in resp.body.decode("utf-8").split("\r\n")
               if ln.startswith("A;"))
    assert row.split(";")[4] == "2"


# ── остаток и страховой запас (прототип: заказ = прогноз + запас − остаток) ──

def _env_with_stock(monkeypatch, stock_df):
    import pandas as pd
    _env(monkeypatch)
    ver = SimpleNamespace(snapshot_key="snap.parquet", status="ready")
    ds = SimpleNamespace(client_id="test", current_version=1)
    monkeypatch.setattr(
        "src.storage.datasets.get_datasets_registry",
        lambda: SimpleNamespace(get=lambda ds_id: ds,
                                get_version=lambda ds_id, v: ver))
    import src.storage.zones as z
    monkeypatch.setattr(z, "get_zone_backend", lambda zone: SimpleNamespace(
        load_dataframe=lambda key: stock_df))
    return pd


def test_stock_subtracted_from_order(monkeypatch):
    import pandas as pd
    _env_with_stock(monkeypatch, pd.DataFrame({
        "sku": ["A", "A", "B"],
        "date": ["2026-07-10", "2026-07-15", "2026-07-15"],
        "stock": [99.0, 10.0, None],
    }))
    out = _call(window=3, dataset_id="ds1")
    a = next(i for i in out["items"] if i["sku"] == "A")
    assert a["stock"] == 10.0                            # последний по дате
    assert a["safety_sum"] == pytest.approx(12.0)        # 42 − 30
    assert a["order_qty"] == 32                          # ceil(42 − 10)
    b = next(i for i in out["items"] if i["sku"] == "B")
    assert b["stock"] is None                            # только NaN-строки
    assert b["order_qty"] == 10                          # без вычитания


def test_stock_never_pushes_order_below_zero(monkeypatch):
    import pandas as pd
    _env_with_stock(monkeypatch, pd.DataFrame({
        "sku": ["A"], "date": ["2026-07-15"], "stock": [10_000.0]}))
    out = _call(window=3, dataset_id="ds1")
    a = next(i for i in out["items"] if i["sku"] == "A")
    assert a["order_qty"] == 0                           # не отрицательный


def test_stock_read_failure_degrades_quietly(monkeypatch):
    _env(monkeypatch)
    monkeypatch.setattr(
        "src.storage.datasets.get_datasets_registry",
        lambda: (_ for _ in ()).throw(RuntimeError("s3 down")))
    out = _call(window=3, dataset_id="ds1")              # страница живёт
    a = next(i for i in out["items"] if i["sku"] == "A")
    assert a["stock"] is None and a["order_qty"] == 42
