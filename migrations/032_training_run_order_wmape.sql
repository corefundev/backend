-- «Точность для заказа» on a training run — WMAPE of the ORDER-WINDOW SUM,
-- stored alongside the daily headline WMAPE. HZ-1 / #464 (2026-07-16).
--
-- Why a separate pair of columns (not replace `wmape`):
--   A buyer orders the SUM for a period; daily misses cancel inside the
--   window, so daily WMAPE is the wrong lens for the purchasing decision
--   (measured: daily 0.459 compresses to 0.235 on the 14-day sum on 1c-full).
--   The math mirrors bench_wf_ab's order_sum slice bit-for-bit: group the
--   walk-forward rows by (sku, fold), window = horizon_step ≤ min(w, H),
--   WMAPE over the group SUMS. The daily `wmape` stays the engineering
--   headline; these are the product/SLA numbers surfaced by
--   GET /clients/{id}/training-runs, the training notifications and (with
--   UI-1/DS-2) the cabinet «точность для заказа» card.
--
--   NULL on runs trained before this column existed, and on runs whose
--   order-window sums have a zero denominator (no measurable demand).
--
-- Idempotent: ADD COLUMN IF NOT EXISTS.
ALTER TABLE sku_training_runs
    ADD COLUMN IF NOT EXISTS wmape_order_7  DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS wmape_order_14 DOUBLE PRECISION;
