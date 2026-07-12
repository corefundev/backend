"""HybridForecaster — band-роутинг ансамбль↔MIMO (#384, SHIP 2026-07-13).

Замер 1c-live (журнал): полный ансамбль давал MASE −2.07% ценой
WMAPE_g +3.65%; гибрид (slow/mid → ансамбль, fast → калиброванный
MIMO/tweedie) снял компромисс: WMAPE_g −0.14% (fast-масса бит-в-бит не
тронута) при MASE −3.04% / per-SKU mean −2.85% — доминирует полный
ансамбль по обеим метрикам.

Маршрутизация — по статик-фиче velocity_band ИЗ СТРОКИ ПРОГНОЗА
(train-computed карта пиннится в артефакте статик-фич → serve-parity
по построению, как в измеренном плече route_slowmid_ens). Строка без
band'а / NaN → MIMO (fallback: fast-путь дешевле и headline-безопасен).

Квантили и conformal — ПО-BAND'ОВО: каждая сторона несёт свою
собственную квантильную машинерию (внутренняя согласованность: точка и
интервал одного SKU приходят из одной модели). Точечный роутинг измерен
стендом; квантильный роутинг структурно следует за ним (отдельный
cost-замер — в бэклоге очереди).

Цена: 5 LightGBM-фитов вместо 3 у ансамбля (~+60-70% времени тренировки
платного тарифа) — владельцем принято (VPS будет усилен к продажам).
"""
from __future__ import annotations

import copy
import logging

import numpy as np
import pandas as pd

from src.models.ensemble import EnsembleForecaster
from src.models.mimo import MIMOForecaster

logger = logging.getLogger(__name__)


class HybridForecaster:
    is_mimo = True        # walk_forward: direct multi-step режим
    is_hybrid = True

    def __init__(self, config: dict):
        self.config = config
        hcfg = (config.get("model", {}) or {}).get("hybrid", {}) or {}
        self.route_bands = {int(b) for b in hcfg.get("route_bands", (0, 1))}
        self.band_col = str(hcfg.get("band_col", "velocity_band"))

        mimo_cfg = copy.deepcopy(config)
        mimo_cfg["model"]["type"] = "mimo"
        mimo_cfg["model"]["objective"] = str(hcfg.get("fast_objective", "tweedie"))
        self.mimo = MIMOForecaster(mimo_cfg)

        ens_cfg = copy.deepcopy(config)
        ens_cfg["model"]["objective"] = "ensemble"
        self.ensemble = EnsembleForecaster(ens_cfg)

        self.feature_cols: list[str] = []

    # ── обучение ─────────────────────────────────────────────────────────
    def fit(self, X, y, groups=None, sample_weight=None, target_censor=None):
        self.mimo.fit(X, y, groups=groups, sample_weight=sample_weight,
                      target_censor=target_censor)
        self.ensemble.fit(X, y, groups=groups, sample_weight=sample_weight,
                          target_censor=target_censor)
        self.feature_cols = list(X.columns)
        # serve-parity NaN-семантики x_last (#418/#422): решает MIMO-сторона —
        # fast-масса ходит через неё; ансамбль-дети всегда legacy-fill
        self.nan_tolerant_lags = bool(getattr(self.mimo, "nan_tolerant_lags", False))
        return self

    def fit_quantiles(self, X, y, quantiles=None, groups=None,
                      target_censor=None):
        self.mimo.fit_quantiles(X, y, quantiles, groups=groups,
                                target_censor=target_censor)
        self.ensemble.fit_quantiles(X, y, quantiles, groups=groups,
                                    target_censor=target_censor)
        return self

    def calibrate_conformal(self, *args, **kwargs) -> dict:
        """CQR обеих сторон; сводка — от MIMO (fast = 83% массы), ансамблевая
        вложена отдельным ключом (обе пишутся в лог тренировки)."""
        res_m = self.mimo.calibrate_conformal(*args, **kwargs) or {}
        res_e = self.ensemble.calibrate_conformal(*args, **kwargs) or {}
        out = dict(res_m)
        out["ensemble_side"] = res_e
        return out

    # ── бленд-веса ансамблевой стороны (проксируются train.py) ───────────
    def compute_blend_weights(self, **kw):
        return self.ensemble.compute_blend_weights(**kw)

    def compute_blend_weights_clean(self, **kw):
        return self.ensemble.compute_blend_weights_clean(**kw)

    # ── маршрутизация ────────────────────────────────────────────────────
    def _ens_mask(self, X: pd.DataFrame) -> np.ndarray:
        if self.band_col not in getattr(X, "columns", []):
            return np.zeros(len(X), dtype=bool)
        bands = pd.to_numeric(X[self.band_col], errors="coerce")
        return bands.isin(list(self.route_bands)).to_numpy()

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        mask = self._ens_mask(X)
        if not mask.any():
            return np.asarray(self.mimo.predict(X), dtype=float)
        if mask.all():
            return np.asarray(self.ensemble.predict(X), dtype=float)
        out = np.asarray(self.mimo.predict(X), dtype=float)
        out[mask] = np.asarray(self.ensemble.predict(X[mask]), dtype=float)
        return out

    def predict_next(self, X_last: pd.DataFrame) -> np.ndarray:
        return self.predict(X_last)[0]

    def predict_quantiles(self, X: pd.DataFrame) -> dict[str, np.ndarray]:
        mask = self._ens_mask(X)
        if not mask.any():
            return self.mimo.predict_quantiles(X)
        if mask.all():
            return self.ensemble.predict_quantiles(X)
        q_m = self.mimo.predict_quantiles(X)
        q_e = self.ensemble.predict_quantiles(X[mask])
        out = {}
        for key, arr in q_m.items():
            merged = np.asarray(arr, dtype=float).copy()
            if key in q_e:
                merged[mask] = np.asarray(q_e[key], dtype=float)
            out[key] = merged
        return out

    def feature_importance(self) -> pd.DataFrame:
        """Fast-масса (83%) ходит через MIMO — его импортансы и есть честный
        headline-агрегат гибрида."""
        return self.mimo.feature_importance()
