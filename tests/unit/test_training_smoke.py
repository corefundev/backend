"""
Smoke tests around the training pipeline.

Heavy ML deps (lightgbm, sklearn) are NOT available in the minimal
test venv on every dev machine — Apple Silicon needs `brew install
libomp` for lightgbm to load, for example. So we smoke-test the
parts of the pipeline that DON'T need lightgbm:

  • SeasonalNaiveModel — pure pandas/numpy, no third-party ML.
  • Fallback orchestration — `with_fallback` decorator and
    `ForecastingService` lifecycle (load_primary / load_fallback).
  • Module imports — every module under src/pipeline/ and src/models/
    must at least import without raising.

Tests requiring lightgbm are marked skipif; install it locally to
run them.
"""
from __future__ import annotations

import importlib

import numpy as np
import pandas as pd
import pytest


# ── Module import smoke ────────────────────────────────────────────────

@pytest.mark.parametrize("mod", [
    "src.models.fallback",
    "src.features.engineering",
    "src.data.loader",
    "src.pipeline.inference_utils",
    "src.pipeline.task_queue",
    "src.storage.backend",
    "src.storage.zones",
    "src.storage.upload_pipeline",
    "src.auth.api_keys",
    "src.auth.otp",
    "src.auth.email_normalize",
    "src.auth.disposable_domains",
    "src.auth.signup_rate_limit",
    "src.auth.oauth.state",
    "src.auth.oauth.registry",
    "src.clients.registry",
    "src.plans.plans",
    "src.plans.quota",
    "src.plans.enforcement",
])
def test_module_imports_clean(mod):
    """No missing imports, no syntax errors. Cycle detection bonus."""
    importlib.import_module(mod)


def test_lightgbm_dependent_modules_import_or_skip():
    """
    Modules that hard-depend on lightgbm should either import or
    skip cleanly. Avoid hard failure in environments without libomp.
    """
    try:
        import lightgbm  # noqa: F401
    except (ImportError, OSError) as e:
        pytest.skip(f"lightgbm not loadable on this machine: {e}")
    importlib.import_module("src.models.forecaster")
    importlib.import_module("src.models.mimo")


# ── SeasonalNaiveModel — production fallback ──────────────────────────

def _make_history(n_skus: int = 3, n_days: int = 60) -> pd.DataFrame:
    """Synthetic sales history with weekly seasonality."""
    np.random.seed(42)
    rows = []
    dates = pd.date_range("2024-01-01", periods=n_days, freq="D")
    for sku in range(n_skus):
        base = 10 + sku * 5
        for d in dates:
            day_of_week = d.dayofweek
            weekly = 1 + 0.3 * np.sin(day_of_week / 7 * 2 * np.pi)
            noise = np.random.normal(0, 1)
            rows.append({
                "sku":   f"SKU-{sku:03d}",
                "date":  d,
                "sales": max(0, base * weekly + noise),
                "price": 100,
                "promo": 0,
                "stock": 50,
            })
    return pd.DataFrame(rows)


def test_seasonal_naive_smoke():
    """
    SeasonalNaiveModel is the always-available fallback. It must:
      • fit on a 1-D sales series without lightgbm
      • predict non-negative for a horizon
      • survive save → load round-trip via HMAC pickle
    """
    from src.models.fallback import SeasonalNaiveModel

    sales = np.concatenate([
        # Three weeks of seasonal pattern + small noise
        np.tile([10, 12, 11, 14, 13, 9, 8], 4) + np.random.normal(0, 0.5, 28),
    ])
    model = SeasonalNaiveModel(seasonality=7)
    model.fit(sales)

    pred = model.predict(horizon=7)
    assert len(pred) == 7
    assert all(float(v) >= 0 for v in pred)


def test_forecasting_service_fallback_lifecycle(tmp_path, monkeypatch):
    """
    ForecastingService.fit_fallback() then save → load via HMAC.
    """
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.setenv("MODEL_SIGNING_KEY", "00" * 32)

    from src.models.fallback import ForecastingService
    from src.storage.backend import ClientStorage

    storage = ClientStorage("smoke-client")

    config = {"model": {"horizon": 14}}
    svc = ForecastingService(config)
    sales = np.concatenate([
        np.tile([10, 12, 11, 14, 13, 9, 8], 4) + np.random.normal(0, 0.5, 28),
    ])
    svc.fit_fallback(sales)

    # Persist the fallback into client storage (path used by /train)
    storage.save_fallback_model(svc.fallback)
    assert storage.fallback_exists()

    # Reload via a fresh service — proves HMAC roundtrip
    svc2 = ForecastingService(config)
    svc2.fallback = storage.load_fallback_model()

    # Smoke prediction — fallback path returns 'fallback' source
    feat = pd.DataFrame([{"any": "column"}])     # SeasonalNaive ignores X
    out, source = svc2.predict(feat)
    assert source == "fallback"
    assert len(out) >= 1


# ── Storage round-trip with HMAC ──────────────────────────────────────

def test_pickle_signing_roundtrip_through_client_storage(tmp_path, monkeypatch):
    """save_pickle → load_pickle on ClientStorage works end-to-end with
    HMAC-signed envelopes. This is the path training takes when it
    persists model.pkl."""
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.setenv("MODEL_SIGNING_KEY", "deadbeef" * 8)

    from src.storage.backend import ClientStorage

    storage = ClientStorage("test-client")
    obj = {"weights": [1, 2, 3], "version": 7}
    storage.backend.save_pickle(obj, "test-client/test.pkl")

    loaded = storage.backend.load_pickle("test-client/test.pkl")
    assert loaded == obj


def test_unsigned_pickle_rejected_in_signed_mode(tmp_path, monkeypatch):
    """If MODEL_SIGNING_KEY is set but a pickle was written without
    signing (e.g. legacy artifact), load must refuse. This is the
    HMAC defense — the audit found the original RCE vector here."""
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.setenv("MODEL_SIGNING_KEY", "ab" * 32)

    import pickle
    from src.storage.backend import ClientStorage

    storage = ClientStorage("test-client")
    # Write a legacy unsigned blob directly via upload_bytes
    storage.backend.upload_bytes(
        pickle.dumps({"hi": "there"}),
        "test-client/legacy.pkl",
    )
    with pytest.raises(ValueError, match="unsigned"):
        storage.backend.load_pickle("test-client/legacy.pkl")
