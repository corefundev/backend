"""#183 (R14-4 / M-A8): anomaly down-weighting is now actually applied.

Two guarantees:
  1. `_fit_with_optional_groups` passes `sample_weight` only to a fit() that
     declares it — a fixed-arity stub still fits cleanly (protects test stubs,
     feedback_signature_change_breaks_stubs).
  2. `sample_weight` genuinely flows into the LightGBM fit and changes the model
     (the old pipeline computed weights then discarded them).
"""
import numpy as np
import pytest

from src.validation.walk_forward import _fit_with_optional_groups


def test_fit_helper_passes_sample_weight_only_when_accepted():
    seen = {}

    class AcceptsBoth:
        def fit(self, X, y, groups=None, sample_weight=None):
            seen["both"] = (groups is not None, sample_weight is not None)

    class AcceptsNeither:
        def fit(self, X, y):           # fixed-arity stub
            seen["neither"] = True

    w = np.array([1.0, 0.1, 1.0])
    _fit_with_optional_groups(AcceptsBoth(), None, None, "g", sample_weight=w)
    assert seen["both"] == (True, True)

    # Must NOT raise "unexpected keyword argument" on a stub that can't take it.
    _fit_with_optional_groups(AcceptsNeither(), None, None, "g", sample_weight=w)
    assert seen["neither"] is True


def test_sample_weight_changes_the_mimo_fit():
    pytest.importorskip("lightgbm")
    from tests.unit.test_improvements import _make_df, _make_config
    from src.models.mimo import MIMOForecaster
    from src.features.engineering import build_features, get_feature_columns

    df     = _make_df(n_skus=2, n_days=80)
    config = _make_config(horizon=3, model_type="mimo")
    df     = build_features(df, config)
    fc     = get_feature_columns(df, config)
    X, y   = df[fc], df["sales"]

    base     = MIMOForecaster(config).fit(X, y)
    w        = np.ones(len(X))
    w[: len(X) // 2] = 0.0                      # zero-out half the rows
    weighted = MIMOForecaster(config).fit(X, y, sample_weight=w)

    assert not np.allclose(base.predict(X), weighted.predict(X)), \
        "sample_weight must influence the MIMO fit (it was previously discarded)"
