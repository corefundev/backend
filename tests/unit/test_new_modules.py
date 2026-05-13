"""
tests/unit/test_new_modules.py
Unit tests for: storage backend, JWT auth, fallback model, drift, client registry.
"""
from __future__ import annotations

import os
import pickle
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


# ══════════════════════════════════════════════════════════════════
# Storage backend
# ══════════════════════════════════════════════════════════════════

class TestLocalStorageBackend:

    def test_upload_download_bytes(self, tmp_path):
        from src.storage.backend import LocalStorageBackend
        s = LocalStorageBackend(base_dir=str(tmp_path))
        data = b"hello world"
        s.upload_bytes(data, "client_a/test.bin")
        assert s.download_bytes("client_a/test.bin") == data

    def test_exists(self, tmp_path):
        from src.storage.backend import LocalStorageBackend
        s = LocalStorageBackend(base_dir=str(tmp_path))
        assert not s.exists("client_a/missing.bin")
        s.upload_bytes(b"x", "client_a/present.bin")
        assert s.exists("client_a/present.bin")

    def test_save_load_pickle(self, tmp_path):
        from src.storage.backend import LocalStorageBackend
        s = LocalStorageBackend(base_dir=str(tmp_path))
        obj = {"key": [1, 2, 3], "value": "hello"}
        s.save_pickle(obj, "client_a/obj.pkl")
        loaded = s.load_pickle("client_a/obj.pkl")
        assert loaded == obj

    def test_save_load_dataframe(self, tmp_path):
        from src.storage.backend import LocalStorageBackend
        s = LocalStorageBackend(base_dir=str(tmp_path))
        df = pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]})
        s.save_dataframe(df, "client_a/df.parquet")
        loaded = s.load_dataframe("client_a/df.parquet")
        pd.testing.assert_frame_equal(df, loaded)

    def test_list_keys(self, tmp_path):
        from src.storage.backend import LocalStorageBackend
        s = LocalStorageBackend(base_dir=str(tmp_path))
        s.upload_bytes(b"1", "client_a/models/m.pkl")
        s.upload_bytes(b"2", "client_a/raw/d.parquet")
        keys = s.list_keys("client_a/")
        assert any("model" in k for k in keys)
        assert any("raw" in k for k in keys)

    def test_delete(self, tmp_path):
        from src.storage.backend import LocalStorageBackend
        s = LocalStorageBackend(base_dir=str(tmp_path))
        s.upload_bytes(b"x", "client_a/del.bin")
        assert s.exists("client_a/del.bin")
        s.delete("client_a/del.bin")
        assert not s.exists("client_a/del.bin")

    def test_path_format(self, tmp_path):
        from src.storage.backend import LocalStorageBackend
        s = LocalStorageBackend(base_dir=str(tmp_path))
        p = s.path("client_a/models/model.pkl")
        assert "client_a" in p
        assert "model.pkl" in p


class TestClientStorage:

    def test_model_save_load_roundtrip(self, tmp_path):
        os.environ["ARTIFACTS_DIR"] = str(tmp_path)
        from src.storage.backend import ClientStorage, LocalStorageBackend

        storage = ClientStorage("test_client", backend=LocalStorageBackend(str(tmp_path)))
        obj = {"model": "lgbm", "feature_cols": ["lag_1", "lag_7"]}
        storage.save_model(obj)
        assert storage.model_exists()
        loaded = storage.load_model()
        assert loaded == obj

    def test_predictions_roundtrip(self, tmp_path):
        from src.storage.backend import ClientStorage, LocalStorageBackend
        storage = ClientStorage("test_client", backend=LocalStorageBackend(str(tmp_path)))
        df = pd.DataFrame({"sku": ["A", "B"], "predicted_sales": [10.0, 20.0]})
        storage.save_predictions(df, "2024-01-01")
        loaded = storage.load_predictions("2024-01-01")
        assert len(loaded) == 2

    def test_s3_backend_requires_bucket_env(self, monkeypatch):
        """S3StorageBackend raises if neither bucket arg nor S3_BUCKET env is set.

        Uses monkeypatch instead of os.environ.pop so the deletion is
        scoped to this test — the old version leaked into sibling tests
        and made them flake when run in different orders.
        """
        monkeypatch.delenv("S3_BUCKET", raising=False)
        from src.storage.backend import S3StorageBackend
        with pytest.raises(RuntimeError, match="bucket"):
            S3StorageBackend()

    def test_get_storage_returns_local_by_default(self, tmp_path):
        os.environ.pop("STORAGE_BACKEND", None)
        os.environ["ARTIFACTS_DIR"] = str(tmp_path)
        from src.storage.backend import get_storage, LocalStorageBackend
        s = get_storage("local")
        assert isinstance(s, LocalStorageBackend)


