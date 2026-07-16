-- DS-1 #466 slice B: dataset-scoped models and forecasts.
-- sku_forecasts becomes partitioned by dataset: PK gains dataset_id so
-- two datasets of one client can hold forecasts for the same (sku, date).
-- Idempotent: guarded by the current PK column count.

-- 1. Container «Основной» for clients that have forecasts but never
--    opened the datasets page (lazy migration hasn't fired for them).
--    Deterministic id → safe to re-run.
INSERT INTO sku_datasets (dataset_id, client_id, name)
SELECT md5(f.client_id || ':default-dataset'), f.client_id, 'Основной'
FROM (SELECT DISTINCT client_id FROM sku_forecasts) f
WHERE NOT EXISTS (
    SELECT 1 FROM sku_datasets d
    WHERE d.client_id = f.client_id AND d.status = 'active')
ON CONFLICT (dataset_id) DO NOTHING;

-- 2. Backfill: existing forecasts belong to the client's default
--    (earliest active) dataset.
UPDATE sku_forecasts f SET dataset_id = d.dataset_id
FROM (
    SELECT DISTINCT ON (client_id) client_id, dataset_id
    FROM sku_datasets WHERE status = 'active'
    ORDER BY client_id, created_at
) d
WHERE f.dataset_id IS NULL AND f.client_id = d.client_id;

-- 3. Safety net for any straggler rows (client with forecasts whose
--    dataset insert somehow failed): a visible sentinel, not a NULL.
UPDATE sku_forecasts SET dataset_id = 'legacy' WHERE dataset_id IS NULL;

-- 4. PK swap (only if still on the 3-column PK).
DO $$
BEGIN
    IF (SELECT count(*) FROM information_schema.key_column_usage
        WHERE table_name = 'sku_forecasts'
          AND constraint_name = 'sku_forecasts_pkey') = 3 THEN
        ALTER TABLE sku_forecasts DROP CONSTRAINT sku_forecasts_pkey;
        ALTER TABLE sku_forecasts ALTER COLUMN dataset_id SET NOT NULL;
        ALTER TABLE sku_forecasts
            ADD PRIMARY KEY (client_id, dataset_id, sku, forecast_date);
    END IF;
END $$;
