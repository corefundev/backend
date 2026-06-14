"""
tests/unit/test_quality_year_quarter_prune.py

Quality-#98 evidence-based prune: `year` (non-generalizing absolute year,
0.00 importance) and `quarter` (redundant with month + dayofyear, 0.00)
are dropped from new models' feature set but still emitted by
_build_calendar_features so pre-prune models that carry them in
feature_cols keep working at serve.
"""
from __future__ import annotations

from src.features.engineering import build_features, get_feature_columns
from tests.unit.test_features import make_config, make_test_df


def test_year_quarter_excluded_from_features():
    df = build_features(make_test_df(n_skus=3), make_config())
    cols = get_feature_columns(df, make_config())
    assert "year" not in cols
    assert "quarter" not in cols


def test_year_quarter_still_emitted_for_backcompat():
    # pre-prune models carry them in feature_cols and select them at
    # predict time — the column must remain in the engineered frame.
    df = build_features(make_test_df(n_skus=3), make_config())
    assert "year" in df.columns
    assert "quarter" in df.columns


def test_generalizing_calendar_features_kept():
    # the seasonal signal that DOES generalize stays in the feature set
    df = build_features(make_test_df(n_skus=3), make_config())
    cols = get_feature_columns(df, make_config())
    for keep in ("dayofyear", "month", "month_sin", "month_cos", "dayofweek"):
        assert keep in cols, keep
