-- DS-2 #467: dataset-centric «Данные» UX.
--
-- 1. sku_uploads.dataset_id — target dataset chosen at upload time
--    («Доложить CSV» from a dataset page / «Создать датасет» flow).
--    After the USER-triggered prep completes (#320 flow unchanged), the
--    process worker auto-attaches the upload to this dataset. NULL =
--    legacy flow (upload not aimed at a dataset).
-- 2. sku_dataset_files.merge_added / merge_replaced — per-file merge
--    delta persisted at attach time («+4 132 · заменено 380» column).
--    The per-version merge_report keeps the authoritative aggregate.
-- 3. sku_training_runs.baseline_wmape — seasonal-naive WMAPE the
--    promotion gate (#227) already computes but never persisted; needed
--    for «Точнее наивного прогноза на N%» in the dataset model card.
--    NULL = pre-DS-2 run or gate skipped/errored.

ALTER TABLE sku_uploads
    ADD COLUMN IF NOT EXISTS dataset_id TEXT;

ALTER TABLE sku_dataset_files
    ADD COLUMN IF NOT EXISTS merge_added    INTEGER,
    ADD COLUMN IF NOT EXISTS merge_replaced INTEGER;

ALTER TABLE sku_training_runs
    ADD COLUMN IF NOT EXISTS baseline_wmape DOUBLE PRECISION;

-- «История подготовок» filters by dataset; partial index keeps the
-- legacy-NULL majority out.
CREATE INDEX IF NOT EXISTS idx_sku_uploads_dataset
    ON sku_uploads (dataset_id) WHERE dataset_id IS NOT NULL;
