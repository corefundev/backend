"""QW3-1b (#316) — fold-clean category-TE плечо стенда (libomp-free).

Свойства:
  • category_te появляется в feature_cols (+1 к базе) и в фит-фрейме;
  • TE fold-clean: значения теста считаются из ТРЕЙН-статистики фолда
    (проверяем формулой на детерминированных уровнях);
  • сглаживание m — dose-response: m=20 и m=100 дают разные значения;
  • SKU вне карты → глобальное среднее фолда (fallback без NaN);
  • без карты плечо падает явной ошибкой (не молча без фичи).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import bench_wf_ab as bench  # noqa: E402


def _config():
    return {
        "data":       {"sku_col": "sku", "date_col": "date", "target_col": "sales"},
        "model":      {"horizon": 5},
        "validation": {"n_splits": 2},
    }


def _df(n_days=60):
    dates = pd.date_range("2026-01-01", periods=n_days, freq="D")
    rows = []
    for d in dates:
        rows.append({"sku": "A", "date": d, "sales": 2.0})     # cat X
        rows.append({"sku": "B", "date": d, "sales": 4.0})     # cat X
        rows.append({"sku": "C", "date": d, "sales": 100.0})   # cat Y
        rows.append({"sku": "D", "date": d, "sales": 10.0})    # вне карты
    df = pd.DataFrame(rows)
    df["lag_1"] = df.groupby("sku")["sales"].shift(1)
    df["days_to_payday"] = 3.0
    df["is_payday_window"] = 0
    return df


CAT_MAP = {"A": "X", "B": "X", "C": "Y"}


class _StubMIMO:
    is_mimo = True

    def __init__(self, captured, horizon):
        self.captured = captured
        self.horizon = horizon

    def fit(self, X, y, groups=None, sample_weight=None):
        self.captured["fits"].append((X.copy(), pd.Series(groups).copy()))
        return self

    def predict(self, last_row):
        return np.full((1, self.horizon), 10.0)


def _run(arm):
    captured = {"fits": []}
    out = bench.run_arm(arm, _df(), _config(),
                        model_factory=lambda: _StubMIMO(captured, 5),
                        category_map=CAT_MAP)
    return out, captured


def test_feature_added_and_counted():
    base = bench.run_arm("base", _df(), _config(),
                         model_factory=lambda: _StubMIMO({"fits": []}, 5))
    out, cap = _run("cat_te")
    assert out["n_features"] == base["n_features"] + 1
    X, _ = cap["fits"][0]
    assert "category_te" in X.columns
    assert not X["category_te"].isna().any()


def test_te_is_fold_clean_and_formula_exact():
    out, cap = _run("cat_te")
    X, g = cap["fits"][0]                      # трейн первого фолда
    m = 20.0
    tr = pd.DataFrame({"sku": g.values, "sales": np.nan})
    # восстановим sales по уровням (детерминированные константы)
    level = {"A": 2.0, "B": 4.0, "C": 100.0, "D": 10.0}
    tr["sales"] = tr["sku"].map(level)
    g_mean = tr["sales"].mean()
    x_rows = tr["sku"].map(CAT_MAP) == "X"
    te_x = (tr[x_rows]["sales"].sum() + m * g_mean) / (x_rows.sum() + m)
    got = X.loc[g.values == "A", "category_te"].iloc[0]
    assert got == pytest.approx(te_x, rel=1e-9)
    # SKU вне карты → глобальное среднее фолда
    got_d = X.loc[g.values == "D", "category_te"].iloc[0]
    assert got_d == pytest.approx(g_mean, rel=1e-9)


def test_smoothing_dose_changes_values():
    _, cap20 = _run("cat_te")
    _, cap100 = _run("cat_te_m100")
    x20, g20 = cap20["fits"][0]
    x100, g100 = cap100["fits"][0]
    v20 = x20.loc[g20.values == "C", "category_te"].iloc[0]
    v100 = x100.loc[g100.values == "C", "category_te"].iloc[0]
    assert v20 != pytest.approx(v100)          # дозы различимы
    # больше сглаживания → ближе к глобальному среднему
    level = {"A": 2.0, "B": 4.0, "C": 100.0, "D": 10.0}
    g_mean = pd.Series(g20.values).map(level).mean()
    assert abs(v100 - g_mean) < abs(v20 - g_mean)


def test_missing_map_raises():
    with pytest.raises(ValueError, match="category map"):
        bench.run_arm("cat_te", _df(), _config(),
                      model_factory=lambda: _StubMIMO({"fits": []}, 5))
