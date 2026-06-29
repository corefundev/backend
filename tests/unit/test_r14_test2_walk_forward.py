"""TEST-2 (#186): walk_forward_validate produces the HEADLINE training metric the
product reports, yet had no direct test of its split / no-overlap / predict-input
behaviour (only the private _get_split_points helper was covered). These tests
drive walk_forward_validate end-to-end with a libomp-free stub MIMO and prove:

  1. the model is NEVER asked to predict FROM a test-window row (no leakage);
  2. the expanding window advances the train cutoff across folds;
  3. the aggregated headline metric (wmape_global) is produced.
"""
import numpy as np
import pandas as pd

from src.validation.walk_forward import walk_forward_validate, _get_split_points
from tests.unit.test_improvements import _make_df, _make_config


class _StubMIMO:
    """Direct multi-step stub (is_mimo → walk_forward uses direct mode). Records
    the date of every row it is asked to predict FROM; returns a constant (1, H)."""
    is_mimo = True

    def __init__(self, captured, horizon, date_col="date"):
        self.captured = captured
        self.horizon = horizon
        self.date_col = date_col
        self.feature_cols = None

    def fit(self, X, y, groups=None, sample_weight=None):
        self.feature_cols = list(X.columns)
        return self

    def predict(self, last_row):
        self.captured["from_dates"].append(pd.Timestamp(last_row[self.date_col].iloc[0]))
        return np.full((1, self.horizon), 10.0)


def _run():
    df = _make_df(n_skus=2, n_days=40)
    config = _make_config(horizon=3)
    config["validation"]["n_splits"] = 2
    captured = {"from_dates": []}
    res = walk_forward_validate(df, lambda: _StubMIMO(captured, 3), ["price"], config)
    return df, config, captured, res


def test_never_predicts_from_its_own_test_window():
    # PER-FOLD no-leak: each fold may only predict from a row ON/BEFORE its own
    # train cutoff — never from its own test window (split, split+H]. (An
    # EARLIER fold's test window legitimately becomes a LATER fold's train data
    # under the expanding window — that is correct, not leakage.)
    df, config, captured, _ = _run()
    horizon  = config["model"]["horizon"]
    n_splits = config["validation"]["n_splits"]
    n_skus   = df["sku"].nunique()

    splits = sorted(pd.Timestamp(s) for s in _get_split_points(df["date"].unique(), horizon, n_splits))
    assert captured["from_dates"], "the validator should have asked the model to predict"
    # _predict_direct_multi_step calls predict() once per SKU, folds processed in
    # order → from_dates groups into n_skus per fold.
    assert len(captured["from_dates"]) == n_splits * n_skus
    for fold_idx, split in enumerate(splits):
        fold_from = captured["from_dates"][fold_idx * n_skus:(fold_idx + 1) * n_skus]
        bad = [d for d in fold_from if d > split]
        assert not bad, (
            f"fold {fold_idx} predicted FROM a row after its train cutoff {split.date()} "
            f"(would leak the test window): {bad}"
        )


def test_expanding_window_advances_train_cutoff():
    _, _, captured, _ = _run()
    # Later folds train on a later cutoff than earlier folds (expanding window).
    assert max(captured["from_dates"]) > min(captured["from_dates"])


def test_produces_headline_metric():
    _, _, _, res = _run()
    assert "wmape_global" in res.aggregated
    # constant-10 forecast vs random positive sales → a finite, positive WMAPE.
    assert np.isfinite(res.aggregated["wmape_global"])
    assert res.aggregated["wmape_global"] > 0
