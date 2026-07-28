"""
ARCH-1 (#205) — post-training artifact generation extracted from task_queue
into `src.pipeline.post_training`.

Two goals:
  1. Structural: the seam landed — task_queue delegates to post_training via a
     LOCAL import (so the API process, which imports task_queue only to enqueue,
     never pulls pandas/features/inference_utils). Lazy-load discipline preserved.
  2. Behavioural: the forecast/anomaly generation — previously untested private
     functions buried in the queue module — now has direct coverage. These are
     forecast-quality-adjacent, the audit's top concern.

All dependencies are mocked: no Redis, no DB, no real model, no lightgbm.
"""
from __future__ import annotations

import inspect
import re
from types import SimpleNamespace

import pandas as pd

import src.pipeline.post_training as pt
import src.pipeline.task_queue as tr


# ── 1. Structural: extraction + lazy-import discipline ────────────────────────

def test_public_functions_exist():
    assert callable(pt.generate_and_store_forecasts)
    assert callable(pt.detect_and_store_anomalies)


def test_task_queue_no_longer_defines_the_business_fns():
    # The old private copies must be gone from task_queue (no dead duplicate).
    assert not hasattr(tr, "_generate_and_store_forecasts")
    assert not hasattr(tr, "_detect_and_store_anomalies")


def test_post_training_imported_locally_not_at_module_level():
    # The whole point of the extraction: post_training (heavy imports) must NOT
    # be imported at task_queue module top-level, or the API process would pull
    # pandas/features again. It is imported inside _post_training_artifacts.
    mod_src = inspect.getsource(tr)
    assert re.search(r"(?m)^from src\.pipeline\.post_training", mod_src) is None, (
        "post_training must not be a top-level import of task_queue (lazy-load)"
    )
    fn_src = inspect.getsource(tr._post_training_artifacts)
    assert "from src.pipeline.post_training import" in fn_src, (
        "_post_training_artifacts must import post_training locally and delegate"
    )
    assert "generate_and_store_forecasts" in fn_src
    assert "detect_and_store_anomalies" in fn_src


# ── 2a. generate_and_store_forecasts ──────────────────────────────────────────

def _wire_forecast_env(monkeypatch, *, forecasts_df, model_exists=True,
                       user_horizon=30, plan_max=7, plan_max_skus=30,
                       model_horizon=28):
    captured: dict = {}

    monkeypatch.setattr(pt, "get_registry",
                        lambda: SimpleNamespace(get=lambda cid: SimpleNamespace(plan="free")))
    monkeypatch.setattr(pt, "get_plan_spec",
                        lambda plan: SimpleNamespace(max_horizon_days=plan_max,
                                                     max_skus=plan_max_skus))

    cfg = {"model": {"horizon": user_horizon},
           "data": {"sku_col": "sku", "date_col": "date", "target_col": "sales"}}
    monkeypatch.setattr(pt, "get_config_manager",
                        lambda cp: SimpleNamespace(get_effective=lambda cid, reg: cfg,
                                                   get_effective_serving=lambda cid, reg: cfg))

    monkeypatch.setattr(pt, "load_data", lambda path, config: pd.DataFrame({"sku": ["A"]}))
    monkeypatch.setattr(pt, "validate_data", lambda df, config: df)

    class _Storage:
        def __init__(self, cid, **k): pass
        def model_exists(self): return model_exists
        def load_model(self): return SimpleNamespace(horizon=model_horizon)
    monkeypatch.setattr(pt, "ClientStorage", _Storage)
    monkeypatch.setattr(pt, "serve_feature_set", lambda m: ([], []))
    monkeypatch.setattr(pt, "build_features", lambda df, config, **k: df)
    monkeypatch.setattr(pt, "get_feature_columns", lambda df, config: ["f1"])

    def _fake_forecast(model, df, feature_cols, config, horizon, max_skus, **kw):
        captured["horizon"] = horizon
        captured["max_skus"] = max_skus
        return forecasts_df
    monkeypatch.setattr(pt, "forecast_all_skus", _fake_forecast)

    monkeypatch.setattr(pt, "get_forecasts_registry", lambda: SimpleNamespace(
        replace_for_client=lambda client_id, run_id, rows, **k: captured.update(rows=rows)))
    return captured


def test_forecasts_happy_path_maps_rows_and_caps_horizon(monkeypatch):
    fc = pd.DataFrame({
        "sku":             ["A", "A"],
        "date":            [pd.Timestamp("2026-01-01"), pd.Timestamp("2026-01-02")],
        "predicted_sales": [10.0, 12.0],
        "p10":             [8.0, float("nan")],     # day 2 has no lower band
        "p90":             [12.0, 14.0],
    })
    cap = _wire_forecast_env(monkeypatch, forecasts_df=fc, user_horizon=30, plan_max=7)
    pt.generate_and_store_forecasts(client_id="acme", data_path="d", model_path="m.pkl",
                                    config_path="c", run_id="r1")
    # horizon capped to the plan ceiling (min(30, 7))
    assert cap["horizon"] == 7
    assert cap["max_skus"] == 30
    # rows mapped; NaN p10 surfaced as None so the FE hides the ribbon.
    # #308: 6th element = order_qty interpolated at default τ=0.7 from [p10,p90]
    # (row1: 8 + 0.75·(12−8) = 11.0; row2 has no p10 → None, no recommendation).
    assert cap["rows"] == [
        ("A", "2026-01-01", 10.0, 8.0, 12.0, 11.0),
        ("A", "2026-01-02", 12.0, None, 14.0, None),
    ]


