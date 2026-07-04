-- 017 (QW2-4 #227): champion/challenger promotion gate verdict per run.
--   gate_passed = TRUE  → challenger promoted (or first model, no champion)
--   gate_passed = FALSE → BLOCKED: champion artifact kept serving; this
--                         run's model was never saved (model_path is NULL)
--   gate_passed IS NULL → pre-gate runs / promotion_gate disabled
ALTER TABLE sku_training_runs ADD COLUMN IF NOT EXISTS gate_passed BOOLEAN;
