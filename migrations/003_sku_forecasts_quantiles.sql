-- P10/P90 columns for the confidence ribbon on the forecast chart.
-- Added in v0.8.26.

ALTER TABLE sku_forecasts ADD COLUMN IF NOT EXISTS p10 DOUBLE PRECISION;
ALTER TABLE sku_forecasts ADD COLUMN IF NOT EXISTS p90 DOUBLE PRECISION;
