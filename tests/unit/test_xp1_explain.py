"""XP-1 (#469): объяснимость-лайт — движок, группировка, ротация снапшотов.

LightGBM-зависимые тесты скипаются на машинах без libomp (macOS локально);
Linux-CI гоняет их полностью — свойство TreeSHAP «вклады суммируются в
прогноз» проверяется точно, это главный инвариант честности фичи.
"""
import numpy as np
import pandas as pd
import pytest

from src.explain import FALLBACK_GROUP, group_of


# ── группировка (pure, без ML) ───────────────────────────────────────────────

@pytest.mark.parametrize("feature,expected", [
    ("lag_224", "Недавний спрос"),
    ("rolling_mean_28", "Недавний спрос"),
    ("sku_share_lag_1", "Недавний спрос"),
    ("price_rolling_std_7", "Цена"),
    ("promo_rolling_14", "Промо"),
    ("stockout_streak", "Наличие на складе"),
    ("is_oos", "Наличие на складе"),
    ("days_since_restock", "Наличие на складе"),
    ("ny_ramp", "Праздники и события"),
    ("feb23_ramp", "Праздники и события"),
    ("is_holiday", "Праздники и события"),
    ("days_to_holiday", "Праздники и события"),
    ("dayofweek_sin", "Сезонность и календарь"),
    ("is_month_end", "Сезонность и календарь"),
    ("days_to_payday", "Сезонность и календарь"),
    ("weekofyear", "Сезонность и календарь"),
    ("temp_roll7", "Погода"),
    ("is_rainy_day", "Погода"),
    ("category_te", "Профиль товара"),
    ("category_te_fallback", "Профиль товара"),
    ("market_total_lag_7", "Рынок"),
    ("currency", "Рынок"),
])
def test_group_mapping_known_features(feature, expected):
    assert group_of(feature) == expected


def test_group_mapping_unknown_falls_back():
    assert group_of("mystery_feature_42") == FALLBACK_GROUP


def test_every_engineering_feature_has_nonfallback_group():
    """Инвентарь реальных фичей стенда не должен молча утекать в 'Другое' —
    новая фича без правила группировки ловится этим тестом."""
    inventory = [
        "lag_1", "lag_7", "rolling_mean_7", "rolling_std_14", "rolling_max_28",
        "dayofweek", "dayofweek_cos", "dayofmonth", "dayofyear", "month",
        "month_sin", "quarter", "year", "weekofyear", "is_weekend",
        "is_month_start", "is_month_end", "days_to_payday", "is_payday_window",
        "price_change", "price_lag_1", "price_rolling_mean_7",
        "promo_lag_1", "promo_rolling_7",
        "stock", "stock_lag_1", "stock_change_1", "stock_rolling_min_7",
        "is_oos", "oos_rolling_30", "stockout_streak", "days_since_restock",
        "is_holiday", "days_to_holiday", "holidays", "ny_ramp", "may1_ramp",
        "event_ramp", "temp_lag1", "temp_range", "rain_roll7", "is_cold_day",
        "is_hot_day", "is_rainy_day", "category_te", "category_te_fallback",
        "market_total", "market_total_lag_1", "currency", "sku_share_lag_1",
        # прод-инвентарь 2026-07-23 (модель 82 фичи, run 8eab1347): FX-пары,
        # расширенная погода, соседние праздничные, velocity_band — эти 21
        # фича УТЕКАЛИ в «Другое» (13% массы у живого SKU)
        "precipitation_mm", "wind_speed_max",
        "days_since_holiday", "is_pre_holiday", "is_post_holiday",
        "usd_rub_lag_1", "usd_rub_change", "usd_rub_rolling_7",
        "eur_rub_lag_1", "cny_rub_change", "byn_rub_rolling_7",
        "kzt_rub_lag_1", "velocity_band",
    ]
    stray = [f for f in inventory if group_of(f) == FALLBACK_GROUP]
    assert not stray, f"фичи без группы: {stray}"


# ── движок на настоящем LightGBM ─────────────────────────────────────────────

def _lgb_or_skip():
    try:
        import lightgbm  # noqa: F401
    except (ImportError, OSError) as e:
        pytest.skip(f"lightgbm not loadable on this machine: {e}")


def _tiny_frame(n_sku: int = 3, days: int = 120) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    rows = []
    for s in range(n_sku):
        base = 5.0 + 3 * s
        for d in range(days):
            rows.append({
                "sku": f"S{s}",
                "date": pd.Timestamp("2025-01-01") + pd.Timedelta(days=d),
                "sales": float(rng.poisson(base)),
                "lag_1": float(rng.poisson(base)),
                "rolling_mean_7": base + rng.normal(0, .5),
                "dayofweek": d % 7,
                "price_change": float(rng.normal(0, .02)),
            })
    return pd.DataFrame(rows)


_FEATS = ["lag_1", "rolling_mean_7", "dayofweek", "price_change"]


