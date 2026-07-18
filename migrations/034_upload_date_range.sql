-- DS-2 #467 tail: per-upload data period for the «Период данных» columns.
-- The sandbox manifest has always computed date_min/date_max — persist
-- them on the registry row at prep completion. NULL = pre-034 upload
-- (backfilled from stored manifests by scripts/backfill_upload_dates.py).
ALTER TABLE sku_uploads
    ADD COLUMN IF NOT EXISTS date_min DATE,
    ADD COLUMN IF NOT EXISTS date_max DATE;