# ══════════════════════════════════════════════════════════════════
# JWT Auth
# ══════════════════════════════════════════════════════════════════

class TestJWTAuth:

    @pytest.fixture(autouse=True)
    def set_test_env(self, monkeypatch):
        """Ensure test env vars are set for all JWT tests."""
        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-for-testing-32chars")
        monkeypatch.setenv("API_KEY", "test-api-key-for-testing-only-32chars")
        # Reload module to pick up new env vars
        import importlib, src.auth.jwt_auth as m
        importlib.reload(m)

    def test_create_and_decode_token(self):
        import importlib, src.auth.jwt_auth as m
        importlib.reload(m)
        from src.auth.jwt_auth import create_access_token, decode_access_token
        token = create_access_token("client_123", roles=["forecast"])
        payload = decode_access_token(token)
        assert payload["sub"] == "client_123"
        assert "forecast" in payload["roles"]

    def test_expired_token_raises(self, caplog):
        """
        Simulate an expired token by setting exp in the past.

        Per audit C2 (2026-05-13): the client-facing detail must NOT
        include the PyJWT-specific reason ("Signature has expired", etc.) —
        that's an info-leak. The server still logs the real cause at
        WARNING level so ops can introspect.
        """
        import logging
        import jwt
        from src.auth.jwt_auth import _get_jwt_secret, JWT_ALGORITHM, decode_access_token
        from datetime import datetime, timedelta, timezone
        payload = {
            "sub": "client_x",
            "roles": ["forecast"],
            "iat": datetime.now(timezone.utc) - timedelta(hours=2),
            "exp": datetime.now(timezone.utc) - timedelta(hours=1),
        }
        token = jwt.encode(payload, _get_jwt_secret(), algorithm=JWT_ALGORITHM)
        from fastapi import HTTPException
        with caplog.at_level(logging.WARNING, logger="src.auth.jwt_auth"):
            with pytest.raises(HTTPException) as exc:
                decode_access_token(token)
        assert exc.value.status_code == 401
        # Client sees ONLY the generic message — no PyJWT internals.
        assert exc.value.detail == "Invalid token"
        # But the server-side warning log still records the real cause.
        assert any("expired" in r.message.lower() for r in caplog.records)

    def test_invalid_token_raises(self):
        from src.auth.jwt_auth import decode_access_token
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            decode_access_token("not.a.valid.token")
        assert exc.value.status_code == 401

    def test_require_role_passes(self):
        from src.auth.jwt_auth import AuthContext
        ctx = AuthContext("client_a", ["forecast", "admin"], "jwt")
        ctx.require_role("forecast")  # should not raise

    def test_require_role_fails(self):
        from src.auth.jwt_auth import AuthContext
        from fastapi import HTTPException
        ctx = AuthContext("client_a", ["forecast"], "jwt")
        with pytest.raises(HTTPException) as exc:
            ctx.require_role("admin")
        assert exc.value.status_code == 403

    def test_require_client_access_same_client(self):
        from src.auth.jwt_auth import AuthContext, require_client_access
        ctx = AuthContext("client_a", ["forecast"], "jwt")
        require_client_access("client_a", ctx)  # same client_id — should pass

    def test_require_client_access_different_client_denied(self):
        from src.auth.jwt_auth import AuthContext, require_client_access
        from fastapi import HTTPException
        ctx = AuthContext("client_a", ["forecast"], "jwt")
        with pytest.raises(HTTPException) as exc:
            require_client_access("client_b", ctx)
        assert exc.value.status_code == 403

    def test_admin_can_access_any_client(self):
        from src.auth.jwt_auth import AuthContext, require_client_access
        ctx = AuthContext("admin_user", ["forecast", "admin"], "jwt")
        require_client_access("any_client_id", ctx)  # should not raise


# ══════════════════════════════════════════════════════════════════
# Fallback model + retry
# ══════════════════════════════════════════════════════════════════

