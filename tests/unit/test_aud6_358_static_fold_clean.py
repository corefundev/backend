"""
AUD-6 (#358) — static features must be fold-clean in walk-forward.

Before: train.py computed the #307 static map (velocity_band / price_tier
terciles) on the FULL frame — including the report-tail days walk-forward
then grades on — and passed that df to walk_forward_validate. A SKU whose
sales jump inside the test window carries a `velocity_band` that partly
encodes that jump, so every fold model trained on a feature leaking its own
test-window level. Systematically optimistic: the client metric, the
promotion gate (#227), the HPO slice (#180) and the clean-blend holdout
(#152). The final/serve path was always clean (full df = full train).

After: `walk_forward_validate(..., fold_feature_fn=...)` rebuilds the
static columns per fold from that fold's train rows only (test rows get the
train-built map — mirroring serve, where the model carries the train-time
map). `fold_static_recompute` is the shared helper; train.py threads it
into both the report walk-forward and run_hpo's trial walk-forward.

Tests are libomp-free (stub MIMO), per test_r14_test2_walk_forward style.
"""
import numpy as np
import pandas as pd

from src.features.static_features import (
    STATIC_COLS,
    _FALLBACK_BAND,
    compute_static_map,
    fold_static_recompute,
    merge_static_features,
)
from src.validation.walk_forward import walk_forward_validate


def _leaky_df(n_days: int = 60, jump_day: int = 51) -> pd.DataFrame:
    """Three SKUs; A is slow for the whole TRAIN era and explodes only in
    the report tail (with horizon=5 × n_splits=2 the fold cutoffs land on
    days 50 and 55, so the jump at day 51 sits inside the graded windows).

    Full-history means: A≈167, B=10, C=100 → A lands in the FAST tercile.
    Means up to day 50:  A=1,   B=10, C=100 → A is the SLOW tercile.
    So A's velocity_band flips 2→0 the moment the map is train-only —
    the exact leak walk-forward folds must not see.
    """
    dates = pd.date_range("2026-01-01", periods=n_days, freq="D")
    rows = []
    for d_idx, d in enumerate(dates, start=1):
        rows.append({"sku": "A", "date": d,
                     "sales": 1000.0 if d_idx >= jump_day else 1.0})
        rows.append({"sku": "B", "date": d, "sales": 10.0})
        rows.append({"sku": "C", "date": d, "sales": 100.0})
    return pd.DataFrame(rows)


def _with_full_frame_statics(df: pd.DataFrame) -> pd.DataFrame:
    smap = compute_static_map(df, "sku", "sales")
    return merge_static_features(df, smap, "sku")


def _config(horizon: int = 5, n_splits: int = 2) -> dict:
    return {
        "data":       {"sku_col": "sku", "date_col": "date", "target_col": "sales"},
        "model":      {"horizon": horizon},
        "validation": {"n_splits": n_splits},
    }


# ── the leak itself, demonstrated ────────────────────────────────────────

def test_full_frame_map_leaks_the_tail_jump():
    """Sanity anchor: the full-frame map really does encode the test-window
    jump (A=fast), while the train-only map calls A slow. If this ever
    stops holding, the fixture no longer exercises the leak."""
    df = _leaky_df()
    full_map = compute_static_map(df, "sku", "sales").set_index("sku")
    train_map = compute_static_map(
        df[df["date"] <= "2026-02-19"], "sku", "sales").set_index("sku")
    assert full_map.loc["A", "velocity_band"] == 2.0
    assert train_map.loc["A", "velocity_band"] == 0.0


# ── fold_static_recompute: the shared helper ─────────────────────────────

def test_recompute_overwrites_leaked_bands_in_both_slices():
    df = _with_full_frame_statics(_leaky_df())
    train = df[df["date"] <= "2026-02-19"].copy()
    test = df[df["date"] > "2026-02-19"].copy()
    assert (train.loc[train["sku"] == "A", "velocity_band"] == 2.0).all(), "fixture"

    train2, test2 = fold_static_recompute(train, test, "sku", "sales")
    assert (train2.loc[train2["sku"] == "A", "velocity_band"] == 0.0).all()
    # test rows get the TRAIN-built band — like serve reads the model map.
    assert (test2.loc[test2["sku"] == "A", "velocity_band"] == 0.0).all()


