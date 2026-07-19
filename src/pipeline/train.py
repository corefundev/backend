"""
src/pipeline/train.py

End-to-end training pipeline v2.0:
  load → GE validate → anomaly detection → features (+ weather + holidays)
  → HPO (optional) → MIMO / LightGBM train → walk-forward validate
  → SHAP explainer → cold-start model → fallback → save (S3/local) → MLflow
"""
from __future__ import annotations

import logging
import sys
import time
from typing import Any, Callable, Optional

import pandas as pd


from src.data.loader import load_config, load_data, validate_data
from src.data.ge_validator import validate_with_great_expectations
from src.clients.config_manager import get_config_manager
from src.data.anomaly_detection import SalesAnomalyDetector
from src.features.engineering import build_features, get_feature_columns
from src.models.forecaster import SKUForecaster, log_to_mlflow
from src.models.mimo import MIMOForecaster
from src.models.fallback import SeasonalNaiveModel
from src.models.explainer import SKUExplainer
from src.storage.backend import ClientStorage
from src.validation.walk_forward import walk_forward_validate

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def _progress(step: int, total: int, label: str) -> None:
    """
    Publish progress to the current RQ job's meta dict so the API can
    surface it via /jobs/{id}. Safe to call when run outside a worker
    (e.g., synchronous fallback path) — get_current_job() returns None.
    """
    logger.info(f"Step {step}/{total}: {label}")
    try:
        from rq import get_current_job
        job = get_current_job()
        if job is None:
            return
        job.meta["progress"] = {"step": step, "total": total, "label": label}
        job.save_meta()
    except Exception as e:    # noqa: BLE001
        logger.debug(f"_progress save_meta skipped: {e}")


def _save_and_log_to_mlflow(
    config: dict,
    agg: dict,
    final_model: Any,
    client_id: str,
) -> tuple[str | None, str | None]:
    """Serialize the trained model to a temp pkl and log it to MLflow.

    Returns ``(mlflow_run_id, error)``:
      - ``(run_id, None)``  — logged cleanly.
      - ``(None, "<ExceptionType>: <msg>")`` — MLflow logging failed.
      - ``(None, None)``    — model has no ``save`` (nothing to log).

    MLflow logging is best-effort TELEMETRY: a failure here must NOT fail
    the training run (the model is already trained + saved and forecasts
    are generated). But — unlike the previous log-and-swallow that left
    ``mlflow_run_id`` invisibly NULL (the R8-12 root cause) — the failure
    is returned as EXPLICIT error state so the caller can persist it to
    ``sku_training_runs.mlflow_logging_error`` (queryable + surfaced in
    the training-runs API). The temp pkl is always cleaned up.
    """
    if not hasattr(final_model, "save"):
        return None, None
    import os as _os
    import tempfile
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as tmp:
            tmp_path = tmp.name
        final_model.save(tmp_path)
        run_id = log_to_mlflow(config, agg, final_model, tmp_path, client_id)
        return run_id, None
    except Exception as e:
        # Explicit error return (NOT a silent swallow): the caller
        # persists this so the degraded run is observable.
        logger.error("MLflow logging failed for client %s: %s", client_id, e)
        return None, f"{type(e).__name__}: {e}"
    finally:
        if tmp_path:
            try:
                _os.unlink(tmp_path)
            except OSError:
                pass


def _hpo_tune_slice(df: pd.DataFrame, date_col: str, report_tail_days: int) -> pd.DataFrame:
    """#180: the inner slice HPO tunes on = `df` minus the final report-tail
    window (the last `report_tail_days` days, which walk-forward later TESTS on).

    Holding that tail out of HPO makes the reported walk-forward metric (step 6,
    on the full df) an honest OUT-OF-SAMPLE estimate instead of the HPO selection
    objective — HPO can no longer tune against the very windows it's graded on.
    """
    cutoff = df[date_col].max() - pd.Timedelta(days=report_tail_days)
    return df[df[date_col] <= cutoff]


