"""
src/validation/backtest_gate.py — automated forecast-quality gate (#150 / A4).

Turns the manual `backtest_runner` into a FAIL-CLOSED champion-vs-challenger
check with a naive-baseline floor:

  • run a temporal-holdout backtest for the CHAMPION config and the CHALLENGER
    config on the SAME data + cutoff (via backtest_runner.run_baseline);
  • score a seasonal-naive(7) BASELINE on the same holdout (the model the
    product itself falls back to — SeasonalNaiveModel(7));
  • the challenger PASSES only if it (a) does not regress WMAPE/MASE vs the
    champion beyond `tolerance`, AND (b) beats the seasonal-naive baseline.
    Anything undecidable (NaN metric) FAILS closed.

The scoring/decision logic here is pure and unit-tested without libomp; the
heavy `run_gate` (which trains LightGBM) runs on CI/staging. Example:

    docker exec docker-worker-1 python -m src.validation.backtest_gate \
        --data data/test/sample_free.csv --tier free --holdout-days 7 \
        --challenger '{"anomaly_detection": {"enabled": true}}' \
        --champion   '{"anomaly_detection": {"enabled": false}}'
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from src.models.fallback import SeasonalNaiveModel
from src.validation.backtest import BacktestResult, temporal_split
from src.validation.metrics import aggregate_metrics


# ── Seasonal-naive baseline (no training / libomp needed) ──────────────────

def score_seasonal_naive(
    df: pd.DataFrame,
    config: dict,
    holdout_days: int,
    seasonality: int = 7,
    label: str = "seasonal_naive_7",
) -> BacktestResult:
    """Score a per-SKU SeasonalNaiveModel(seasonality) on the holdout tail.

    For each SKU: fit on the pre-cutoff series, forecast `holdout_days` ahead,
    align to the holdout dates, and pool the metrics the same way `run_backtest`
    does — so its numbers are directly comparable to a model backtest.
    """
    sku_col    = config["data"]["sku_col"]
    date_col   = config["data"]["date_col"]
    target_col = config["data"]["target_col"]

    df = df.copy()
    df[date_col] = pd.to_datetime(df[date_col])
    train, holdout, cutoff = temporal_split(df, holdout_days, date_col)

    rows: list[dict] = []
    train_tail: dict = {}
    for sku, g in train.sort_values(date_col).groupby(sku_col):
        y = g[target_col].to_numpy(dtype=float)
        train_tail[sku] = y
        preds = SeasonalNaiveModel(seasonality=seasonality).fit(y).predict(holdout_days)
        for h in range(holdout_days):
            d = (cutoff + pd.Timedelta(days=h)).normalize()
            rows.append({sku_col: sku, date_col: d, "predicted": float(preds[h])})

    pred_df = pd.DataFrame(rows)
    a = holdout[[sku_col, date_col, target_col]].copy()
    a[date_col] = pd.to_datetime(a[date_col]).dt.normalize()
    scored = a.merge(pred_df, on=[sku_col, date_col], how="inner")
    if scored.empty:
        raise ValueError("seasonal-naive baseline: no (sku,date) overlap with holdout")
    scored = scored.rename(columns={target_col: "actual"})
    scored["sku"] = scored[sku_col]
    scored["train_values"] = scored[sku_col].map(train_tail)

    per_sku = _per_sku_wmape(scored, sku_col)
    agg = aggregate_metrics(per_sku, raw_df=scored)
    return BacktestResult(
        holdout_days=holdout_days,
        cutoff=str(cutoff.date()),
        n_skus=int(scored[sku_col].nunique()),
        n_scored_rows=int(len(scored)),
        wmape_global=float(agg.get("wmape_global", float("nan"))),
        mase_global=float(agg.get("mase_global", float("nan"))),
        smape_global=float(agg.get("smape_global", float("nan"))),
        label=label,
    )


def _per_sku_wmape(scored: pd.DataFrame, sku_col: str) -> pd.DataFrame:
    rows = []
    for sku, g in scored.groupby(sku_col):
        yt = g["actual"].to_numpy(dtype=float)
        yp = g["predicted"].to_numpy(dtype=float)
        denom = float(np.abs(yt).sum())
        wmape = float(np.abs(yt - yp).sum() / denom) if denom > 1e-10 else float("nan")
        rows.append({"sku": sku, "wmape": wmape, "mase": float("nan"), "smape": float("nan")})
    return pd.DataFrame(rows)


# ── Fail-closed gate decision (pure logic) ─────────────────────────────────

@dataclass
class GateVerdict:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    champion_wmape: float = float("nan")
    challenger_wmape: float = float("nan")
    baseline_wmape: float = float("nan")
    champion_mase: float = float("nan")
    challenger_mase: float = float("nan")

    def as_dict(self) -> dict:
        return {
            "passed": self.passed,
            "reasons": self.reasons,
            "champion_wmape": self.champion_wmape,
            "challenger_wmape": self.challenger_wmape,
            "baseline_wmape": self.baseline_wmape,
            "champion_mase": self.champion_mase,
            "challenger_mase": self.challenger_mase,
        }


def evaluate_gate(
    champion: Optional[BacktestResult],
    challenger: BacktestResult,
    baseline: BacktestResult,
    tolerance: float = 0.02,
) -> GateVerdict:
    """Fail-closed promotion decision.

    PASS requires ALL of:
      1. challenger WMAPE is a finite number (NaN ⇒ undecidable ⇒ FAIL);
      2. challenger beats the seasonal-naive baseline (WMAPE strictly lower);
      3. challenger does not regress vs champion beyond `tolerance` on WMAPE
         OR MASE (a regression on either headline metric blocks promotion).
         When there is no champion (first model), step 3 is skipped.

    `tolerance` is the fractional WMAPE/MASE slack allowed vs the champion
    (0.02 = challenger may be at most 2% worse before it's blocked).
    """
    reasons: list[str] = []
    v = GateVerdict(
        passed=False,
        challenger_wmape=challenger.wmape_global,
        challenger_mase=challenger.mase_global,
        baseline_wmape=baseline.wmape_global,
        champion_wmape=champion.wmape_global if champion else float("nan"),
        champion_mase=champion.mase_global if champion else float("nan"),
    )

    # 1. Undecidable → fail closed.
    if not np.isfinite(challenger.wmape_global):
        reasons.append("challenger WMAPE is NaN/inf — undecidable, failing closed")
        v.reasons = reasons
        return v

    # 2. Must beat the naive baseline.
    if not np.isfinite(baseline.wmape_global):
        reasons.append("baseline WMAPE is NaN — cannot establish a floor, failing closed")
        v.reasons = reasons
        return v
    if challenger.wmape_global >= baseline.wmape_global:
        reasons.append(
            f"challenger WMAPE {challenger.wmape_global:.4f} does not beat the "
            f"seasonal-naive baseline {baseline.wmape_global:.4f}"
        )
        v.reasons = reasons
        return v

    # 3. Must not regress vs champion (when one exists).
    if champion is not None:
        for metric, ch_val, champ_val in (
            ("WMAPE", challenger.wmape_global, champion.wmape_global),
            ("MASE", challenger.mase_global, champion.mase_global),
        ):
            if np.isfinite(champ_val) and np.isfinite(ch_val):
                if ch_val > champ_val * (1.0 + tolerance):
                    reasons.append(
                        f"challenger {metric} {ch_val:.4f} regresses vs champion "
                        f"{champ_val:.4f} beyond tolerance {tolerance:.0%}"
                    )
        if reasons:
            v.reasons = reasons
            return v

    v.passed = True
    v.reasons = ["challenger beats baseline and does not regress vs champion"]
    return v


# ── Heavy orchestrator (libomp / CI-staging) ───────────────────────────────

def run_gate(
    data_path: str,
    config_path: str = "configs/config.yaml",
    holdout_days: int = 7,
    tier: str = "free",
    champion_extra: Optional[dict] = None,
    challenger_extra: Optional[dict] = None,
    tolerance: float = 0.02,
    seasonality: int = 7,
) -> dict:
    """Train champion + challenger on the same holdout, score the seasonal-naive
    baseline, and return the gate verdict. Trains LightGBM → run on CI/staging.

    champion_extra=None means "no champion" (first-model bootstrap): the gate
    then only enforces the baseline floor.
    """
    from src.validation.backtest_runner import run_baseline

    df = pd.read_csv(data_path)
    base_config = __import__(
        "src.data.loader", fromlist=["load_config"]
    ).load_config(config_path)

    baseline = score_seasonal_naive(df, base_config, holdout_days, seasonality)

    challenger = run_baseline(
        data_path, config_path, holdout_days,
        label="challenger", tier=tier, extra_config=challenger_extra,
    )
    champion = None
    if champion_extra is not None:
        champion = run_baseline(
            data_path, config_path, holdout_days,
            label="champion", tier=tier, extra_config=champion_extra,
        )

    verdict = evaluate_gate(champion, challenger, baseline, tolerance)
    return {
        "verdict": verdict.as_dict(),
        "baseline": baseline.as_dict(),
        "challenger": challenger.as_dict(),
        "champion": champion.as_dict() if champion else None,
        "tier": tier,
        "holdout_days": holdout_days,
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Forecast-quality champion/challenger gate.")
    p.add_argument("--data", default="data/test/sample_free.csv")
    p.add_argument("--config", default="configs/config.yaml")
    p.add_argument("--holdout-days", type=int, default=7)
    p.add_argument("--tier", default="free")
    p.add_argument("--champion", default=None, help="JSON extra_config for champion (omit = first-model)")
    p.add_argument("--challenger", default=None, help="JSON extra_config for challenger")
    p.add_argument("--tolerance", type=float, default=0.02)
    args = p.parse_args()

    out = run_gate(
        args.data, args.config, args.holdout_days, args.tier,
        champion_extra=json.loads(args.champion) if args.champion else None,
        challenger_extra=json.loads(args.challenger) if args.challenger else None,
        tolerance=args.tolerance,
    )
    print(json.dumps(out, indent=2, default=str))
    raise SystemExit(0 if out["verdict"]["passed"] else 1)


if __name__ == "__main__":
    main()