def test_recompute_preserves_rows_and_order():
    df = _with_full_frame_statics(_leaky_df())
    train = df[df["date"] <= "2026-02-19"].copy()
    test = df[df["date"] > "2026-02-19"].copy()
    train2, test2 = fold_static_recompute(train, test, "sku", "sales")
    for before, after in ((train, train2), (test, test2)):
        assert len(after) == len(before)
        pd.testing.assert_frame_equal(
            after[["sku", "date", "sales"]].reset_index(drop=True),
            before[["sku", "date", "sales"]].reset_index(drop=True),
        )
        assert list(after.columns) == list(before.columns)


def test_sku_unseen_in_train_gets_fallback_band():
    """A SKU that first appears inside the test window has no train history —
    serve would hand it _FALLBACK_BAND from the model map; folds must match."""
    df = _with_full_frame_statics(_leaky_df())
    train = df[df["date"] <= "2026-02-19"].copy()
    test = df[df["date"] > "2026-02-19"].copy()
    newcomer = test.iloc[:3].copy()
    newcomer["sku"] = "NEW"
    test = pd.concat([test, newcomer], ignore_index=True)

    _, test2 = fold_static_recompute(train, test, "sku", "sales")
    got = test2.loc[test2["sku"] == "NEW", list(STATIC_COLS)]
    assert (got == _FALLBACK_BAND).all().all()


# ── walk_forward_validate: the hook is applied before the fit ────────────

class _CapturingMIMO:
    """Direct multi-step stub; records the train frame (with its SKU groups)
    each fold fits on — X carries only feature columns, so the per-SKU
    assertion needs `groups` alongside it."""
    is_mimo = True

    def __init__(self, captured, horizon):
        self.captured = captured
        self.horizon = horizon

    def fit(self, X, y, groups=None, sample_weight=None):
        self.captured["fits"].append((X.copy(), groups.copy()))
        return self

    def predict(self, last_row):
        return np.full((1, self.horizon), 10.0)


def _run_wf(fold_feature_fn):
    df = _with_full_frame_statics(_leaky_df())
    captured = {"fits": []}
    walk_forward_validate(
        df, lambda: _CapturingMIMO(captured, 5),
        ["velocity_band", "price_tier"], _config(),
        fold_feature_fn=fold_feature_fn,
    )
    assert captured["fits"], "walk-forward should have fitted folds"
    return captured


def test_folds_see_train_only_bands_with_the_hook():
    captured = _run_wf(
        lambda tr, te: fold_static_recompute(tr, te, "sku", "sales"))
    for fold_X, groups in captured["fits"]:
        # A's jump starts strictly AFTER every fold's train cutoff (days
        # 50/55), so an honest fold can never call A fast (band 2) — the
        # full-frame map does exactly that. Other SKUs (C) are legitimately
        # fast, hence the per-SKU assertion.
        a_bands = fold_X.loc[(groups == "A").to_numpy(), "velocity_band"]
        assert (a_bands != 2.0).all(), (
            "a fold model trained on the full-frame (leaked) velocity_band"
        )


def test_folds_see_leaked_bands_without_the_hook():
    """Counter-assertion: drop the hook and the leak is visible — proves the
    test detects the defect rather than passing vacuously."""
    captured = _run_wf(None)
    assert any((fold_X.loc[(groups == "A").to_numpy(), "velocity_band"] == 2.0).any()
               for fold_X, groups in captured["fits"])


# ── wiring: train.py + hpo.py thread the hook (house pin style) ──────────

def test_train_pipeline_threads_the_hook():
    from pathlib import Path
    src = Path("src/pipeline/train.py").read_text()
    i_def = src.index("def _fold_statics")
    assert "fold_static_recompute" in src[i_def:i_def + 600]
    i_wf = src.index("wf_result = walk_forward_validate(")
    assert "fold_feature_fn=_fold_statics" in src[i_wf:i_wf + 400], (
        "report walk-forward must rebuild statics per fold"
    )
    i_hpo = src.index("best_params = run_hpo(")
    assert "fold_feature_fn=_fold_statics" in src[i_hpo:i_hpo + 300], (
        "HPO trials must rebuild statics per fold too (#180 slice excludes "
        "tail ROWS but full-frame static values still encoded the tail)"
    )


def test_run_hpo_threads_the_hook_into_trial_validation():
    import inspect
    from src.models import hpo
    assert "fold_feature_fn" in inspect.signature(hpo.run_hpo).parameters
    src = inspect.getsource(hpo.run_hpo)
    i_call = src.index("walk_forward_validate(df, factory")
    assert "fold_feature_fn=fold_feature_fn" in src[i_call:i_call + 200]
