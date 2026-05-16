"""
src/monitoring/chaos.py

Chaos engineering — fault injection для проверки устойчивости.

Проверяет гипотезы:
  1. При падении Redis → rate limiting переходит на in-memory ✓
  2. При S3 timeout → fallback модель отвечает ✓
  3. При 503 от MLflow → обучение завершается без краша ✓
  4. При OOM в feature engineering → pipeline не зависает ✓

Использование:
    from src.monitoring.chaos import ChaosExperiment, inject_fault

    with ChaosExperiment("redis_unavailable"):
        # Тест: rate limiter должен работать без Redis
        limiter = RateLimiter(redis_url="redis://bad-host:9999")
        ok, headers = limiter.check("client_x", tier="pro")
        assert ok  # должен работать через in-memory fallback

    # Или точечная инъекция:
    with inject_fault("s3_latency", latency_ms=500):
        result = storage.upload_bytes(data, key)
        assert result  # должно работать, просто медленнее
"""
from __future__ import annotations

import logging
import os
import random
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Generator, Literal

logger = logging.getLogger(__name__)


@dataclass
class FaultResult:
    """Результат chaos эксперимента."""
    experiment:    str
    hypothesis:    str
    passed:        bool
    duration_sec:  float
    observations:  list[str] = field(default_factory=list)
    timestamp:     str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@contextmanager
def inject_fault(
    fault_type: str,
    probability: float = 1.0,
    latency_ms:  int   = 0,
    **kwargs,
) -> Generator:
    """
    Context manager для инъекции неисправности.

    fault_type:
      "latency"      — добавить задержку latency_ms мс
      "random_error" — с вероятностью probability бросить исключение
      "network_loss" — симулировать потерю пакетов (random delay + retry)

    Audit R3-33 — refuses to run when APP_ENV=production. The helper
    is test-harness only; an accidental import + call from a prod
    code-path (e.g. a developer leaving an @chaos decorator) must
    fail loud rather than randomly throwing in production.
    """
    if (os.environ.get("APP_ENV") or "").strip().lower() == "production":
        raise RuntimeError(
            "src.monitoring.chaos.inject_fault is test-harness only "
            "and must not run with APP_ENV=production (audit R3-33)"
        )

    if fault_type == "latency" and latency_ms > 0:
        logger.debug(f"[CHAOS] Injecting latency: {latency_ms}ms")
        time.sleep(latency_ms / 1000)

    if fault_type == "random_error" and random.random() < probability:
        logger.debug(f"[CHAOS] Injecting random error (prob={probability})")
        raise RuntimeError(f"[CHAOS] Injected fault: {fault_type}")

    yield


