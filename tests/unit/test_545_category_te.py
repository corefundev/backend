"""#545: category target-encoding as the third static feature.

Contract mirrors the bench arm (bench_wf_ab cat_te, confirmed −4.6/−4.08%
WMAPE 2026-07-19): Bayesian smoothing to the TRAIN global mean (m=20),
unknown SKU/category → global mean, fold-clean recompute, graceful
absence when the dataset carries no category column.
"""
import pandas as pd
import pytest

from src.features.static_features import (
    compute_static_map,
    fold_static_recompute,
    merge_static_features,
)


def _df(with_cat=True):
    rows = []
    for sku, cat, level in (("a", "х/б", 10.0), ("b", "х/б", 20.0),
                            ("c", "молоко", 100.0)):
        for d in pd.date_range("2024-01-01", periods=10):
            rows.append({"sku": sku, "date": d, "sales": level,
                         **({"category": cat} if with_cat else {})})
    return pd.DataFrame(rows)


def test_te_math_matches_bench_formula():
    df = _df()
    smap = compute_static_map(df, "sku", "sales",
                              category_col="category", te_smoothing=20)
    g = df["sales"].mean()
    # категория х/б: sum=10*10+20*10=300, count=20
    expected = (300 + 20 * g) / (20 + 20)
    got = float(smap.loc[smap["sku"] == "a", "category_te"].iloc[0])
    assert got == pytest.approx(expected)
    # обе SKU одной категории получают одно значение
    assert got == pytest.approx(
        float(smap.loc[smap["sku"] == "b", "category_te"].iloc[0]))


def test_unknown_sku_falls_back_to_train_global_mean():
    df = _df()
    smap = compute_static_map(df, "sku", "sales",
                              category_col="category", te_smoothing=20)
    serve = pd.DataFrame({"sku": ["новый"], "sales": [0.0]})
    out = merge_static_features(serve, smap, "sku")
    assert float(out["category_te"].iloc[0]) == pytest.approx(df["sales"].mean())


def test_no_category_column_is_a_noop_not_zeros():
    df = _df(with_cat=False)
    smap = compute_static_map(df, "sku", "sales",
                              category_col="category", te_smoothing=20)
    assert "category_te" not in smap.columns
    out = merge_static_features(df.copy(), smap, "sku")
    assert "category_te" not in out.columns


def test_fold_recompute_is_leakage_clean():
    # Тестовые строки со ВЗРЫВОМ продаж не должны влиять на TE:
    # статистика — только из train-строк фолда.
    df = _df()
    train = df[df["date"] < "2024-01-08"].copy()
    test = df[df["date"] >= "2024-01-08"].copy()
    test.loc[:, "sales"] = 10_000.0
    tr, te = fold_static_recompute(train, test, "sku", "sales",
                                   category_col="category", te_smoothing=20)
    assert float(te["category_te"].max()) < 1_000, \
        "test-window sales leaked into the category statistic"
    # train и test одной SKU несут одно значение (карта train-фолда)
    a_tr = float(tr.loc[tr["sku"] == "a", "category_te"].iloc[0])
    a_te = float(te.loc[te["sku"] == "a", "category_te"].iloc[0])
    assert a_tr == pytest.approx(a_te)


def test_backward_compatible_positional_call():
    # Урок «signature change breaks stubs»: старые вызовы без kwargs живы.
    df = _df(with_cat=False)
    smap = compute_static_map(df, "sku", "sales")
    assert list(smap.columns) == ["sku", "velocity_band", "price_tier"]
    tr, te = fold_static_recompute(df, df.copy(), "sku", "sales")
    assert "category_te" not in tr.columns


def test_raw_category_column_never_reaches_feature_cols():
    from src.features.engineering import get_feature_columns
    df = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=3),
                       "sku": "a", "sales": 1.0,
                       "category": "х/б", "category_te": 5.0})
    cfg = {"data": {"date_col": "date", "sku_col": "sku",
                    "target_col": "sales", "category_col": "category"},
           "features": {}}
    cols = get_feature_columns(df, cfg)
    assert "category" not in cols
    assert "category_te" in cols
