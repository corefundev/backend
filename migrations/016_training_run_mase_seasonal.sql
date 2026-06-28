-- Seasonal MASE (m=7) on a training run, reported ALONGSIDE the existing
-- lag-1 (m=1) MASE. R14-2 / #181 (2026-06-28).
--
-- Why a second MASE column (not replace `mase`):
--   The existing `mase` scales model error against the lag-1 "same as
--   yesterday" naive (m=1) — the WEAKEST baseline for weekly-seasonal retail
--   demand, so it flatters the model. But the product itself serves a
--   SeasonalNaiveModel(seasonality=7) as its fallback, so the honest question
--   is "does the model beat the SEASONAL naive (m=7)?". `mase_seasonal` answers
--   that. We report BOTH (additive, non-breaking) rather than redefining `mase`
--   — historical `mase` values stay comparable; the new column adds the
--   stronger-baseline signal. Surfaced by GET /clients/{id}/training-runs and
--   the training-complete email.
--
--   NULL on runs trained before this column existed (and on runs whose SKUs
--   have < 8 days of history, where the m=7 naive is undefined).
--
-- Idempotent: ADD COLUMN IF NOT EXISTS.
ALTER TABLE sku_training_runs
    ADD COLUMN IF NOT EXISTS mase_seasonal DOUBLE PRECISION;