def _fit_quantiles_with_conformal(final_model, df, X, y, config, agg,
                                  target_censor=None) -> None:
    """#151 CQR: fit the quantile heads on a temporal PROPER subset (rows
    before the calibration window), then calibrate their coverage on the
    held-out most-recent `conformal_cal_days` — see src/models/conformal.py.

    The POINT-forecast heads keep the full history (they are not
    conformalized); only the quantile sub-models give up the tail, which is
    what makes the conformity scores honest (the quantile models never saw
    those targets). Too-short history → quantiles fit on full data, raw
    intervals served, loud warning (never a broken/∞ band).

    Scalar calibration metrics are merged into `agg` (flat floats only — the
    dict flows to MLflow log_metrics and sku_training_runs).
    """
    sku_col   = config["data"]["sku_col"]
    date_col  = config["data"]["date_col"]
    model_cfg = config.get("model", {})
    enabled   = bool(model_cfg.get("conformal_enabled", True))
    cal_days  = int(model_cfg.get("conformal_cal_days", 28))
    alpha     = float(model_cfg.get("conformal_alpha", 0.2))
    horizon   = int(model_cfg.get("horizon", 14))

    dates     = pd.to_datetime(df[date_col])
    cal_start = dates.max() - pd.Timedelta(days=cal_days - 1)
    proper    = dates < cal_start
    proper_dates = int(dates[proper].nunique())

    if not enabled or proper_dates <= 2 * horizon:
        if enabled:
            logger.warning(
                "#151: history too short for a conformal calibration split "
                "(%d proper dates ≤ 2×horizon=%d) — quantile heads fit on full "
                "data, RAW (uncalibrated) intervals served",
                proper_dates, 2 * horizon,
            )
        final_model.fit_quantiles(X, y, groups=df[sku_col],
                                  target_censor=target_censor)
        return

    final_model.fit_quantiles(
        X[proper], y[proper], groups=df[sku_col][proper],
        target_censor=None if target_censor is None else target_censor[proper],
    )
    summary = final_model.calibrate_conformal(
        X, y, dates=dates, cal_start=cal_start, groups=df[sku_col], alpha=alpha,
        target_censor=target_censor,
    )
    if summary:
        agg["conformal_alpha"]           = summary["alpha"]
        agg["conformal_n_cal"]           = summary["n_cal"]
        agg["conformal_coverage_pre"]    = summary["coverage_pre"]
        agg["conformal_coverage_post"]   = summary["coverage_post"]
        agg["conformal_correction_mean"] = summary["correction_mean"]
        agg["pinball_p10_post"]          = summary["pinball_lo_post"]
        agg["pinball_p90_post"]          = summary["pinball_hi_post"]


def _seasonal_naive_wmape_on_frame(combined, date_col) -> float:
    """#247: seasonal-naive-7 WMAPE on the IDENTICAL (sku, fold, date) rows
    the challenger was graded on. Each row carries its fold's per-SKU train
    series (`train_values`); the naive prediction for step h cycles the last
    trained week: tail7[(h-1) % 7]. Short histories (<7) fall back to the
    last trained value. Returns NaN on an unusable frame (gate treats NaN
    baselines fail-open at the infra level — never a fabricated number)."""
    import numpy as np
    if combined is None or len(combined) == 0:
        return float("nan")
    need = {"fold", "actual", "train_values", date_col}
    if not need.issubset(combined.columns):
        return float("nan")
    fold_start = combined.groupby("fold")[date_col].transform("min")
    h = (pd.to_datetime(combined[date_col]) - pd.to_datetime(fold_start)).dt.days + 1
    err = 0.0
    act = 0.0
    for tv, hh, a in zip(combined["train_values"], h, combined["actual"]):
        arr = np.asarray(tv, dtype=float) if tv is not None else np.empty(0)
        if arr.size >= 7:
            pred = float(arr[-7:][(int(hh) - 1) % 7])
        elif arr.size:
            pred = float(arr[-1])
        else:
            continue
        err += abs(float(a) - pred)
        act += abs(float(a))
    return err / act if act > 1e-10 else float("nan")


