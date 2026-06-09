"""
tests/unit/test_backtest_runner.py

R11-#76 — plumbing for src/validation/backtest_runner.py (the real-pipeline
wiring). The HEAVY run (LightGBM/libomp) happens on staging; here we monkeypatch
the pipeline calls and assert the wrapper orchestration: isolation env, file-
based training hand-off, and the serve-path column contract.
"""
from __future__ import annotations

import os

import pandas as pd
import pytest

# backtest_runner imports the training pipeline, which hard-depends on lightgbm
# (needs libomp). On a box without libomp (macOS dev) the import OSErrors — skip
# the whole module cleanly there; CI/Linux has libomp and runs it. Same guard as
# test_training_smoke.py (project_ml_test_failures_unverified).
try:
    import lightgbm  # noqa: F401
except (ImportError, OSError) as e:  # pragma: no cover - env-dependent
    pytest.skip(f"lightgbm not loadable on this machine: {e}", allow_module_level=True)

import src.validation.backtest_runner as runner  # noqa: E402


def test_serve_fn_returns_only_point_forecast_columns(monkeypatch):
    cfg = {"data": {"sku_col": "sku", "date_col": "date", "target_col": "sales"}}
    monkeypatch.setattr(runner, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(runner, "build_features", lambda df, c: df)
    monkeypatch.setattr(runner, "get_feature_columns", lambda df, c: ["f1"])

    captured = {}

    def fake_forecast_all(model, df, feature_cols, config, horizon=None):
        captured["horizon"] = horizon
        # production returns extra columns — the wrapper must drop them
        return pd.DataFrame([
            {"sku": "A", "date": "2024-01-01", "predicted_sales": 5.0,
             "p10": 1.0, "p90": 9.0, "step": 1, "source": "primary"},
        ])

    monkeypatch.setattr(runner, "forecast_all_skus", fake_forecast_all)

    serve_fn = runner._make_serve_fn("configs/config.yaml")
    out = serve_fn(model=object(), history_df=pd.DataFrame({"sku": ["A"]}), horizon=7)

    assert list(out.columns) == ["sku", "date", "predicted_sales"]
    assert captured["horizon"] == 7


def test_train_fn_uses_isolated_backtest_client_and_loads_model(monkeypatch, tmp_path):
    seen = {}

    def fake_run_training(data_path, config_path, client_id, output_dir):
        # train_df must be materialised to a CSV path (pipeline is file-based)
        assert os.path.isfile(data_path), data_path
        seen["client_id"] = client_id
        seen["data_path"] = data_path
        seen["config_path"] = config_path
        return {"model_path": os.path.join(output_dir, "model.pkl")}

    sentinel_model = object()
    monkeypatch.setattr(runner, "run_training_pipeline", fake_run_training)
    monkeypatch.setattr(runner, "load_model_any_format", lambda p, c: sentinel_model)

    train_fn = runner._make_train_fn("configs/config.yaml", str(tmp_path))
    df = pd.DataFrame({"sku": ["A", "A"], "date": ["2024-01-01", "2024-01-02"], "sales": [1, 2]})
    model = train_fn(df, {"data": {}})

    assert model is sentinel_model
    assert seen["client_id"] == "backtest"     # never a real client
    assert seen["config_path"] == "configs/config.yaml"
    assert seen["data_path"].endswith(".csv")


def test_run_baseline_forces_local_storage(monkeypatch, tmp_path):
    # tiny dataset on disk so pd.read_csv works without the real fixture
    data = tmp_path / "d.csv"
    pd.DataFrame({"sku": ["A"], "date": ["2024-01-01"], "sales": [1]}).to_csv(data, index=False)

    # even if the env says s3 (as the prod worker does), a backtest must FORCE
    # local so it never writes its throwaway model to the production S3 store.
    monkeypatch.setenv("STORAGE_BACKEND", "s3")
    monkeypatch.setattr(runner, "load_config",
                        lambda *a, **k: {"data": {"sku_col": "sku", "date_col": "date", "target_col": "sales"}})
    # short-circuit the actual backtest — we only assert the isolation side-effect
    monkeypatch.setattr(runner, "run_backtest", lambda *a, **k: "RESULT")

    out = runner.run_baseline(data_path=str(data), holdout_days=1)
    assert out == "RESULT"
    assert os.environ.get("STORAGE_BACKEND") == "local"   # forced override