def _fit_mimo(objective: str):
    from src.models.mimo import MIMOForecaster
    cfg = {"model": {"horizon": 3, "objective": objective,
                     "n_estimators": 12, "num_leaves": 7,
                     "learning_rate": 0.3},
           "data": {"sku_col": "sku", "date_col": "date",
                    "target_col": "sales"}}
    df = _tiny_frame()
    m = MIMOForecaster(cfg)
    m.fit(df[_FEATS], df["sales"], groups=df["sku"])
    return m, df


def test_contributions_sum_to_raw_prediction_identity_link():
    """TreeSHAP-инвариант: для objective=regression (identity-линк)
    Σвкладов + base == предсказание головы, точно."""
    _lgb_or_skip()
    from src.explain import explain_anchor
    m, df = _fit_mimo("regression")
    anchor = df[df["sku"] == "S1"].sort_values("date").iloc[[-1]]
    ex = explain_anchor(m, anchor)
    contrib_sum = sum(ex["groups"].values()) + ex["base"]
    raw = np.asarray(m.predict(anchor[_FEATS]), dtype=float)[0]
    assert abs(contrib_sum - float(raw.sum())) < 1e-6
    assert ex["heads"] == 3
    assert ex["prediction"] >= 0


def test_contributions_log_link_direction_tweedie():
    """Для tweedie вклады в raw-score (лог-линк): exp(Σ+base) ≈ прогноз
    головы h=1 — фиксируем связь, на которой держится честность долей."""
    _lgb_or_skip()
    import lightgbm as lgb
    df = _tiny_frame()
    head = lgb.LGBMRegressor(objective="tweedie", n_estimators=12,
                             num_leaves=7, learning_rate=0.3)
    head.fit(df[_FEATS], df["sales"])
    x = df[_FEATS].iloc[[-1]]
    contrib = np.asarray(head.predict(x, pred_contrib=True))[0]
    assert abs(np.exp(contrib.sum()) - float(head.predict(x)[0])) < 1e-6


def test_build_explanations_shape_and_meta():
    _lgb_or_skip()
    from src.explain import build_explanations
    m, df = _fit_mimo("regression")
    cfg = {"data": {"sku_col": "sku", "date_col": "date",
                    "target_col": "sales"}}
    ex = build_explanations(m, df, cfg, horizon=14, max_skus=2)
    assert set(ex.columns) == {"sku", "group", "contribution",
                               "prediction_sum", "base", "heads"}
    assert ex["sku"].nunique() == 2            # max_skus капнул
    assert (ex["heads"] == 3).all()            # горизонт капнут головами модели
    per_sku = ex.groupby("sku")["prediction_sum"].nunique()
    assert (per_sku == 1).all()


def test_ensemble_uses_primary_child():
    """Ансамбль объясняется ведущим ребёнком — объект с primary_objective
    и models_-словарём маршрутизируется в него."""
    _lgb_or_skip()
    from src.explain import explain_anchor
    m, df = _fit_mimo("regression")

    class _FakeEnsemble:
        primary_objective = "regression"
        models_ = {"regression": m}

    anchor = df[df["sku"] == "S0"].sort_values("date").iloc[[-1]]
    direct = explain_anchor(m, anchor)
    routed = explain_anchor(_FakeEnsemble(), anchor)
    assert routed["groups"] == direct["groups"]


# ── ротация снапшотов ────────────────────────────────────────────────────────

def test_save_explanations_rotates_prev(tmp_path, monkeypatch):
    """Вторая запись уводит первую в _prev; третья затирает prev второй.
    Локальный backend, без S3."""
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path))
    from src.storage.backend import ClientStorage, LocalStorageBackend
    storage = ClientStorage("xp1test", backend=LocalStorageBackend(str(tmp_path)))

    df1 = pd.DataFrame({"sku": ["A"], "group": ["Цена"],
                        "contribution": [1.0], "prediction_sum": [10.0],
                        "base": [0.5], "heads": [3]})
    df2 = df1.assign(contribution=[2.0])

    storage.save_explanations(df1)
    assert storage.explanations_exist()
    assert not storage.explanations_prev_exist()

    storage.save_explanations(df2)
    assert storage.explanations_prev_exist()
    assert storage.load_explanations()["contribution"].iloc[0] == 2.0
    assert storage.load_explanations_prev()["contribution"].iloc[0] == 1.0


def test_explain_survives_missing_besteffort_columns():
    """FX/погода — best-effort: их колонки могут отсутствовать во фрейме
    сервинга (упавший фетч ЦБ). Движок заполняет 0.0 (идиома serve-path),
    а не роняет все SKU KeyError'ом — регресс 2026-07-23 (0 rows)."""
    _lgb_or_skip()
    from src.explain import explain_anchor
    m, df = _fit_mimo("regression")
    anchor = (df[df["sku"] == "S1"].sort_values("date").iloc[[-1]]
              .drop(columns=["price_change"]))    # симулируем пропавшую фичу
    ex = explain_anchor(m, anchor)
    assert ex["heads"] == 3
    assert ex["prediction"] >= 0
    assert "Цена" in ex["groups"]                 # группа есть, вклад ≈ 0