def _promotion_gate(df, config, agg, client_id, wf_combined=None):
    """QW2-4 (#227): champion/challenger promotion decision, fail-closed on
    verdicts and fail-open ONLY on gate infrastructure errors.

    challenger = this run's walk-forward pooled metrics (agg);
    champion   = the client's newest FINISHED run with a persisted wmape;
    baseline   = seasonal-naive(7) scored on the same df's holdout tail.

    Returns (verdict_or_None, block: bool). block=True ⇒ the caller must NOT
    save the model (the champion artifact keeps serving) — only a legitimate
    FAILED verdict with an existing champion blocks. First model (no
    champion) always promotes: blocking it would leave the client with no
    model at all, while the SeasonalNaive serve fallback still floors quality
    — the verdict is persisted so the below-baseline fact is visible.
    Infrastructure errors (registry down, baseline scoring crash) log loudly
    and promote — the gate must never brick training because of its own bugs.
    """
    from types import SimpleNamespace
    if not bool(config.get("model", {}).get("promotion_gate", True)):
        return None, False
    try:
        from src.validation.backtest_gate import evaluate_gate, score_seasonal_naive
        horizon = int(config["model"].get("horizon", 14))
        tolerance = float(config.get("model", {}).get("gate_tolerance", 0.02))
        date_col = config["data"]["date_col"]

        champion = None
        try:
            from src.storage.training_runs import FINISHED, get_training_runs_registry
            for r in get_training_runs_registry().list_for_client(client_id, limit=20):
                # Era markers: a champion metric is comparable ONLY when it
                # was measured under the SAME evaluation methodology as the
                # challenger; otherwise first-model semantics. Each ruler
                # change adds a presence-marker (era fields are monotone in
                # time, so requiring them here can never select a stale
                # non-serving run over the actual serving artifact):
                #   • mase_seasonal (#247b) — post-2026-06-28 honest
                #     methodology (#180 HPO holdout / #181 / #182);
                #   • eval_coverage (#540) — MA-1 #519 honest eval universe
                #     (dead tails scored, 100% window coverage). Sparse-era
                #     WMAPEs are optically ~20% lower; live case: challenger
                #     0.512 beat naive 0.677 yet was blocked by a sparse-era
                #     champion 0.407.
                # #268: champion = newest run whose ARTIFACT exists (it is
                # what actually serves) — NOT gate_passed: a promoted-first-
                # model with an honest FAIL verdict must become the champion,
                # else every next retrain sees "no champion" forever.
                if (r.status == FINISHED and r.wmape is not None
                        and getattr(r, "model_path", None)
                        and getattr(r, "mase_seasonal", None) is not None
                        and getattr(r, "eval_coverage", None) is not None):
                    champion = SimpleNamespace(wmape_global=float(r.wmape),
                                               mase_global=float(r.mase or "nan"))
                    break
        except Exception as reg_err:    # noqa: BLE001 — no registry ≠ no gate
            logger.warning("#227 gate: champion lookup failed (%s) — treating as first model", reg_err)

        # #247: score the naive on the SAME walk-forward windows the
        # challenger was graded on — a single-tail baseline (the original
        # wiring) never faces the hard folds and biases the gate
        # anti-challenger (live case: challenger 0.837 over 3 folds vs a
        # one-window naive 0.393). Single-tail stays as a defensive fallback
        # when the frame is unavailable.
        bl_wmape = _seasonal_naive_wmape_on_frame(wf_combined, date_col)
        if bl_wmape == bl_wmape:  # not NaN
            baseline = SimpleNamespace(wmape_global=bl_wmape,
                                       mase_global=float("nan"))
        else:
            baseline = score_seasonal_naive(df, config, holdout_days=horizon)
        challenger = SimpleNamespace(
            wmape_global=float(agg.get("wmape_global", float("nan"))),
            mase_global=float(agg.get("mase_global", float("nan"))),
        )
        verdict = evaluate_gate(champion, challenger, baseline, tolerance=tolerance)
        agg["gate_passed"] = float(verdict.passed)
        # DS-2 #467: persist the naive baseline the gate already computed —
        # the dataset model card shows «точнее наивного на N%» from it.
        _bl = float(getattr(baseline, "wmape_global", float("nan")))
        if _bl == _bl:  # not NaN
            agg["baseline_wmape"] = _bl
        if verdict.passed:
            logger.info("#227 gate PASSED: %s", verdict.as_dict())
            return verdict, False
        if champion is None:
            logger.warning(
                "#227 gate FAILED but no champion exists — promoting the first "
                "model anyway (below-baseline fact persisted): %s", verdict.reasons,
            )
            return verdict, False
        logger.warning(
            "#227 gate FAILED — challenger BLOCKED, champion keeps serving: %s",
            verdict.reasons,
        )
        return verdict, True
    except Exception as e:    # noqa: BLE001 — infra fail-open, loud
        logger.error("#227 gate infrastructure error — promoting WITHOUT a "
                     "verdict (fail-open on gate bugs only): %s", e, exc_info=True)
        return None, False


