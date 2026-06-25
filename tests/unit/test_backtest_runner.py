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
    monkeypatch.setattr(runner, "build_features", lambda df, c, **k: df)
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

    def fake_run_training(data_path, config_path, client_id):
        # train_df must be materialised to a CSV path (pipeline is file-based)
        assert os.path.isfile(data_path), data_path
        seen["client_id"] = client_id
        seen["data_path"] = data_path
        seen["config_path"] = config_path
        # model_path is owned by ClientStorage; load_model_any_format is mocked,
        # so the exact value is irrelevant to this test.
        return {"model_path": "backtest/model.pkl"}

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
    # short-circuit the actual backtest + client registration — we only assert
    # the isolation side-effects here.
    monkeypatch.setattr(runner, "run_backtest", lambda *a, **k: "RESULT")
    monkeypatch.setattr(runner, "_register_backtest_client",
                        lambda tier, extra_config=None: None)

    out = runner.run_baseline(data_path=str(data), holdout_days=1)
    assert out == "RESULT"
    assert os.environ.get("STORAGE_BACKEND") == "local"   # forced override
    assert os.environ.get("DATABASE_URL") is None         # forced off the prod DB


def test_run_baseline_propagates_extra_config_feature_flags_to_serve(monkeypatch, tmp_path):
    # extra_config feature flags (e.g. features.external_regressors_ru) must reach
    # the SERVE config, not just the train client registry. If they diverge, a
    # config-gated feature is trained-in yet never built at serve, and the model
    # errors selecting the missing columns (train/serve skew — caught on the 1C
    # FX A/B). run_backtest receives the iso_config (also written to the yaml that
    # serve_fn loads), so assert the merged flag landed there.
    data = tmp_path / "d.csv"
    pd.DataFrame({"sku": ["A"], "date": ["2024-01-01"], "sales": [1]}).to_csv(data, index=False)
    monkeypatch.setattr(runner, "load_config",
                        lambda *a, **k: {"data": {"sku_col": "sku", "date_col": "date", "target_col": "sales"}})
    monkeypatch.setattr(runner, "_register_backtest_client",
                        lambda tier, extra_config=None: None)
    captured = {}
    monkeypatch.setattr(runner, "run_backtest",
                        lambda df, cfg, **k: captured.update(cfg=cfg) or "RESULT")

    runner.run_baseline(
        data_path=str(data), holdout_days=1, tier="free",
        extra_config={"features": {"external_regressors_ru": {"enabled": True}}},
    )
    feats = captured["cfg"].get("features", {}).get("external_regressors_ru", {})
    assert feats.get("enabled") is True


def test_isolated_config_points_mlflow_at_local_file_store(tmp_path):
    base = {"data": {"sku_col": "sku", "date_col": "date", "target_col": "sales"},
            "mlflow": {"tracking_uri": "http://mlflow:5000", "experiment_name": "prod"}}
    cfg = runner._isolated_config(base, str(tmp_path), tier="free")
    # a backtest must NEVER log to the production mlflow server / S3 bucket
    assert cfg["mlflow"]["tracking_uri"].startswith("file://")
    assert str(tmp_path) in cfg["mlflow"]["tracking_uri"]
    assert cfg["mlflow"]["experiment_name"] == "backtest-free"
    # original config object is untouched (deep copy)
    assert base["mlflow"]["tracking_uri"] == "http://mlflow:5000"


def test_register_backtest_client_carries_tier_config(monkeypatch):
    # the tier's objective/HPO must reach the pipeline via a REGISTERED client
    # config (so plan-tier defaults see user_set_* and don't clobber it — the
    # bug the first business run hit). Capture what gets registered.
    captured = {}

    class _FakeReg:
        def register(self, record):
            captured["record"] = record

    monkeypatch.setattr(runner, "get_registry", lambda: _FakeReg())

    runner._register_backtest_client("business")
    rec = captured["record"]
    assert rec.client_id == "backtest" and rec.plan == "business"
    assert rec.config["model"]["objective"] == "ensemble"
    assert rec.config["hpo"] == {"enabled": True, "n_trials": 30}
    assert rec.config["features"]["external_regressors_ru"]["enabled"] is False

    runner._register_backtest_client("free")
    rec = captured["record"]
    assert rec.plan == "free" and rec.config["model"]["objective"] == "mse"
    assert rec.config["hpo"]["enabled"] is False


def test_run_baseline_rejects_unknown_tier(tmp_path):
    data = tmp_path / "d.csv"
    pd.DataFrame({"sku": ["A"], "date": ["2024-01-01"], "sales": [1]}).to_csv(data, index=False)
    with pytest.raises(ValueError, match="tier"):
        runner.run_baseline(data_path=str(data), tier="enterprise")


def test_register_backtest_client_deep_merges_extra_config(monkeypatch):
    # extra_config A/B plumbing: deep-merges over the tier config
    # (override wins on leaves, sibling keys preserved, source untouched).
    captured = {}

    class _FakeReg:
        def register(self, record):
            captured["record"] = record

    monkeypatch.setattr(runner, "get_registry", lambda: _FakeReg())
    runner._register_backtest_client(
        "free", extra_config={"features": {"is_gap_day_enabled": True}}
    )
    cfg = captured["record"].config
    assert cfg["features"]["is_gap_day_enabled"] is True           # override landed
    assert cfg["model"]["objective"] == "mse"                      # tier sibling kept
    assert cfg["features"]["external_regressors_ru"] == {"enabled": False}
    # source tier dict not mutated
    assert "is_gap_day_enabled" not in runner._TIER_CLIENT_CONFIG["free"]["features"]


def test_deep_merge_is_recursive_and_pure():
    base = {"a": {"x": 1, "y": 2}, "b": 3}
    out = runner._deep_merge(base, {"a": {"y": 9, "z": 4}})
    assert out == {"a": {"x": 1, "y": 9, "z": 4}, "b": 3}
    assert base == {"a": {"x": 1, "y": 2}, "b": 3}    # input untouched