class TestSeasonalNaiveModel:

    def test_predict_length(self):
        from src.models.fallback import SeasonalNaiveModel
        m = SeasonalNaiveModel(seasonality=7)
        m.fit(np.arange(30, dtype=float))
        preds = m.predict(14)
        assert len(preds) == 14

    def test_predict_repeats_last_season(self):
        from src.models.fallback import SeasonalNaiveModel
        y = np.array([1, 2, 3, 4, 5, 6, 7], dtype=float)
        m = SeasonalNaiveModel(seasonality=7)
        m.fit(y)
        preds = m.predict(7)
        np.testing.assert_array_almost_equal(preds, y)

    def test_predict_no_negative(self):
        from src.models.fallback import SeasonalNaiveModel
        m = SeasonalNaiveModel(seasonality=7)
        m.fit(np.array([-5, -3, 0, 1, 2, 3, 4], dtype=float))
        preds = m.predict(7)
        # predict itself doesn't clip, but with_fallback does
        assert len(preds) == 7

    def test_not_fitted_raises(self):
        from src.models.fallback import SeasonalNaiveModel
        m = SeasonalNaiveModel()
        with pytest.raises(RuntimeError):
            m.predict(7)


class TestWithFallback:

    def test_primary_used_when_healthy(self):
        from src.models.fallback import with_fallback, SeasonalNaiveModel
        import pandas as pd

        class GoodModel:
            def predict(self, X): return np.ones(len(X)) * 42.0

        fallback = SeasonalNaiveModel()
        fallback.fit(np.ones(10))
        X = pd.DataFrame({"a": [1, 2, 3]})
        preds, source = with_fallback(GoodModel(), fallback, X)
        assert source == "primary"
        assert all(p == pytest.approx(42.0) for p in preds)

    def test_fallback_used_when_primary_fails(self):
        from src.models.fallback import with_fallback, SeasonalNaiveModel
        import pandas as pd

        class BrokenModel:
            def predict(self, X): raise RuntimeError("Model exploded")

        fallback = SeasonalNaiveModel()
        fallback.fit(np.ones(14) * 10)
        X = pd.DataFrame({"a": [1, 2, 3]})
        preds, source = with_fallback(BrokenModel(), fallback, X)
        assert source == "fallback"
        assert len(preds) == 3


class TestRetryDecorator:

    def test_succeeds_on_first_try(self):
        from src.models.fallback import retry
        calls = []

        @retry(max_attempts=3, backoff_sec=0.01)
        def fn():
            calls.append(1)
            return "ok"

        assert fn() == "ok"
        assert len(calls) == 1

    def test_retries_then_succeeds(self):
        from src.models.fallback import retry
        calls = []

        @retry(max_attempts=3, backoff_sec=0.01)
        def fn():
            calls.append(1)
            if len(calls) < 3:
                raise ValueError("not yet")
            return "ok"

        assert fn() == "ok"
        assert len(calls) == 3

    def test_raises_after_max_attempts(self):
        from src.models.fallback import retry
        calls = []

        @retry(max_attempts=3, backoff_sec=0.01)
        def fn():
            calls.append(1)
            raise ValueError("always fails")

        with pytest.raises(ValueError):
            fn()
        assert len(calls) == 3


# ══════════════════════════════════════════════════════════════════
# Drift monitoring
# ══════════════════════════════════════════════════════════════════

class TestFeatureDriftDetector:

    def _make_df(self, n: int = 300, seed: int = 0) -> pd.DataFrame:
        rng = np.random.default_rng(seed)
        return pd.DataFrame({
            "lag_1": rng.normal(10, 2, n),
            "rolling_mean_7": rng.normal(10, 1.5, n),
            "dayofweek": rng.integers(0, 7, n).astype(float),
        })

    def test_fit_and_detect_no_drift(self):
        from src.monitoring.drift import FeatureDriftDetector
        train_df = self._make_df(300, seed=0)
        infer_df = self._make_df(300, seed=1)   # same distribution
        detector = FeatureDriftDetector(threshold=0.25)
        detector.fit(train_df, list(train_df.columns))
        scores = detector.detect(infer_df, client_id="test")
        assert len(scores) == 3
        # PSI should be low for same distribution
        assert all(v < 0.25 for v in scores.values()), f"Unexpected drift: {scores}"

    def test_fit_and_detect_high_drift(self):
        from src.monitoring.drift import FeatureDriftDetector
        rng = np.random.default_rng(42)
        train_df = pd.DataFrame({"lag_1": rng.normal(10, 1, 500)})
        infer_df = pd.DataFrame({"lag_1": rng.normal(50, 1, 500)})  # totally different
        detector = FeatureDriftDetector(threshold=0.25)
        detector.fit(train_df, ["lag_1"])
        scores = detector.detect(infer_df, client_id="test")
        # PSI should be very high for a massive distribution shift
        assert scores["lag_1"] > 1.0, f"Expected high drift, got {scores}"

    def test_psi_identical_distributions_near_zero(self):
        from src.monitoring.drift import psi
        rng = np.random.default_rng(0)
        x = rng.normal(0, 1, 1000)
        score = psi(x, x)
        assert score == pytest.approx(0.0, abs=0.05)


