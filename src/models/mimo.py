"""
src/models/mimo.py

MIMO (Multi-Input Multi-Output) forecaster.
Trains H separate LightGBM models, each predicting step h directly
from the same feature set — no recursive error accumulation.

Also implements quantile regression (p10/p50/p90) for interval forecasts.

Usage:
    model = MIMOForecaster(config)
    model.fit(X, y_matrix)          # y_matrix: shape (N, H)
    preds = model.predict(X)        # shape (N, H)
    intervals = model.predict_quantiles(X)  # {p10, p50, p90}
"""
from __future__ import annotations

import logging
import pickle
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class MIMOForecaster:
    """
    Direct multi-step forecaster: one LightGBM model per horizon step.
    Eliminates recursive error accumulation.
    """

    # Marker so walk-forward / batch-forecast know to use the
    # direct multi-step prediction (one shot returns h=1..H) instead
    # of recursing 1-step-at-a-time. The whole point of MIMO is to
    # AVOID recursion's compounding errors — recursive use would
    # defeat the architecture.
    is_mimo = True

    def __init__(self, config: dict):
        self.config    = config
        self.horizon   = config["model"]["horizon"]
        self.models_   : list[lgb.LGBMRegressor] = []
        self.q_models_ : dict[str, list[lgb.LGBMRegressor]] = {}
        self.feature_cols: list[str] = []
        # #229: market-хвост — None у старых моделей/до attach
        self.market_tail = None
        self.static_map = None

    def _base_params(self, extra: dict | None = None) -> dict:
        m = self.config["model"]
        from src.models.forecaster import lgb_objective_params
        p = dict(
            n_estimators    = m.get("n_estimators",    500),
            learning_rate   = m.get("learning_rate",   0.05),
            num_leaves      = m.get("num_leaves",      64),
            min_child_samples = m.get("min_child_samples", 20),
            feature_fraction = m.get("feature_fraction", 0.8),
            bagging_fraction = m.get("bagging_fraction", 0.8),
            bagging_freq    = m.get("bagging_freq",    5),
            # L-A10 (#186): seed the RNG so every direct head (and quantile
            # sub-model — `extra` overrides objective/alpha only, not this) is
            # reproducible. Default 42; config `model.random_state` overrides.
            # n_jobs config-driven (default -1; tests pin 1 for reproducibility).
            random_state    = m.get("random_state",    42),
            n_jobs          = m.get("n_jobs",          -1),
            verbose         = -1,
            **lgb_objective_params(m),
        )
        # `extra` overrides — quantile branch passes
        # {"objective": "quantile", "alpha": q} to wipe the default
        # objective and run quantile regression instead. Keep that
        # contract: extra wins.
        if extra:
            p.update(extra)
        return p

    def fit(
        self,
        X: pd.DataFrame,
        y: pd.Series,
        groups: pd.Series | None = None,
        sample_weight: np.ndarray | None = None,
        target_censor: pd.Series | None = None,
    ) -> "MIMOForecaster":
        """
        Fit H direct models. For step h, target = sales shifted h steps back.
        X must already be the feature matrix (no future leakage).

        groups: optional per-row SKU labels (same index as X/y). When provided,
        target shifts respect SKU boundaries — `y.groupby(groups).shift(-h)`
        keeps "h days ahead" within the same SKU and turns boundary rows into
        NaN that get dropped. Without it the global `y.shift(-h)` smears the
        end of one SKU's series into the start of the next, polluting H≈14
        rows per SKU boundary with cross-SKU targets.

        sample_weight: optional per-row weights aligned POSITIONALLY to X (#183,
        anomaly down-weighting). Each horizon drops the NaN-target rows, so the
        weight vector is sliced by the SAME boolean mask before the per-head fit.
        None → unweighted.

        target_censor (#228): optional 0/1 series aligned like `y` marking
        days whose OBSERVED sales are censored (out-of-stock: zero sales ≠
        zero demand). Rows whose TARGET day (t+h) is censored are DROPPED
        from head h's fit — keyed to the target day, not the input row, so
        one OOS day removes exactly the examples it corrupts.
        """
        self.feature_cols = list(X.columns)
        self.models_ = []
        w = None if sample_weight is None else np.asarray(sample_weight)

        params = self._base_params()
        if params["objective"] in {"tweedie", "poisson"} and (y < 0).any():
            logger.warning(
                "MIMO: negative targets detected with objective=%s — clipping",
                params["objective"],
            )
            y = y.clip(lower=0)

        # Build targets for each horizon step using historical shifts
        for h in range(1, self.horizon + 1):
            if groups is None:
                y_h = y.shift(-h)
            else:
                y_h = y.groupby(groups).shift(-h)
            mask = y_h.notna()
            if target_censor is not None:
                c_h = (target_censor.shift(-h) if groups is None
                       else target_censor.groupby(groups).shift(-h))
                mask &= ~(c_h.fillna(0) > 0)          # drop censored-target rows (#228)
            X_h  = X[mask]
            y_h  = y_h[mask]

            fit_kwargs: dict = {"callbacks": [lgb.log_evaluation(period=-1)]}
            if w is not None:
                # mask is index-aligned to X; .to_numpy() gives the positional
                # boolean to slice the positional weight vector to the kept rows.
                fit_kwargs["sample_weight"] = w[mask.to_numpy()]
            model = lgb.LGBMRegressor(**params)
            model.fit(X_h, y_h, **fit_kwargs)
            self.models_.append(model)

        logger.info(
            f"MIMO: fitted {self.horizon} direct models on {len(X)} rows "
            f"(objective={params['objective']}, per_sku_shift={groups is not None})"
        )

        # QH-5 #422 band- / QH-9 #442 step-калибровка (общий fold-clean
        # probe — один внутренний фит на обе). Только при groups (нужны
        # границы SKU); best-effort — сбой не роняет фит.
        mc = self.config.get("model") or {}
        bc_cfg = mc.get("band_calibration") or {}
        sc_cfg = mc.get("step_calibration") or {}
        if ((bc_cfg.get("enabled") or sc_cfg.get("enabled"))
                and groups is not None):
            self._fit_probe_calibrations(X, y, groups, sample_weight,
                                         target_censor)

        return self

    def fit_quantiles(
        self,
        X: pd.DataFrame,
        y: pd.Series,
        quantiles: list[float] | None = None,
        groups: pd.Series | None = None,
        target_censor: pd.Series | None = None,
    ) -> "MIMOForecaster":
        """Fit quantile models for interval forecasts (p10/p50/p90).

        target_censor (#228): censored-target rows are dropped per head —
        OOS zeros corrupt the demand distribution the quantiles learn just
        as much as the point forecast."""
        if quantiles is None:
            quantiles = [0.1, 0.5, 0.9]
        self.feature_cols = list(X.columns)
        self.q_models_ = {}

        for q in quantiles:
            q_models = []
            key = f"p{int(q*100)}"
            for h in range(1, self.horizon + 1):
                if groups is None:
                    y_h = y.shift(-h)
                else:
                    y_h = y.groupby(groups).shift(-h)
                mask = y_h.notna()
                if target_censor is not None:
                    c_h = (target_censor.shift(-h) if groups is None
                           else target_censor.groupby(groups).shift(-h))
                    mask &= ~(c_h.fillna(0) > 0)      # #228
                model = lgb.LGBMRegressor(
                    **self._base_params({"objective": "quantile", "alpha": q})
                )
                model.fit(X[mask], y_h[mask], callbacks=[lgb.log_evaluation(period=-1)])
                q_models.append(model)
            self.q_models_[key] = q_models
            logger.info(f"MIMO: fitted quantile q={q} ({self.horizon} models)")
        return self

    # ── QH-5 #422: per-band мультипликативная калибровка точечного прогноза ──
    # Ship-решение 2026-07-12 (журнал замеров): MASE −2.64% / mean −2.97% /
    # slow-band −5% на 1c-live при нейтральном WMAPE_g. Скоуп СТРОГО как
    # измерено: только predict()/predict_next(); квантили и conformal НЕ
    # калибруются (coverage откалиброван на сырых — их масштабирование =
    # отдельный замер по cost-метрике). Ensemble-члены — калибровка
    # ЗАПРЕЩЕНА до отдельного замера (R14: платная метрика).

    def _fit_probe_calibrations(self, X, y, groups, sample_weight,
                                target_censor) -> None:
        """Fold-clean оценка факторов (алгоритм = плечо band_cal стенда):
        probe-фит на «голове» (без последних H строк каждого SKU), band'ы
        по тершилям среднего спроса головы, фактор = clip(Σфакт/Σпрогноз)
        по band'у на внутреннем хвосте. QH-9 #442: тот же probe даёт и
        per-step факторы clip(Σфакт_h/Σпрогноз_h) — считаются по
        band-скорректированному прогнозу (step правит остаток, уровень
        не считается дважды). Best-effort: любой сбой — модель остаётся
        без факторов (=1.0), обучение не падает."""
        try:
            import copy as _copy
            mc = self.config.get("model") or {}
            bc = mc.get("band_calibration") or {}
            sc = mc.get("step_calibration") or {}
            do_band = bool(bc.get("enabled"))
            do_step = bool(sc.get("enabled"))
            lo, hi = bc.get("clip", [0.8, 1.25])
            g = pd.Series(groups).reset_index(drop=True)
            Xr = X.reset_index(drop=True)
            yr = pd.Series(y).reset_index(drop=True)
            sw = (pd.Series(sample_weight).reset_index(drop=True)
                  if sample_weight is not None else None)
            tc = (pd.Series(target_censor).reset_index(drop=True)
                  if target_censor is not None else None)

            fwd = g.groupby(g).cumcount()
            cnt = g.map(g.value_counts())
            tail = (cnt - fwd) <= self.horizon
            head = ~tail

            cfg = _copy.deepcopy(self.config)
            cfg["model"]["band_calibration"] = {"enabled": False}
            cfg["model"]["step_calibration"] = {"enabled": False}
            probe = MIMOForecaster(cfg)
            probe.fit(Xr[head], yr[head], groups=g[head],
                      sample_weight=None if sw is None else sw[head].to_numpy(),
                      target_censor=None if tc is None else tc[head])

            mean_by_sku = yr[head].groupby(g[head]).mean()
            t1, t2 = np.quantile(mean_by_sku.to_numpy(), [1 / 3, 2 / 3])
            band_by_sku = {str(s): int(np.digitize(v, [t1, t2]))
                           for s, v in mean_by_sku.items()}

            head_counts = g[head].value_counts()
            eligible = set(head_counts[head_counts >= 2 * self.horizon].index)
            head_groups = g[head].groupby(g[head]).groups
            tail_groups = g[tail].groupby(g[tail]).groups
            sums_p = {0: 0.0, 1: 0.0, 2: 0.0}
            sums_a = {0: 0.0, 1: 0.0, 2: 0.0}
            probe_rows = []       # (band, preds[:k], actual[:k]) для step
            for sku in eligible:
                try:
                    raw = probe.predict(Xr.loc[[head_groups[sku][-1]]])
                except Exception:    # noqa: BLE001 — SKU пропускается
                    continue
                preds = np.clip(np.asarray(raw, dtype=float)[0], 0, None)
                actual = yr.loc[tail_groups.get(sku, [])].to_numpy()[: len(preds)]
                k = min(len(preds), len(actual))
                if k == 0:
                    continue
                b = band_by_sku.get(str(sku), 1)
                sums_p[b] += float(preds[:k].sum())
                sums_a[b] += float(actual[:k].sum())
                if do_step:
                    probe_rows.append((b, preds[:k], actual[:k]))
            factors = {b: float(np.clip(sums_a[b] / sums_p[b], lo, hi))
                       for b in sums_p if sums_p[b] > 0}
            if do_band:
                self.band_cal_ = {"factors": factors,
                                  "band_of_sku": band_by_sku,
                                  "clip": [float(lo), float(hi)]}
                logger.info(f"Band calibration fitted: factors={factors}")
            if do_step:
                slo, shi = sc.get("clip", [0.8, 1.25])
                band_f = factors if do_band else {}
                sp = np.zeros(self.horizon)
                sa = np.zeros(self.horizon)
                for b, p, a in probe_rows:
                    f = band_f.get(b, 1.0)
                    sp[: len(p)] += p * f
                    sa[: len(a)] += a
                step_factors = [
                    float(np.clip(sa[h] / sp[h], slo, shi)) if sp[h] > 0
                    else 1.0
                    for h in range(self.horizon)
                ]
                self.step_cal_ = {"factors": step_factors,
                                  "clip": [float(slo), float(shi)]}
                logger.info(f"Step calibration fitted: factors={step_factors}")
        except Exception as e:    # noqa: BLE001 — залогировано, не проглочено
            logger.warning(f"Probe calibration failed (model stays uncalibrated): {e}")

    def _band_factor_vec(self, X: pd.DataFrame) -> np.ndarray:
        """Per-row фактор band'а SKU; 1.0 для legacy-моделей (нет атрибута),
        неизвестных SKU и фреймов без sku-колонки — serve-parity с probe."""
        bc = getattr(self, "band_cal_", None)
        if not bc or not bc.get("factors"):
            return np.ones(len(X))
        sku_col = self.config.get("data", {}).get("sku_col", "sku")
        if sku_col not in X.columns:
            return np.ones(len(X))
        bands = X[sku_col].astype(str).map(bc["band_of_sku"])
        return bands.map(bc["factors"]).fillna(1.0).to_numpy(dtype=float)

    def _step_factor_vec(self) -> np.ndarray:
        """QH-9 #442: per-step факторы горизонта; ones для legacy-моделей
        (нет атрибута) и выключенной калибровки — serve-parity с probe."""
        sc = getattr(self, "step_cal_", None)
        if not sc or not sc.get("factors"):
            return np.ones(self.horizon)
        return np.asarray(sc["factors"], dtype=float)

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        """
        Predict all H steps for each row in X.
        Returns array shape (len(X), H).
        """
        if not self.models_:
            raise RuntimeError("Call fit() first")
        preds = np.stack(
            [np.clip(m.predict(X[self.feature_cols]), 0, None) for m in self.models_],
            axis=1,
        )
        # QH-5 #422: точечный прогноз × фактор band'а (квантили не трогаем);
        # QH-9 #442: × фактор шага горизонта (default OFF до замера)
        preds = preds * self._band_factor_vec(X)[:, None]
        preds = preds * self._step_factor_vec()[None, :]
        return preds  # shape (N, H)

    def predict_next(self, X_last: pd.DataFrame) -> np.ndarray:
        """
        Predict H-step forecast for a single last row.
        Returns 1D array of length H.
        """
        return self.predict(X_last)[0]

    def calibrate_conformal(
        self,
        X: pd.DataFrame,
        y: pd.Series,
        dates: pd.Series,
        cal_start: pd.Timestamp,
        groups: pd.Series | None = None,
        alpha: float = 0.2,
        lo_key: str = "p10",
        hi_key: str = "p90",
        target_censor: pd.Series | None = None,
    ) -> dict:
        """CQR calibration (#151) — see src/models/conformal.py for the method.

        PRECONDITION: fit_quantiles() was trained on the PROPER subset only
        (rows whose shifted targets all predate `cal_start`) — the temporal
        split lives in the caller (train.py); this method scores the heads on
        the held-out calibration targets and stores one correction per head
        in `self.conformal_`, which predict_quantiles() then applies.

        Calibration rows for head h: rows whose TARGET date (t+h) falls on or
        after `cal_start` — exactly the targets the quantile models never saw.
        Returns a summary dict (pooled + per-band coverage pre/post, pinball
        pre/post) for logging/metrics; {} when nothing could be calibrated.
        """
        from src.models.conformal import (
            conformity_scores, empirical_coverage, per_head_corrections,
            pinball_loss,
        )
        if not self.q_models_ or lo_key not in self.q_models_ or hi_key not in self.q_models_:
            raise RuntimeError("Call fit_quantiles() first (lo/hi heads required)")

        dates = pd.to_datetime(dates)
        scores_by_head: list[np.ndarray] = []
        # Flat per-row collectors for the summary (pooled + stratified).
        col_y, col_lo, col_hi, col_head, col_sku = [], [], [], [], []

        for h in range(1, self.horizon + 1):
            if groups is None:
                y_h = y.shift(-h)
                d_h = dates.shift(-h)
            else:
                y_h = y.groupby(groups).shift(-h)
                d_h = dates.groupby(groups).shift(-h)
            mask = y_h.notna() & (d_h >= cal_start)
            if target_censor is not None:
                c_h = (target_censor.shift(-h) if groups is None
                       else target_censor.groupby(groups).shift(-h))
                # #228: censored actuals are corrupted ground truth
                mask &= ~(c_h.fillna(0) > 0)
            if not bool(mask.any()):
                scores_by_head.append(np.empty(0))
                continue
            X_m  = X[mask][self.feature_cols]
            y_m  = y_h[mask].to_numpy(dtype=float)
            lo_m = self.q_models_[lo_key][h - 1].predict(X_m)
            hi_m = self.q_models_[hi_key][h - 1].predict(X_m)
            scores_by_head.append(conformity_scores(y_m, lo_m, hi_m))
            col_y.append(y_m)
            col_lo.append(lo_m)
            col_hi.append(hi_m)
            col_head.append(np.full(len(y_m), h))
            if groups is not None:
                col_sku.append(groups[mask].to_numpy())

        if not col_y:
            logger.warning("conformal: empty calibration window — skipping (raw quantiles served)")
            return {}

        corrections, info = per_head_corrections(scores_by_head, alpha)
        self.conformal_ = {
            "lo_key": lo_key, "hi_key": hi_key, "alpha": alpha,
            "corrections": corrections, "info": info,
        }

        yv  = np.concatenate(col_y)
        lov = np.concatenate(col_lo)
        hiv = np.concatenate(col_hi)
        hv  = np.concatenate(col_head).astype(int)
        adj = corrections[hv - 1]

        # ── #219 Mondrian: (horizon-block × volume band) corrections ─────────
        # Bands = terciles of per-SKU mean demand over the passed (train)
        # rows; the SKU→band map is persisted with the model so serve can
        # look a row's band up from its sku column (unseen/absent → the
        # band-agnostic fallback row). Enabled by config; needs groups and
        # ≥3 distinct SKU volumes to form terciles — else per-head (#151)
        # behaviour stands.
        mondrian_on = bool(
            self.config.get("model", {}).get("conformal_mondrian", True)
        )
        if mondrian_on and groups is not None and col_sku:
            from src.models.conformal import mondrian_correction_table
            sku_mean = y.groupby(groups).mean()
            uniq = np.unique(sku_mean.to_numpy(dtype=float))
            if len(uniq) >= 3:
                qs = np.quantile(uniq, [1 / 3, 2 / 3])
                band_of_sku = {
                    str(s): int(np.digitize(v, qs)) for s, v in sku_mean.items()
                }
                sk = np.concatenate(col_sku).astype(str)
                bands_rows = np.array([band_of_sku[s] for s in sk])
                scores_flat = np.concatenate(scores_by_head)
                table, minfo = mondrian_correction_table(
                    scores=scores_flat, heads=hv, bands=bands_rows,
                    n_heads=self.horizon, n_bands=3, alpha=alpha,
                )
                self.conformal_.update({
                    "mode": "mondrian",
                    "table": table,
                    "band_of_sku": band_of_sku,
                    "mondrian_info": minfo,
                })
                # summary reflects what will be SERVED: per-row (band, head)
                adj = table[bands_rows, hv - 1]
                logger.info(
                    "conformal (#219): mondrian table %s, %d strata fell back (%s)",
                    table.shape, len(minfo["fallbacks"]), minfo["fallbacks"][:6],
                )
            else:
                logger.warning(
                    "#219: <3 distinct SKU volumes — mondrian skipped, "
                    "per-head corrections served"
                )
        summary: dict = {
            "alpha":            alpha,
            "n_cal":            int(len(yv)),
            "coverage_pre":     empirical_coverage(yv, lov, hiv),
            "coverage_post":    empirical_coverage(yv, lov - adj, hiv + adj),
            "correction_mean":  float(np.mean(corrections)),
            "fallback_heads":   info["fallback_heads"],
            "pinball_lo_pre":   pinball_loss(yv, lov, 0.5 * alpha),
            "pinball_lo_post":  pinball_loss(yv, np.clip(lov - adj, 0, None), 0.5 * alpha),
            "pinball_hi_pre":   pinball_loss(yv, hiv, 1.0 - 0.5 * alpha),
            "pinball_hi_post":  pinball_loss(yv, hiv + adj, 1.0 - 0.5 * alpha),
        }
        # Stratified coverage: horizon step × SKU volume band (terciles by
        # mean calibration target). A pooled 90% can hide under-coverage on
        # slow movers — exactly where stockouts hurt (#151).
        if col_sku:
            sk_arr = np.concatenate(col_sku)
            sku_mean_arr = pd.Series(yv).groupby(pd.Series(sk_arr)).transform("mean").to_numpy()
            qs = (np.quantile(np.unique(sku_mean_arr), [1 / 3, 2 / 3])
                  if len(np.unique(sku_mean_arr)) >= 3 else None)
            if qs is not None:
                band = np.digitize(sku_mean_arr, qs)      # 0=slow 1=mid 2=fast
                for b, name in enumerate(("slow", "mid", "fast")):
                    m = band == b
                    if m.any():
                        summary[f"coverage_post_{name}"] = empirical_coverage(
                            yv[m], (lov - adj)[m], (hiv + adj)[m]
                        )
        per_head_cov = {
            int(h): empirical_coverage(yv[hv == h], (lov - adj)[hv == h], (hiv + adj)[hv == h])
            for h in np.unique(hv)
        }
        summary["coverage_post_by_head"] = per_head_cov
        logger.info(
            "conformal (#151): n_cal=%d coverage %.3f → %.3f (nominal %.2f), "
            "mean correction %+.3f, fallback heads=%s",
            summary["n_cal"], summary["coverage_pre"], summary["coverage_post"],
            1 - alpha, summary["correction_mean"], info["fallback_heads"],
        )
        return summary

    def predict_quantiles(self, X: pd.DataFrame) -> dict[str, np.ndarray]:
        """
        Return {p10, p50, p90} each shape (N, H).

        #151: when `calibrate_conformal` ran, the stored per-head CQR
        corrections widen (or tighten) the lo/hi pair before the ≥0 clip —
        models pickled before #151 lack the attribute and serve raw quantiles
        exactly as before.
        """
        if not self.q_models_:
            raise RuntimeError("Call fit_quantiles() first")
        out = {
            key: np.stack([m.predict(X[self.feature_cols]) for m in models], axis=1)
            for key, models in self.q_models_.items()
        }
        conf = getattr(self, "conformal_", None)
        if conf:
            if conf.get("mode") == "mondrian":
                # #219: per-row (band × head) lookup. The table's LAST row is
                # the band-agnostic fallback — used when the frame carries no
                # sku column or the SKU wasn't in the calibration map.
                table = np.asarray(conf["table"], dtype=float)
                band_map = conf["band_of_sku"]
                fallback_row = table.shape[0] - 1
                sku_col = self.config.get("data", {}).get("sku_col", "sku")
                if sku_col in X.columns:
                    idx = (
                        X[sku_col].astype(str).map(band_map)
                        .fillna(fallback_row).astype(int).to_numpy()
                    )
                else:
                    idx = np.full(len(X), fallback_row)
                c = table[idx]                                 # (N, H)
            else:
                c = np.asarray(conf["corrections"], dtype=float)  # (H,) broadcasts
            if conf["lo_key"] in out:
                out[conf["lo_key"]] = out[conf["lo_key"]] - c
            if conf["hi_key"] in out:
                out[conf["hi_key"]] = out[conf["hi_key"]] + c
        return {k: np.clip(v, 0, None) for k, v in out.items()}

    def feature_importance(self) -> pd.DataFrame:
        """Mean LightGBM gain importance across the H direct heads.

        A feature that matters for some horizons but not others still
        surfaces (averaged), which is the honest view for a multi-step
        model. Returns [feature, importance] sorted desc; empty if unfit.
        """
        if not self.models_:
            return pd.DataFrame(columns=["feature", "importance"])
        imp = np.mean([m.feature_importances_ for m in self.models_], axis=0)
        return (
            pd.DataFrame({"feature": self.feature_cols, "importance": imp})
            .sort_values("importance", ascending=False)
            .reset_index(drop=True)
        )

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump({
                "models":       self.models_,
                "q_models":     self.q_models_,
                "feature_cols": self.feature_cols,
                "horizon":      self.horizon,
                # #229: market-хвост — serve мержит колонки из него
                "market_tail":  getattr(self, "market_tail", None),
                # #307: static-карта (velocity_band, price_tier) — serve мержит по SKU
                "static_map":   getattr(self, "static_map", None),
            }, f)
        logger.info(f"MIMO model saved → {path}")

    @classmethod
    def load(cls, path: str | Path, config: dict) -> "MIMOForecaster":
        # AUDIT R2-16: raw pickle.load — NOT for production paths.
        # See src/models/SECURITY.md. Prod loads MUST go through
        # src.pipeline.inference_utils.load_model_any_format.
        with open(path, "rb") as f:
            state = pickle.load(f)  # nosec — see SECURITY.md
        obj = cls(config)
        obj.models_       = state["models"]
        obj.q_models_     = state.get("q_models", {})
        obj.feature_cols  = state["feature_cols"]
        obj.horizon       = state["horizon"]
        # #229: .get — старые pickle без хвоста легальны (их feature_cols
        # market-колонок не требуют)
        _tail = state.get("market_tail")
        if _tail is not None:
            obj.market_tail = _tail
        _smap = state.get("static_map")
        if _smap is not None:
            obj.static_map = _smap
        return obj
