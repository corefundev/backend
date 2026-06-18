-- Drop the dormant monthly-training-counter columns. #73 (2026-06-18).
--
-- Monthly run caps were an early idea, removed functionally 2026-06-02
-- (PR #71) — no plan caps monthly training runs. The two counter columns
-- below were left in place but unused (every plan = unlimited). The app
-- code that read/wrote them is removed in the same change-set as this
-- migration (registry.try_record_training_run no longer references them;
-- ClientRecord, quota.QuotaStatus, PlanSpec, and the FE types dropped the
-- fields), so the columns are pure dead schema.
--
-- The only remaining training throttles are the per-plan cooldown
-- (Free = 12h) and the single-in-flight gate (R11-H4), neither of which
-- touches these columns. Forward-only per migrations/README.md.
--
-- IF EXISTS keeps this idempotent against any environment where a prior
-- manual cleanup already dropped them.

ALTER TABLE sku_clients DROP COLUMN IF EXISTS training_runs_this_month;
ALTER TABLE sku_clients DROP COLUMN IF EXISTS training_runs_window_start;
