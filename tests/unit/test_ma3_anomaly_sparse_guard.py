"""MA-3 #521 (аудит H2) — sparse-SKU больше не расстреливается IQR-веткой."""
import pandas as pd

from src.data.anomaly_detection import SalesAnomalyDetector


def _sparse_df():
    # 80% нулей: q1=q3=0 → iqr=0
    sales = [0]*16 + [3, 5, 2, 4]
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=20),
        "sku": "s", "sales": sales,
    })


def test_guard_skips_degenerate_iqr():
    df, w = SalesAnomalyDetector(sparse_iqr_guard=True).fit_detect(
        _sparse_df(), sku_col="sku", target_col="sales")
    sold = df[df.sales > 0]
    assert not sold.is_anomaly.any()
    assert (w == 1.0).all()


def test_legacy_behavior_reproducible_for_ab():
    df, w = SalesAnomalyDetector(sparse_iqr_guard=False).fit_detect(
        _sparse_df(), sku_col="sku", target_col="sales")
    sold = df[df.sales > 0]
    assert sold.is_anomaly.all()          # старый дефект воспроизводим
    assert (w[df.sales.to_numpy() > 0] == 0.1).all()


def test_normal_sku_still_flags_outliers():
    sales = [10.0]*29 + [500.0]
    df = pd.DataFrame({"date": pd.date_range("2026-01-01", periods=30),
                       "sku": "n", "sales": sales})
    out, w = SalesAnomalyDetector(sparse_iqr_guard=True).fit_detect(
        df, sku_col="sku", target_col="sales")
    assert bool(out.iloc[-1].is_anomaly)
    assert w[-1] == 0.1
