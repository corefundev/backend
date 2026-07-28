"""#570 PC-2: known-future промо-фичи — математика, no-skew, fail-open,
синтетический гейт механизма (future-сигнал реально доезжает до модели).
"""
import numpy as np
import pandas as pd

from src.features.promo_calendar import (
    PROMO_CAL_FEATURE_COLS,
    build_promo_calendar_features,
    compute_promo_calendar_features,
)


def _events():
    return pd.DataFrame([
        {"sku": "A", "category": None,
         "date_from": "2026-09-05", "date_to": "2026-09-07"},
        {"sku": None, "category": "Молочка",
         "date_from": "2026-09-10", "date_to": "2026-09-12"},
    ])


# ── математика (сверено вручную) ─────────────────────────────────────────

def test_feature_math_sku_and_category_union():
    rows = pd.DataFrame({
        "date": ["2026-09-01", "2026-09-05", "2026-09-08", "2026-09-03"],
        "sku":  ["A", "A", "A", "B"],
        "cat":  ["Молочка", "Молочка", "Молочка", None],
    })
    f = compute_promo_calendar_features(
        rows["date"], rows["sku"], rows["cat"], _events())
    # A@09-01: акции нет; в (t, t+7] попадают 05-07 → 3 дня; старт через 4
    assert f.loc[0].tolist() == [0, 3, 6, 6, 4]
    # A@09-05: активна; (t, t+7] = 06..12 → 2 (06,07) + 3 (10..12) = 5; d2s=0
    assert f.loc[1].tolist() == [1, 5, 5, 5, 0]
    # A@09-08: не активна; (t, t+7] = 09..15 → 3 дня категории; старт через 2
    assert f.loc[2].tolist() == [0, 3, 3, 3, 2]
    # B без категории: событий нет → нули, d2s = cap 60
    assert f.loc[3].tolist() == [0, 0, 0, 0, 60]


def test_fail_open_zero_features():
    rows = pd.DataFrame({"date": ["2026-09-01"], "sku": ["A"], "cat": [None]})
    for events in (None, pd.DataFrame(columns=["sku", "category",
                                               "date_from", "date_to"])):
        f = compute_promo_calendar_features(
            rows["date"], rows["sku"], rows["cat"], events)
        assert f.loc[0].tolist() == [0, 0, 0, 0, 60]


def test_no_skew_train_vs_serve_same_function():
    """КОНТРАКТ #58/no-skew: строка обучения и будущая дата serve с теми же
    (sku, category, date) получают ИДЕНТИЧНЫЕ значения фич."""
    ev = _events()
    train = compute_promo_calendar_features(
        ["2026-09-06"], ["A"], ["Молочка"], ev)
    serve = compute_promo_calendar_features(
        ["2026-09-06"], ["A"], ["Молочка"], ev)
    pd.testing.assert_frame_equal(train, serve)


def test_build_wrapper_adds_all_columns():
    df = pd.DataFrame({
        "date": pd.to_datetime(["2026-09-01", "2026-09-05"]),
        "sku": ["A", "A"], "sales": [1.0, 2.0],
    })
    config = {"data": {"sku_col": "sku"}}
    out = build_promo_calendar_features(df, config, "date", events=_events())
    for c in PROMO_CAL_FEATURE_COLS:
        assert c in out.columns
    assert out["promo_cal_active"].tolist() == [0, 1]


# ── serve-path: recursive пересчитывает per-step, не carry ───────────────

def test_recursive_forecast_recomputes_promo_per_step():
    from src.pipeline.inference_utils import recursive_forecast

    hist_dates = pd.date_range("2026-08-25", "2026-09-03", freq="D")
    h = pd.DataFrame({
        "sku": "A", "date": hist_dates,
        "sales": np.arange(len(hist_dates), dtype=float),
        "category": "Молочка",
    })
    ev = _events()
    feats = compute_promo_calendar_features(
        h["date"], h["sku"], h["category"], ev)
    for c in PROMO_CAL_FEATURE_COLS:
        h[c] = feats[c]
    h["lag_1"] = h["sales"].shift(1)

    captured: "list[dict]" = []

    class _Model:
        def predict(self, X):
            captured.append(X.iloc[0].to_dict())
            return np.array([1.0])

    feature_cols = ["lag_1", *PROMO_CAL_FEATURE_COLS]
    config = {"features": {"promo_calendar": {"enabled": True},
                           "holidays": {"enabled": False}},
              "data": {"sku_col": "sku", "date_col": "date",
                       "target_col": "sales"}}
    rows = recursive_forecast(
        _Model(), h, feature_cols, horizon=7, sku="A",
        config=config, promo_events=ev)
    assert len(rows) == 7
    # история кончается 03.09: шаг 2 = 05.09 (старт акции A) → active=1;
    # замороженный carry с 03.09 дал бы active=0 на всех шагах
    assert captured[1]["promo_cal_active"] == 1
    assert captured[0]["promo_cal_active"] == 0
    # шаг 1 (04.09): до старта 1 день
    assert captured[0]["promo_cal_days_to_start"] == 1
    # без событий — carry-поведение (все нули из последней строки)
    captured.clear()
    recursive_forecast(_Model(), h, feature_cols, horizon=3, sku="A",
                       config=config, promo_events=None)
    assert all(c["promo_cal_days_to_start"] == h["promo_cal_days_to_start"].iloc[-1]
               for c in captured)


