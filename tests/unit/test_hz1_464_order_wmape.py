"""
HZ-1 (#464) — «точность для заказа» в проде: WMAPE суммы за окно заказа.

Закупщик заказывает СУММУ периода: разнознаковые дневные промахи внутри
окна взаимно гасятся (неравенство треугольника: |Σe| ≤ Σ|e|), однознаковый
сдвиг уровня честно остаётся. Прод считает ту же математику, что bench
(#473): группировка (sku, fold), окно horizon_step ≤ min(w, H), WMAPE по
СУММАМ групп — и хранит результат в sku_training_runs рядом с дневной
WMAPE (миграция 032), откуда его берут API, e-mail/telegram и кабинет.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import numpy as np
import pandas as pd

from src.validation.metrics import aggregate_metrics


def _frame(rows):
    df = pd.DataFrame(rows, columns=["sku", "fold", "horizon_step",
                                     "actual", "predicted"])
    df["date"] = pd.Timestamp("2026-01-01") + pd.to_timedelta(
        df["horizon_step"] - 1, unit="D")
    return df


def _two_sku_example() -> pd.DataFrame:
    """Ручной пример из разбора: A — тайминг-шум (сумма сходится в ноль),
    B — систематическое занижение (ничего не гасится)."""
    rows = []
    for step, pred in enumerate([14, 5, 13, 8], start=1):    # A: Σ=40 = факт
        rows.append(("A", 0, step, 10.0, float(pred)))
    for step, pred in enumerate([7, 8, 7, 8], start=1):      # B: Σ=30 vs 40
        rows.append(("B", 0, step, 10.0, float(pred)))
    return _frame(rows)


def _metrics_df(raw: pd.DataFrame) -> pd.DataFrame:
    # aggregate_metrics needs a per-SKU frame; the order metric only uses raw_df.
    return pd.DataFrame({"wmape": [0.1], "mase": [1.0], "smape": [0.1]})


# ── математика ────────────────────────────────────────────────────────────

def test_hand_example_daily_030_order_0125():
    raw = _two_sku_example()
    agg = aggregate_metrics(_metrics_df(raw), raw_df=raw)
    assert abs(agg["wmape_global"] - 0.30) < 1e-9          # (14+10)/80
    # h_max=4 → оба продуктовых окна усечены до полного горизонта
    assert abs(agg["wmape_order_7"] - 0.125) < 1e-9        # (0+10)/80
    assert agg["wmape_order_14"] == agg["wmape_order_7"]


def test_pure_timing_noise_cancels_to_zero():
    raw = _two_sku_example()
    raw = raw[raw["sku"] == "A"].copy()                    # только шумный SKU
    agg = aggregate_metrics(_metrics_df(raw), raw_df=raw)
    assert agg["wmape_global"] > 0.3                       # дни промахнуты
    assert agg["wmape_order_7"] == 0.0                     # сумма — нет


def test_order_never_exceeds_daily_triangle_inequality():
    rng = np.random.RandomState(464)
    rows = []
    for sku in ("X", "Y", "Z"):
        for fold in (0, 1):
            actual = rng.uniform(5, 20, size=14)
            pred = actual + rng.uniform(-6, 6, size=14)
            for step in range(1, 15):
                rows.append((sku, fold, step,
                             float(actual[step - 1]), float(pred[step - 1])))
    raw = _frame(rows)
    agg = aggregate_metrics(_metrics_df(raw), raw_df=raw)
    assert agg["wmape_order_14"] <= agg["wmape_global"] + 1e-12
    assert agg["wmape_order_7"] <= agg["wmape_global"] + 1e-12


def test_windows_differ_when_error_sits_in_the_tail():
    rows = []
    for step in range(1, 15):
        pred = 10.0 if step <= 7 else 16.0                 # промах только в 8..14
        rows.append(("A", 0, step, 10.0, pred))
    raw = _frame(rows)
    agg = aggregate_metrics(_metrics_df(raw), raw_df=raw)
    assert agg["wmape_order_7"] == 0.0
    assert agg["wmape_order_14"] > 0.2


def test_zero_denominator_and_missing_column_leave_keys_absent():
    raw = _two_sku_example()
    raw["actual"] = 0.0
    agg = aggregate_metrics(_metrics_df(raw), raw_df=raw)
    assert "wmape_order_7" not in agg and "wmape_order_14" not in agg

    raw2 = _two_sku_example().drop(columns=["horizon_step"])
    agg2 = aggregate_metrics(_metrics_df(raw2), raw_df=raw2)
    assert "wmape_order_7" not in agg2                     # рукодельный raw_df


def test_folds_do_not_bleed_into_each_other():
    """Разнознаковые промахи в РАЗНЫХ фолдах гасить нельзя — окно заказа
    живёт внутри фолда (дисциплина L-A7)."""
    rows = [("A", 0, 1, 10.0, 16.0),                       # +6 в фолде 0
            ("A", 1, 1, 10.0, 4.0)]                        # −6 в фолде 1
    raw = _frame(rows)
    agg = aggregate_metrics(_metrics_df(raw), raw_df=raw)
    assert abs(agg["wmape_order_7"] - 12.0 / 20.0) < 1e-9  # не 0!


# ── бит-в-бит паритет со стендом (#473) ───────────────────────────────────

def test_parity_with_bench_order_sum():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "bench_wf_ab", Path("scripts/bench_wf_ab.py"))
    bench = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bench)

    rng = np.random.RandomState(7)
    split0, split1 = pd.Timestamp("2026-02-01"), pd.Timestamp("2026-02-15")
    rows = []
    for sku in ("A", "B", "C"):
        for fold, split in ((0, split0), (1, split1)):
            actual = rng.uniform(0, 15, size=14)
            pred = np.clip(actual + rng.uniform(-5, 5, size=14), 0, None)
            for step in range(1, 15):
                rows.append({"sku": sku, "fold": fold,
                             "date": split + pd.Timedelta(days=step),
                             "actual": float(actual[step - 1]),
                             "predicted": float(pred[step - 1])})
    combined = pd.DataFrame(rows)

    out = bench.decompose(combined, [split0, split1],
                          {"A": 0, "B": 1, "C": 2}, "sku")
    raw = combined.copy()
    raw["horizon_step"] = (
        raw["date"] - raw["fold"].map({0: split0, 1: split1})).dt.days
    agg = aggregate_metrics(_metrics_df(raw), raw_df=raw)

    assert abs(agg["wmape_order_7"] - out["order_sum"]["7"]["wmape"]) < 1e-5
    assert abs(agg["wmape_order_14"] - out["order_sum"]["14"]["wmape"]) < 1e-5


# ── walk_forward штампует horizon_step ────────────────────────────────────

def test_walk_forward_combined_carries_horizon_step():
    from src.validation.walk_forward import walk_forward_validate
    from tests.unit.test_improvements import _make_df, _make_config

    class _Stub:
        is_mimo = True

        def __init__(self, h):
            self.h = h

        def fit(self, X, y, groups=None, sample_weight=None):
            return self

        def predict(self, last_row):
            return np.full((1, self.h), 5.0)

    df = _make_df(n_skus=2, n_days=40)
    config = _make_config(horizon=3)
    config["validation"]["n_splits"] = 2
    res = walk_forward_validate(df, lambda: _Stub(3), ["price"], config)
    assert "horizon_step" in res.combined.columns
    steps = res.combined.groupby("fold")["horizon_step"].agg(["min", "max"])
    assert (steps["min"] == 1).all() and (steps["max"] <= 3).all()
    # и продуктовые ключи доехали до agg
    assert "wmape_order_7" in res.aggregated


# ── проводка: run row, уведомления, миграция ─────────────────────────────

def test_task_queue_persists_and_fans_out():
    tq = Path("src/pipeline/task_queue.py").read_text()
    assert 'wmape_order_7=metrics.get("wmape_order_7")' in tq
    assert 'wmape_order_14=metrics.get("wmape_order_14")' in tq
    assert 'wmape_order_14=metrics.get("wmape_order_14")' in tq.split("notif_args = dict(")[1]


def test_both_senders_accept_the_kwarg_and_render_the_number():
    from src.notifications.training_email import notify_training_finished as e
    from src.notifications.telegram import notify_training_finished as t
    for fn in (e, t):
        assert "wmape_order_14" in inspect.signature(fn).parameters
    for p in ("src/notifications/training_email.py",
              "src/notifications/telegram.py"):
        assert "Точность для заказа" in Path(p).read_text(), p


def test_email_renders_percentage_not_wmape():
    from src.notifications.training_email import _render_finished
    _, body = _render_finished("acme", 60.0, 5, 0.459, 1.0,
                               wmape_order_14=0.235)
    assert "Точность для заказа (14 дн): 76.5%" in body
    _, body_none = _render_finished("acme", 60.0, 5, 0.459, 1.0)
    assert "Точность для заказа (14 дн): —" in body_none


def test_registry_and_migration():
    from src.storage.training_runs import (
        PostgresTrainingRunsRegistry, TrainingRunRecord, to_dict,
    )
    cols = PostgresTrainingRunsRegistry._UPDATABLE_COLUMNS
    assert {"wmape_order_7", "wmape_order_14"} <= cols
    rec = TrainingRunRecord(run_id="r", client_id="c", plan="free",
                            data_path="p", wmape_order_14=0.235)
    d = to_dict(rec)
    assert d["wmape_order_14"] == 0.235 and d["wmape_order_7"] is None

    mig = Path("migrations/032_training_run_order_wmape.sql").read_text()
    assert "ADD COLUMN IF NOT EXISTS wmape_order_7" in mig
    assert "ADD COLUMN IF NOT EXISTS wmape_order_14" in mig
