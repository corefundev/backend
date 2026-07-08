-- DP-4b (#324): prep-stage state for «Подготовка данных».
-- sniff_report      — the in-sandbox format sniff (headers, sample, encoding…)
-- mapping_proposal  — column-mapping engine output (per-field confidence)
-- confirmed_mapping — the {canonical: source} mapping applied at parse time,
--                     filled by the prep worker's SYSTEM auto-mapping (there is
--                     no user mapping step). NULL for legacy/pre-prep uploads.
ALTER TABLE sku_uploads ADD COLUMN IF NOT EXISTS sniff_report      JSONB;
ALTER TABLE sku_uploads ADD COLUMN IF NOT EXISTS mapping_proposal  JSONB;
ALTER TABLE sku_uploads ADD COLUMN IF NOT EXISTS confirmed_mapping JSONB;
