"""
tests/unit/test_r11_58_sku_encoded.py

R11-#58 — the sku_encoded train/serve skew.

`pd.factorize` assigns codes by order-of-appearance in the frame it's
called on. Training sees all SKUs (codes 0..N-1); the /predict path
builds features on ONE SKU, so it always produces code 0 — misapplying
the training SKU-0 effect to every served SKU.

The fix: new models exclude sku_encoded from their feature set
(get_feature_columns default), so they are served honestly. The column
is STILL emitted by build_features for backward compatibility — pre-#58
models carry it in their persisted feature_cols and would KeyError at
predict-time if it vanished.
"""
from __future__ import annotations

from src.features.engineering import build_features, get_feature_columns
from tests.unit.test_features import make_config, make_test_df


def _built(flag=None):
    df = make_test_df(n_skus=5)
    cfg = make_config()
    if flag is not None:
        cfg["features"]["sku_encoded_enabled"] = flag
    return build_features(df, cfg), cfg


def test_sku_encoded_excluded_from_features_by_default():
    df_feat, cfg = _built()
    cols = get_feature_columns(df_feat, cfg)
    assert "sku_encoded" not in cols, (
        "default: new models must NOT train on the skewed sku_encoded"
    )


def test_sku_encoded_still_emitted_for_backcompat():
    # The column must remain in the engineered frame so pre-#58 models
    # (whose persisted feature_cols include it) still find it at serve.
    df_feat, _ = _built()
    assert "sku_encoded" in df_feat.columns


def test_sku_encoded_included_when_flag_on():
    df_feat, cfg = _built(flag=True)
    cols = get_feature_columns(df_feat, cfg)
    assert "sku_encoded" in cols


def test_factorize_skew_reproduced_single_sku_is_zero():
    # Demonstrates the bug the exclusion sidesteps: building features on
    # one SKU yields code 0 regardless of which SKU it is — so a feature
    # keyed on it cannot align with training.
    cfg = make_config()
    full = build_features(make_test_df(n_skus=5), cfg)
    # codes 0..4 across the 5 SKUs when built together
    assert full["sku_encoded"].nunique() == 5

    one = make_test_df(n_skus=5)
    one = one[one["sku"] == "SKU_003"].copy()
    one_feat = build_features(one, cfg)
    assert (one_feat["sku_encoded"] == 0).all(), (
        "single-SKU serve frame always factorizes to 0 — the skew"
    )


def test_excluded_feature_set_is_serve_consistent():
    # With the feature excluded, the feature set built on the full frame
    # equals the set built on any single-SKU slice (order/identity no
    # longer leaks in) — the property that makes /predict honest.
    cfg = make_config()
    full = get_feature_columns(build_features(make_test_df(n_skus=5), cfg), cfg)
    one = make_test_df(n_skus=5)
    one = one[one["sku"] == "SKU_002"].copy()
    one_cols = get_feature_columns(build_features(one, cfg), cfg)
    assert set(full) == set(one_cols)