def test_forecasts_skip_when_no_model_path(monkeypatch):
    cap = _wire_forecast_env(monkeypatch, forecasts_df=pd.DataFrame())
    pt.generate_and_store_forecasts(client_id="acme", data_path="d", model_path=None,
                                    config_path="c", run_id="r1")
    assert "rows" not in cap   # returned before any write


def test_forecasts_skip_when_model_missing_in_storage(monkeypatch):
    cap = _wire_forecast_env(monkeypatch, forecasts_df=pd.DataFrame(), model_exists=False)
    pt.generate_and_store_forecasts(client_id="acme", data_path="d", model_path="m.pkl",
                                    config_path="c", run_id="r1")
    assert "rows" not in cap


# ── 2b. detect_and_store_anomalies ────────────────────────────────────────────

def _wire_anomaly_env(monkeypatch, *, df, flag_threshold):
    captured: dict = {}
    monkeypatch.setattr(pt, "get_registry", lambda: SimpleNamespace())
    cfg = {"data": {"sku_col": "sku", "date_col": "date", "target_col": "sales"}}
    monkeypatch.setattr(pt, "get_config_manager",
                        lambda cp: SimpleNamespace(get_effective=lambda cid, reg: cfg,
                                                   get_effective_serving=lambda cid, reg: cfg))
    monkeypatch.setattr(pt, "load_data", lambda path, config: df)
    monkeypatch.setattr(pt, "validate_data", lambda d, config: d)
    monkeypatch.setattr(pt, "build_features", lambda d, config: d)

    class _Detector:
        def fit_detect(self, d, sku_col, target_col, date_col):
            flagged = d.copy()
            flagged["is_anomaly"] = d[target_col] >= flag_threshold
            return flagged, None
    monkeypatch.setattr(pt, "SalesAnomalyDetector", _Detector)
    monkeypatch.setattr(pt, "get_anomalies_registry", lambda: SimpleNamespace(
        replace_for_client=lambda client_id, run_id, rows, **k: captured.update(rows=rows)))
    return captured


def test_anomalies_filters_to_lookback_window(monkeypatch):
    df = pd.DataFrame({
        "sku":   ["A", "A", "B"],
        "date":  [pd.Timestamp("2026-03-15"),   # anomaly, inside 90d
                  pd.Timestamp("2025-06-01"),    # anomaly, OUTSIDE 90d → dropped
                  pd.Timestamp("2026-04-01")],   # not an anomaly (max date)
        "sales": [200.0, 300.0, 50.0],
    })
    cap = _wire_anomaly_env(monkeypatch, df=df, flag_threshold=100.0)
    pt.detect_and_store_anomalies(client_id="acme", data_path="d", config_path="c",
                                  run_id="r1", lookback_days=90)
    assert cap["rows"] == [("A", "2026-03-15", 200.0)]


def test_anomalies_none_flagged_persists_empty(monkeypatch):
    df = pd.DataFrame({
        "sku":   ["A", "B"],
        "date":  [pd.Timestamp("2026-03-15"), pd.Timestamp("2026-04-01")],
        "sales": [10.0, 20.0],
    })
    cap = _wire_anomaly_env(monkeypatch, df=df, flag_threshold=100.0)
    pt.detect_and_store_anomalies(client_id="acme", data_path="d", config_path="c",
                                  run_id="r1")
    assert cap["rows"] == []   # explicit empty replace, not skipped


def test_forecasts_interval_honesty_is_per_row(monkeypatch):
    # #158 → QW2-1 (#224): interval honesty moved to the serve SOURCE — the
    # chained dispatcher emits calibrated bands on the direct segment and
    # None on the recursive tail. post_training must pass both through
    # per-row (no run-level suppression): a banded row stores its band, a
    # None/NaN row stores None.
    fc = pd.DataFrame({
        "sku":             ["A", "A"],
        "date":            [pd.Timestamp("2026-01-01"), pd.Timestamp("2026-01-20")],
        "predicted_sales": [10.0, 11.0],
        "p10":             [8.0, None],               # tail row: band suppressed at source
        "p90":             [12.0, None],
    })
    cap = _wire_forecast_env(monkeypatch, forecasts_df=fc,
                             user_horizon=30, plan_max=90, model_horizon=14)
    pt.generate_and_store_forecasts(client_id="acme", data_path="d",
                                    model_path="m.pkl", config_path="c", run_id="r1")
    assert cap["horizon"] == 30
    assert cap["rows"] == [
        ("A", "2026-01-01", 10.0, 8.0, 12.0, 11.0),  # direct: band kept, order_qty @0.7
        ("A", "2026-01-20", 11.0, None, None, None), # recursive tail: point-only, no rec
    ]