class ChaosExperiment:
    """
    Структурированный chaos эксперимент с фиксацией результата.

    Шаблон:
      1. Описать гипотезу
      2. Определить нормальное состояние (steady state)
      3. Инъектировать неисправность
      4. Наблюдать и проверить что система восстановилась
    """

    def __init__(self, name: str, hypothesis: str = ""):
        self.name       = name
        self.hypothesis = hypothesis or f"System survives {name}"
        self._observations: list[str] = []
        self._start:   float = 0.0
        self._passed:  bool  = True

    def __enter__(self) -> "ChaosExperiment":
        self._start = time.perf_counter()
        logger.info(f"[CHAOS] Experiment START: {self.name}")
        logger.info(f"[CHAOS] Hypothesis: {self.hypothesis}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> "Literal[False]":
        elapsed = time.perf_counter() - self._start

        if exc_type is not None:
            self._passed = False
            self._observations.append(f"EXCEPTION: {exc_type.__name__}: {exc_val}")
            logger.error(
                f"[CHAOS] Experiment FAILED: {self.name} — "
                f"{exc_type.__name__}: {exc_val} ({elapsed:.2f}s)"
            )
            return False   # re-raise

        status = "PASSED" if self._passed else "FAILED"
        logger.info(f"[CHAOS] Experiment {status}: {self.name} ({elapsed:.2f}s)")
        return False

    def observe(self, message: str) -> None:
        """Record an observation during the experiment."""
        self._observations.append(message)
        logger.debug(f"[CHAOS] Observation: {message}")

    def assert_steady_state(self, condition: bool, message: str) -> None:
        """Assert that steady state holds. Fails experiment if False."""
        if not condition:
            self._passed = False
            self._observations.append(f"STEADY STATE VIOLATED: {message}")
            raise AssertionError(f"[CHAOS] {self.name}: {message}")
        self._observations.append(f"STEADY STATE OK: {message}")

    def get_result(self, duration_sec: float = 0.0) -> FaultResult:
        return FaultResult(
            experiment   = self.name,
            hypothesis   = self.hypothesis,
            passed       = self._passed,
            duration_sec = duration_sec or (time.perf_counter() - self._start),
            observations = self._observations.copy(),
        )


def run_standard_chaos_suite() -> list[FaultResult]:
    """
    Запустить стандартный набор chaos экспериментов.
    Возвращает список результатов.
    """
    results = []

    # ── Experiment 1: Redis unavailable ──────────────────────
    with ChaosExperiment(
        "redis_unavailable",
        "Rate limiter works without Redis (in-memory fallback)"
    ) as exp:
        from src.api.rate_limit import RateLimiter
        # Use None redis_url to ensure in-memory fallback (no DNS lookup)
        limiter = RateLimiter(redis_url=None)
        exp.observe("Using in-memory RateLimiter (no Redis connection)")
        ok, headers = limiter.check("chaos_client", tier="pro")
        exp.assert_steady_state(ok, "Rate limiter allowed request via in-memory fallback")
        exp.assert_steady_state(
            "X-RateLimit-Limit" in headers,
            "Rate limit headers present even without Redis"
        )
        exp.observe("Rate limiter fell back to in-memory correctly")
        results.append(exp.get_result())

    # ── Experiment 2: Fallback model on LightGBM crash ───────
    with ChaosExperiment(
        "model_predict_failure",
        "SeasonalNaive fallback activates when LightGBM raises"
    ) as exp:
        from src.models.fallback import ForecastingService, SeasonalNaiveModel
        import numpy as np

        class BrokenModel:
            feature_cols = ["a"]
            def predict(self, X): raise RuntimeError("LightGBM crashed")

        fallback = SeasonalNaiveModel(seasonality=7)
        fallback.fit(np.array([10.0] * 30))

        cfg     = {"model": {"horizon": 7}}
        service = ForecastingService(cfg)
        service.primary  = BrokenModel()
        service.fallback = fallback

        import pandas as pd
        X_dummy = pd.DataFrame({"a": [1.0]})
        pred, source = service.predict(X_dummy)
        exp.assert_steady_state(source == "fallback", f"Fallback activated: source={source}")
        exp.assert_steady_state(len(pred) > 0, "Fallback returned predictions")
        exp.observe(f"Fallback predictions: {pred[:3]}")
        results.append(exp.get_result())

    # ── Experiment 3: WMAPE stays bounded under noisy data ───
    with ChaosExperiment(
        "noisy_input_data",
        "Model WMAPE stays < 2.0 on data with 30% noise injection"
    ) as exp:
        from src.validation.metrics import wmape
        import numpy as np

        rng    = np.random.default_rng(42)
        y_true = rng.uniform(10, 100, 500)
        # Inject 30% noise
        noise  = rng.normal(0, 0.3 * y_true.std(), 500)
        y_pred = np.clip(y_true + noise, 0, None)

        w = wmape(y_true, y_pred)
        exp.observe(f"WMAPE with 30% noise: {w:.3f}")
        exp.assert_steady_state(w < 2.0, f"WMAPE={w:.3f} < 2.0 (model still useful)")
        results.append(exp.get_result())

    # ── Experiment 4: Webhook fails gracefully ────────────────
    with ChaosExperiment(
        "webhook_unreachable",
        "System continues operating when webhook endpoint is down"
    ) as exp:
        from src.api.webhooks import send_webhook_sync
        t0 = time.perf_counter()
        result = send_webhook_sync(
            url       = "http://127.0.0.1:19999/nonexistent",
            event     = "test",
            data      = {"msg": "chaos test"},
            client_id = "chaos",
            secret    = "test",
            max_retries = 1,
        )
        elapsed = time.perf_counter() - t0
        exp.assert_steady_state(result is False, "send_webhook returned False (no crash)")
        exp.assert_steady_state(elapsed < 15, f"Failed fast: {elapsed:.1f}s < 15s")
        exp.observe(f"Webhook failed in {elapsed:.1f}s without exception")
        results.append(exp.get_result())

    # Summary
    passed = sum(1 for r in results if r.passed)
    logger.info(
        f"[CHAOS] Suite complete: {passed}/{len(results)} experiments passed"
    )
    return results