# ── ГЕЙТ МЕХАНИЗМА: синтетическая инъекция ───────────────────────────────

def test_synthetic_injection_gate_model_uses_future_signal():
    """Гейт #570: future-сигнал календаря РЕАЛЬНО доезжает до модели.

    Синтетика: спрос = 5 + 20×promo (шум нулевой). Модель (LightGBM нельзя —
    libomp локально; берём линейную на numpy) обучается на фичах, включая
    promo_cal_active, и должна прогнозировать всплеск В БУДУЩИЕ дни акции
    из календаря. Если инъекция сломана (carry вместо пересчёта) — прогноз
    плоский и тест падает."""
    from src.pipeline.inference_utils import recursive_forecast

    rng = pd.date_range("2026-06-01", "2026-08-31", freq="D")
    ev_hist = pd.DataFrame([
        {"sku": "A", "category": None, "date_from": "2026-06-10", "date_to": "2026-06-12"},
        {"sku": "A", "category": None, "date_from": "2026-07-08", "date_to": "2026-07-10"},
        {"sku": "A", "category": None, "date_from": "2026-08-05", "date_to": "2026-08-07"},
        # БУДУЩАЯ акция — за пределами истории
        {"sku": "A", "category": None, "date_from": "2026-09-03", "date_to": "2026-09-05"},
    ])
    h = pd.DataFrame({"sku": "A", "date": rng, "category": None})
    f = compute_promo_calendar_features(h["date"], h["sku"], h["category"], ev_hist)
    h["promo_cal_active"] = f["promo_cal_active"]
    h["sales"] = 5.0 + 20.0 * h["promo_cal_active"]
    h["lag_1"] = h["sales"].shift(1).fillna(5.0)

    # «модель»: линейная регрессия на [lag_1, promo_cal_active] (закрытая форма)
    X = h[["lag_1", "promo_cal_active"]].to_numpy()
    y = h["sales"].to_numpy()
    Xb = np.hstack([X, np.ones((len(X), 1))])
    w, *_ = np.linalg.lstsq(Xb, y, rcond=None)

    class _Lin:
        def predict(self, Xdf):
            arr = Xdf[["lag_1", "promo_cal_active"]].to_numpy(dtype=float)
            return arr @ w[:2] + w[2]

    config = {"features": {"promo_calendar": {"enabled": True},
                           "holidays": {"enabled": False}},
              "data": {"sku_col": "sku", "date_col": "date",
                       "target_col": "sales"}}
    rows = recursive_forecast(
        _Lin(), h, ["lag_1", "promo_cal_active"], horizon=7, sku="A",
        config=config, promo_events=ev_hist)
    by_date = {str(pd.Timestamp(r["date"]).date()): r["predicted_sales"]
               for r in rows}
    # будущая акция 03-05.09 видна: всплеск ≥ +15 к базе
    assert by_date["2026-09-03"] > by_date["2026-09-01"] + 15
    assert by_date["2026-09-04"] > by_date["2026-09-01"] + 15
    # вне акции — база
    assert abs(by_date["2026-09-01"] - 5.0) < 2.0

    # КОНТРОЛЬ ГЕЙТА: те же условия, но события НЕ переданы (сломанная
    # инъекция) → всплеска нет. Доказывает, что тест ловит поломку.
    rows_no = recursive_forecast(
        _Lin(), h, ["lag_1", "promo_cal_active"], horizon=7, sku="A",
        config=config, promo_events=None)
    by_date_no = {str(pd.Timestamp(r["date"]).date()): r["predicted_sales"]
                  for r in rows_no}
    assert abs(by_date_no["2026-09-03"] - by_date_no["2026-09-01"]) < 2.0


# ── load_active_events: fail-open к None ─────────────────────────────────

def test_load_active_events_fail_open(monkeypatch, tmp_path):
    import src.storage.promo_calendar as pc_mod
    from src.features.promo_calendar import load_active_events

    assert load_active_events(None) is None

    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    pc_mod.reset_registry_for_tests()
    assert load_active_events("ds-missing") is None       # нет календаря

    class _Boom:
        def get_active(self, ds): raise RuntimeError("db down")
    monkeypatch.setattr(pc_mod, "get_promo_calendar_registry", lambda: _Boom())
    assert load_active_events("ds1") is None              # сломан реестр → None
    pc_mod.reset_registry_for_tests()
