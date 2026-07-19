-- MA-2 #520 (аудит F3): объёмный headline маскирует хвост боли —
-- публикуем распределение per-SKU рядом с ним + честность охвата (MA-1).
ALTER TABLE sku_training_runs
    ADD COLUMN IF NOT EXISTS wmape_median  DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS wmape_p90     DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS eval_coverage DOUBLE PRECISION;