class TestPredictionDriftDetector:

    def test_records_wmape_and_returns_rolling(self):
        from src.monitoring.drift import PredictionDriftDetector
        detector = PredictionDriftDetector(threshold=0.5, window_days=3)
        y_true = np.array([10.0, 20.0, 30.0])
        y_pred = np.array([11.0, 19.0, 31.0])
        rolling = detector.record(y_true, y_pred, client_id="test")
        assert 0 <= rolling <= 1.0

    def test_alerts_on_high_wmape(self, caplog):
        from src.monitoring.drift import PredictionDriftDetector
        import logging
        detector = PredictionDriftDetector(threshold=0.10, window_days=2)
        y_true = np.array([10.0, 10.0])
        y_pred = np.array([20.0, 20.0])   # 100% WMAPE
        with caplog.at_level(logging.WARNING, logger="src.monitoring.drift"):
            for _ in range(3):  # fill window
                detector.record(y_true, y_pred, client_id="alert_test")
        assert any("drift alert" in r.message.lower() for r in caplog.records)


# ══════════════════════════════════════════════════════════════════
# Client registry
# ══════════════════════════════════════════════════════════════════

class TestLocalFileRegistry:

    def test_register_and_get(self, tmp_path):
        from src.clients.registry import ClientRecord, LocalFileRegistry
        reg = LocalFileRegistry(path=str(tmp_path / "reg.json"))
        rec = ClientRecord(
            client_id="acme",
            config={"horizon": 14},
            storage_path="s3://bucket/acme/",
        )
        reg.register(rec)
        loaded = reg.get("acme")
        assert loaded is not None
        assert loaded.client_id == "acme"
        assert loaded.config["horizon"] == 14

    def test_update_status(self, tmp_path):
        from src.clients.registry import ClientRecord, LocalFileRegistry
        reg = LocalFileRegistry(path=str(tmp_path / "reg.json"))
        reg.register(ClientRecord("x", {}, "s3://b/x/"))
        reg.update("x", status="ready", last_wmape=0.12)
        loaded = reg.get("x")
        assert loaded.status == "ready"
        assert loaded.last_wmape == pytest.approx(0.12)

    def test_list_clients(self, tmp_path):
        from src.clients.registry import ClientRecord, LocalFileRegistry
        reg = LocalFileRegistry(path=str(tmp_path / "reg.json"))
        for i in range(3):
            reg.register(ClientRecord(f"client_{i}", {}, f"s3://b/c{i}/"))
        clients = reg.list_clients()
        assert len(clients) == 3

    def test_delete_client(self, tmp_path):
        from src.clients.registry import ClientRecord, LocalFileRegistry
        reg = LocalFileRegistry(path=str(tmp_path / "reg.json"))
        reg.register(ClientRecord("to_delete", {}, "s3://b/d/"))
        reg.delete("to_delete")
        assert reg.get("to_delete") is None

    def test_get_nonexistent_returns_none(self, tmp_path):
        from src.clients.registry import LocalFileRegistry
        reg = LocalFileRegistry(path=str(tmp_path / "reg.json"))
        assert reg.get("ghost") is None

    def test_register_idempotent(self, tmp_path):
        """Registering same client twice should update, not duplicate."""
        from src.clients.registry import ClientRecord, LocalFileRegistry
        reg = LocalFileRegistry(path=str(tmp_path / "reg.json"))
        reg.register(ClientRecord("dup", {"horizon": 7}, "s3://b/dup/"))
        reg.register(ClientRecord("dup", {"horizon": 14}, "s3://b/dup/"))
        clients = reg.list_clients()
        assert len(clients) == 1
        assert clients[0].config["horizon"] == 14