def run_training_pipeline(
    data_path: str,
    config_path: str = "configs/config.yaml",
    client_id:  str = "default",
    dataset_id: Optional[str] = None,
    dataset_is_default: bool = False,
) -> dict:
    t0 = time.time()
    logger.info(f"=== Training pipeline START | client={client_id} ===")

    # ── Load system config ───────────────────────────────────
    config = load_config(config_path)

    # ── Apply per-client config overrides ────────────────────
    # Client config is merged ON TOP of system config.
    # System config.yaml is never modified.
    record = None
    try:
        from src.clients.registry import get_registry
        registry = get_registry()
        mgr      = get_config_manager(config_path)
        record   = registry.get(client_id)
        config   = mgr.get_effective(client_id, registry)
        logger.info(f"Config for client={client_id}: applied per-client overrides")
    except Exception as e:
        logger.warning(f"Could not load per-client config overrides: {e}. Using system defaults.")
        record = None

    # ── Plan-tier defaults ────────────────────────────────────
    # Each plan has its own HPO budget (Free 0 / Start 15 / Business 30),
    # default loss objective (Free→mse / Start, Business→ensemble), and RU
    # external-regressor default (auto-on for paid tiers; Free off to keep
    # free trainings fast and avoid ЦБ РФ load). A default is applied ONLY
    # when the client hasn't set the gating key — explicit user choice wins.
    #
    # #96: driven declaratively off the actual client override instead of
    # hand-listed per-key tracking booleans. Each row co-locates the GATE key (its
    # presence in client_cfg suppresses the default) with the TARGET it
    # writes, so a new plan default can't silently clobber a user's explicit
    # value by forgetting to add a matching flag (the trap backtest_runner
    # warns about).
    try:
        # Модель-аудит H1: единый источник план-дефолтов — config_manager.
        # apply_plan_defaults(config, record) обязана давать РОВНО тот же
        # конфиг, что и serve-пути (get_effective_serving) — иначе
        # feature_cols модели разойдутся с serve-фреймом.
        from src.clients.config_manager import apply_plan_defaults
        apply_plan_defaults(config, record)
        logger.info(
            f"Plan-tier defaults: plan={record.plan if record else 'free'} "
            f"hpo_n_trials={config.get('hpo', {}).get('n_trials', 0)} "
            f"objective={config.get('model', {}).get('objective', 'mse')} "
            f"regressors_ru={config.get('features', {}).get('external_regressors_ru', {}).get('enabled', False)}"
        )
    except Exception as e:    # noqa: BLE001
        logger.warning(f"Could not apply plan defaults: {e}")

    # DS-1 #466: датасет = своя модель — артефакты уезжают в
    # {client}/datasets/{dataset}/… . Для ДЕФОЛТНОГО датасета модель
    # дублируется и в легаси-слот клиента (его читают /predict и
    # существующие клиентские потоки) — см. сохранение ниже.
    storage = ClientStorage(client_id, dataset_id=dataset_id)
    logger.info(f"Storage: {storage.backend.__class__.__name__} → {storage.path('models/model.pkl')}")

    # ── 1. Load ───────────────────────────────────────────────
    _progress(1, 9, "Загрузка данных")
    df = load_data(data_path, config)

    # ── 2. GE Validation ──────────────────────────────────────
    _progress(2, 9, "Проверка качества (Great Expectations)")
    ge_result = validate_with_great_expectations(df, config, raise_on_failure=True)
    df = validate_data(df, config)
    storage.save_raw_data(df)

    # ── 3. Anomaly detection (configure) ──────────────────────
    # #183: detection RUNS in step 4 (after build_features) so the global
    # IsolationForest can see the lag_/rolling_ feature columns — running it
    # here (pre-features) left those columns absent, so only per-SKU IQR ever
    # fired. Build the detector now; apply it once features exist.
    _progress(3, 9, "Поиск аномалий")
    anom_cfg = config.get("anomaly_detection", {})

    def _new_detector() -> SalesAnomalyDetector:
        return SalesAnomalyDetector(
            contamination=anom_cfg.get("contamination", 0.05),
            iqr_factor=anom_cfg.get("iqr_factor", 3.0),
            anomaly_weight=anom_cfg.get("anomaly_weight", 0.1),
            sparse_iqr_guard=anom_cfg.get("sparse_iqr_guard", True),
        )

    anomaly_enabled = anom_cfg.get("enabled", True)

    # ── 4. Feature engineering ────────────────────────────────
    _progress(4, 9, "Построение признаков (календарь, лаги, погода, праздники)")
    df = build_features(df, config)
    # #229: market features — MODEL-CARRIED state (см. src/features/market.py:
    # serve-parity запрещает считать их в build_features). Train-мерж по
    # полному ряду; хвост вшивается в модель перед save (attach ниже).
    #
    # #380 (2026-07-11): default OFF. Честный 3-fold walk-forward на живом
    # 1c-датасете: +5.05% WMAPE ВРЕДА (шаг 1 горизонта +58%) — абсолютные
    # уровни каталога нестационарны (−60% за период) и не переносятся
    # вперёд. Включение — осознанный opt-in через client config
    # (features.market.enabled: true); кандидаты на возврат в default —
    # нормализованные варианты (share-only/momentum) после замера стендом.
    market_series = None
    if bool(config.get("features", {}).get("market", {}).get("enabled", False)):
        from src.features.market import compute_market_tail, merge_market_features
        market_series = compute_market_tail(df, config["data"]["date_col"],
                                            config["data"]["target_col"])
        df = merge_market_features(df, market_series, config["data"]["date_col"])
        logger.info("#229: market features ENABLED by client config (opt-in)")

    # #307: cross-series static positioning (velocity_band, price_tier) —
    # тот же MODEL-CARRIED контракт, что у market. Карта строится на полном
    # train-фрейме, вшивается в артефакт (attach ниже), на serve мержится.
    from src.features.static_features import (
        compute_static_map,
        fold_static_recompute,
        merge_static_features,
    )
    static_map = compute_static_map(df, config["data"]["sku_col"],
                                    config["data"]["target_col"])
    df = merge_static_features(df, static_map, config["data"]["sku_col"])

    # AUD-6 (#358): walk-forward/HPO must NOT see this full-frame map — it
    # was computed over the report-tail days they grade on (velocity_band
    # partly encodes the test-window sales level → optimistic metric, gate,
    # HPO). Each fold rebuilds the map from its own train rows; the final
    # fit below keeps the full-frame map (full df = full train, serve-parity).
    def _fold_statics(train_df: Any, test_df: Any) -> tuple:
        return fold_static_recompute(train_df, test_df,
                                     config["data"]["sku_col"],
                                     config["data"]["target_col"])

    feature_cols = get_feature_columns(df, config)
    storage.save_features(df)
    logger.info(f"  {len(feature_cols)} features, {len(df)} rows")

    target_col = config["data"]["target_col"]
    sku_col    = config["data"]["sku_col"]

    # #228 OOS demand censoring: zero sales on a zero-stock day is a CENSORED
    # observation, not zero demand — such target days teach the model to
    # under-forecast exactly the SKUs that stock out. When the client ships a
    # stock column, rows whose TARGET day is OOS are dropped per horizon head
    # (point + quantile fits + conformal scores). No stock column → no-op.
    target_censor: Optional[Any] = None
    target_censor_fn: Optional[Callable[[Any], Any]] = None
    if bool(config.get("model", {}).get("oos_censoring", True)) and "is_oos" in df.columns:
        n_oos = int((df["is_oos"] > 0).sum())
        if n_oos > 0:
            target_censor = df["is_oos"]
            target_censor_fn = lambda fold_df: fold_df["is_oos"]   # noqa: E731
            logger.info("#228: OOS censoring active — %d censored day-rows (%.2f%%)",
                        n_oos, 100.0 * n_oos / max(len(df), 1))

    # #183: with features present, detect anomalies on the feature frame and
    # build per-row sample weights for the FINAL fit. `is_anomaly` is excluded
    # from feature_cols (get_feature_columns) so it never becomes a model input.
    sample_weights_full: Optional[Any] = None
    sample_weight_fn: Optional[Callable[[Any], Any]] = None
    if anomaly_enabled:
        df, sample_weights_full = _new_detector().fit_detect(
            df, sku_col=sku_col, target_col=target_col,
        )

        # Fold-aware weights for honest walk-forward: each fold recomputes
        # weights on its OWN train rows only — never test-fold data (avoids the
        # L-A3 preprocessing-leakage trap) — so the reported metric reflects the
        # same anomaly-weighted fit the final model uses (closes the M-A8 skew).
        def _fold_weights(train_df: Any) -> Any:
            _, w = _new_detector().fit_detect(
                train_df, sku_col=sku_col, target_col=target_col,
            )
            return w

        sample_weight_fn = _fold_weights

    # B1 #319: recency decay — recent rows count more. Composed
    # MULTIPLICATIVELY with the anomaly weights above (orthogonal signals);
    # per-fold weights anchor to the fold's OWN cutoff (train_df max date),
    # so walk-forward stays fold-clean. Default OFF until the honest 1c-live
    # bench shows a gain (measure-first; см. журнал замеров).
    rd_cfg = config.get("recency_decay", {}) or {}
    if rd_cfg.get("enabled", False):
        import numpy as np

        from src.data.recency import (
            DEFAULT_FLOOR, DEFAULT_HALF_LIFE_DAYS, recency_weights,
        )
        _rd_hl    = rd_cfg.get("half_life_days", DEFAULT_HALF_LIFE_DAYS)
        _rd_floor = rd_cfg.get("floor", DEFAULT_FLOOR)
        _date_col = config["data"]["date_col"]
        _rec_full = recency_weights(df, _date_col,
                                    half_life_days=_rd_hl, floor=_rd_floor)
        sample_weights_full = (
            _rec_full if sample_weights_full is None
            else np.asarray(sample_weights_full) * _rec_full
        )
        _anom_weight_fn = sample_weight_fn

        def _rec_fold_weights(train_df: Any) -> Any:
            w = recency_weights(train_df, _date_col,
                                half_life_days=_rd_hl, floor=_rd_floor)
            if _anom_weight_fn is not None:
                w = np.asarray(_anom_weight_fn(train_df)) * w
            return w

        sample_weight_fn = _rec_fold_weights
        logger.info("#319: recency decay ENABLED (half_life=%sd, floor=%s)",
                    _rd_hl, _rd_floor)

    X = df[feature_cols]
    y = df[target_col]

    # ── 5. HPO (optional) ─────────────────────────────────────
    # #180: tune HPO on an INNER slice that EXCLUDES the walk-forward report tail
    # (the last n_splits*horizon days step 6 tests on). Otherwise HPO minimizes
    # error on the very windows the reported metric is then computed on →
    # selection bias → an optimistically-low number shown to the customer. If
    # history is too short to hold a tail out, SKIP HPO entirely so the reported
    # metric stays unbiased (no tuning ⇒ nothing to over-fit against).
    hpo_cfg = config.get("hpo", {})
    if hpo_cfg.get("enabled", False):
        horizon  = int(config["model"]["horizon"])
        date_col = config["data"]["date_col"]
        n_splits = int(config.get("validation", {}).get("n_splits", 3))
        trial_horizon = int(hpo_cfg.get("trial_horizon", min(horizon, 5)))
        df_tune = _hpo_tune_slice(df, date_col, report_tail_days=n_splits * horizon)
        # HPO needs enough inner history for its OWN walk-forward folds.
        min_tune_dates = 2 * n_splits * trial_horizon
        n_tune_dates   = int(df_tune[date_col].nunique())
        if n_tune_dates >= min_tune_dates:
            _progress(5, 9, "Подбор гиперпараметров (Optuna)")
            from src.models.hpo import run_hpo
            best_params = run_hpo(df_tune, feature_cols, config,
                                  hpo_cfg.get("n_trials", 30),
                                  fold_feature_fn=_fold_statics)
            if best_params:
                config["model"].update(best_params)
                logger.info(
                    "  HPO best params (tuned on held-out inner slice, %d/%d dates): %s",
                    n_tune_dates, int(df[date_col].nunique()), best_params,
                )
        else:
            _progress(5, 9, "HPO пропущен (история коротка для honest-holdout)")
            logger.warning(
                "#180: history too short for a held-out HPO split (%d tune-dates "
                "< %d) — skipping HPO so the reported metric stays unbiased",
                n_tune_dates, min_tune_dates,
            )
    else:
        _progress(5, 9, "HPO пропущен")

    # ── 6. Walk-forward validation ────────────────────────────
    # The validator class MIRRORS the final production model class so
    # the metrics reported to the user are honest — the same prediction
    # mechanism that /app/forecasts will use is what gets graded here.
    # Walk-forward picks direct-multi-step mode automatically when the
    # model exposes the is_mimo / is_ensemble marker.
    #
    # A factory (lambda) gives each fold a fresh instance: stale state
    # from prior folds shouldn't leak into new ones.
    _progress(6, 9, "Walk-forward валидация")
    objective_cfg = str(config.get("model", {}).get("objective", "")).lower()
    # Three different forecaster classes can fit the validator slot;
    # walk_forward_validate accepts any of them via its callable
    # interface. Annotate with the broad Callable type so reassignment
    # across branches doesn't trip mypy.
    val_factory: Callable[[], Any]
    if objective_cfg == "hybrid":
        # #384 SHIP: band-роутинг ансамбль↔MIMO (платный default)
        from src.models.hybrid import HybridForecaster
        val_factory = lambda: HybridForecaster(config)
        val_label = "hybrid"
    elif objective_cfg == "ensemble":
        from src.models.ensemble import EnsembleForecaster
        val_factory = lambda: EnsembleForecaster(config)
        val_label = "ensemble"
    elif config["model"].get("type", "lgbm") == "mimo":
        val_factory = lambda: MIMOForecaster(config)
        val_label = "mimo"
    else:
        val_factory = lambda: SKUForecaster(config)
        val_label = "lgbm-baseline"
    logger.info(f"  Walk-forward validator class: {val_label}")
    wf_result = walk_forward_validate(
        df, val_factory, feature_cols, config, sample_weight_fn=sample_weight_fn,
        target_censor_fn=target_censor_fn, fold_feature_fn=_fold_statics,
    )
    agg = wf_result.aggregated
    logger.info(f"  WMAPE={agg.get('wmape_mean',0):.3f} MASE={agg.get('mase_mean',0):.3f}")
    storage.save_per_sku_metrics(wf_result.per_sku_metrics)

    # ── 6.5 Promotion gate (QW2-4 #227) ───────────────────────
    # Decided BEFORE the final fit: a blocked challenger skips the final
    # fit + quantiles + save entirely (the champion artifact keeps serving
    # untouched, and post-training forecasts are skipped via model_path=None).
    gate_verdict, gate_blocked = _promotion_gate(df, config, agg, client_id,
                                                 wf_combined=wf_result.combined)
    if gate_blocked:
        elapsed = time.time() - t0
        logger.warning("=== Training pipeline GATE-BLOCKED in %.1fs ===", elapsed)
        return {
            "client_id":       client_id,
            "model_path":      None,          # nothing saved — champion serves
            "model_type":      config["model"].get("type", "lgbm"),
            "storage_backend": storage.backend.__class__.__name__,
            "metrics":         agg,
            "gate":            gate_verdict.as_dict(),
            "mlflow_run_id":   None,
            "mlflow_logging_error": None,
            "n_skus":          df[sku_col].nunique(),
            "n_features":      len(feature_cols),
            "n_rows":          len(df),
            "elapsed_sec":     elapsed,
            "ge_stats":        ge_result.statistics,
        }

    # ── 7. Train final model ──────────────────────────────────
    _progress(7, 9, "Обучение финальной модели")
    model_type = config["model"].get("type", "lgbm")
    objective  = str(config.get("model", {}).get("objective", "")).lower()
    is_ensemble = objective == "ensemble"
    is_hybrid   = objective == "hybrid"

    # Same shape as val_factory above — three classes can land here,
    # so use a single broad annotation rather than an if-else union.
    final_model: Any
    if is_hybrid:
        # #384 SHIP: гибрид фитит калиброванный MIMO + ансамбль; бленд-веса
        # ансамблевой стороны — той же clean-holdout дисциплиной (#152),
        # прокси-методы гибрида ведут к ансамблю.
        from src.models.hybrid import HybridForecaster as _Hybrid
        final_model = _Hybrid(config)
        blend_lookback = int(
            config.get("model", {}).get("ensemble_lookback_days", 28)
        )
        clean_weights = False
        if bool(config.get("model", {}).get("blend_clean_holdout", True)):
            clean_weights = final_model.compute_blend_weights_clean(
                df_full=df, X=X, y=y,
                sku_col=sku_col,
                date_col=config["data"]["date_col"],
                target_col=target_col,
                groups=df[sku_col],
                sample_weight=sample_weights_full,
                target_censor=target_censor,
                lookback_days=blend_lookback,
            )
        final_model.fit(X, y, groups=df[sku_col], sample_weight=sample_weights_full,
                        target_censor=target_censor)
        _fit_quantiles_with_conformal(final_model, df, X, y, config, agg,
                                      target_censor=target_censor)
        if not clean_weights:
            final_model.compute_blend_weights(
                df_full=df,
                sku_col=sku_col,
                date_col=config["data"]["date_col"],
                target_col=target_col,
                lookback_days=blend_lookback,
            )
        agg["blend_weights_clean"] = float(clean_weights)
        logger.info("  Hybrid (band-routed ensemble+MIMO) + quantiles fitted")
    elif is_ensemble:
        # 3 MIMO children with different objectives, blended per SKU.
        # Adds ~3× training time and memory for a meaningful per-SKU
        # accuracy gain on mixed catalogs. EnsembleForecaster was
        # already imported in step 6 for the validator factory.
        final_model = EnsembleForecaster(config)
        blend_lookback = int(
            config.get("model", {}).get("ensemble_lookback_days", 28)
        )
        # #152 (A1): blend weights from a CLEAN holdout — temp children fit
        # on history-minus-window, weights estimated on the window they never
        # saw. Runs BEFORE the final fit so the throwaway children are freed
        # first (peak memory stays at one ensemble — the 1.5GiB worker
        # ceiling). Short history → False → legacy pseudo-holdout after fit.
        clean_weights = False
        if bool(config.get("model", {}).get("blend_clean_holdout", True)):
            clean_weights = final_model.compute_blend_weights_clean(
                df_full=df, X=X, y=y,
                sku_col=sku_col,
                date_col=config["data"]["date_col"],
                target_col=target_col,
                groups=df[sku_col],
                sample_weight=sample_weights_full,
                target_censor=target_censor,
                lookback_days=blend_lookback,
            )
        final_model.fit(X, y, groups=df[sku_col], sample_weight=sample_weights_full,
                        target_censor=target_censor)
        _fit_quantiles_with_conformal(final_model, df, X, y, config, agg,
                                      target_censor=target_censor)
        if not clean_weights:
            # Legacy pseudo-holdout (window seen by the children) — only the
            # short-history / disabled fallback since #152.
            final_model.compute_blend_weights(
                df_full=df,
                sku_col=sku_col,
                date_col=config["data"]["date_col"],
                target_col=target_col,
                lookback_days=blend_lookback,
            )
        agg["blend_weights_clean"] = float(clean_weights)
        logger.info("  Ensemble (Tweedie+MAE+MSE) + quantile models fitted")
    elif model_type == "mimo":
        final_model = MIMOForecaster(config)
        final_model.fit(X, y, groups=df[sku_col], sample_weight=sample_weights_full,
                        target_censor=target_censor)
        _fit_quantiles_with_conformal(final_model, df, X, y, config, agg,
                                      target_censor=target_censor)
        logger.info("  MIMO model + quantile models fitted")
    else:
        final_model = SKUForecaster(config)
        final_model.fit(X, y, sample_weight=sample_weights_full,
                        target_censor=target_censor)

    # #418: persist NaN-lag semantics with the model — serve keys its
    # fill-vs-keep-NaN parity decision on this attribute (see
    # inference_utils.direct_forecast), so legacy models stay untouched.
    final_model.nan_tolerant_lags = bool(
        config["features"].get("per_sku_lags", False))

    # (#231 KILL 2026-07-07: cold-start cluster fit удалён — результат
    # никогда не сохранялся/не грузился (CPU-waste), serve-дыры нет
    # (own-history фичи + ensemble default_weights + SeasonalNaive), окно
    # применимости пустое: <28д-SKU отбивается 30-row-гейтом /predict.
    # Анализ и решение: issue #231.)

    # SHAP explainer is constructed here only for forward-compat with
    # the /explain endpoint on the roadmap. Drop it once that endpoint
    # actually uses it; until then the import smoke-tests the wiring.
    if is_hybrid:
        lgbm_inner = (final_model.mimo.models_[0]
                      if final_model.mimo.models_ else None)
    elif is_ensemble:
        primary = final_model.models_[final_model.primary_objective]
        lgbm_inner = primary.models_[0] if primary.models_ else None
    elif model_type == "lgbm":
        lgbm_inner = getattr(final_model, "model", None)
    else:
        lgbm_inner = final_model.models_[0] if final_model.models_ else None
    _ = SKUExplainer(lgbm_inner, feature_cols) if lgbm_inner else None    # noqa: F841

    # ── Save fallback ─────────────────────────────────────────
    fallback = SeasonalNaiveModel(seasonality=7)
    fallback.fit(y.values)
    storage.save_fallback_model(fallback)

    # ── Save primary model ────────────────────────────────────
    # #229: market-хвост вшивается в артефакт ДО сохранения (serve мержит
    # колонки из него — см. src/features/market.py). Также до MLflow-лога:
    # хелпер выше сохраняет во временный pkl тот же объект.
    if market_series is not None:
        from src.features.market import attach_market_to_model
        attach_market_to_model(final_model, market_series)
    # #307: static-карта вшивается тем же образом, ДО save/MLflow-лога.
    from src.features.static_features import attach_static_to_model
    attach_static_to_model(final_model, static_map)
    model_path = storage.save_model(final_model)
    logger.info(f"  Saved → {model_path}")
    # DS-1 #466: модель дефолтного датасета дублируется в легаси-слот
    # клиента — его читают /predict и все существующие клиентские потоки;
    # мульти-датасетный serve адресует datasets-неймспейс напрямую.
    if dataset_id and dataset_is_default:
        legacy_path = ClientStorage(client_id).save_model(final_model)
        logger.info(f"  Saved (default-dataset legacy slot) → {legacy_path}")

    # ── 8. Cold-start + SHAP done above; mark step 8 ──────────
    _progress(8, 9, "Cold-start модель + SHAP объяснения")

    # ── MLflow logging ────────────────────────────────────────
    _progress(9, 9, "Запись эксперимента в MLflow")
    run_id, mlflow_logging_error = _save_and_log_to_mlflow(
        config, agg, final_model, client_id
    )

    elapsed = time.time() - t0
    logger.info(f"=== Training pipeline DONE in {elapsed:.1f}s ===")

    return {
        "client_id":       client_id,
        "model_path":      model_path,
        "model_type":      model_type,
        "storage_backend": storage.backend.__class__.__name__,
        "metrics":         agg,
        "gate":            gate_verdict.as_dict() if gate_verdict else None,
        "mlflow_run_id":   run_id,
        # R11: best-effort MLflow telemetry — NULL when it logged fine,
        # else the error string. Persisted by task_queue to
        # sku_training_runs.mlflow_logging_error so a degraded run is
        # observable (queryable + surfaced in the training-runs API)
        # instead of silently swallowed (the R8-12 root cause).
        "mlflow_logging_error": mlflow_logging_error,
        "n_skus":          df[sku_col].nunique(),
        "n_features":      len(feature_cols),
        "n_rows":          len(df),
        "elapsed_sec":     elapsed,
        "ge_stats":        ge_result.statistics,
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--data",   required=True)
    parser.add_argument("--config", default="configs/config.yaml")
    parser.add_argument("--client", default="default")
    args = parser.parse_args()
    result = run_training_pipeline(args.data, args.config, args.client)
    import json
    logger.info("Pipeline result: " + json.dumps({k: str(v) for k,v in result.items()}, default=str))
