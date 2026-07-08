-- #308 newsvendor: service-level ORDER-QUANTITY recommendation per forecast row.
-- Interpolated from the calibrated [p10,p90] at the client's model.service_level.
-- NULL when the model has no calibrated band (old models / recursive tail) —
-- the UI then shows no order recommendation for that row.

ALTER TABLE sku_forecasts ADD COLUMN IF NOT EXISTS order_qty DOUBLE PRECISION;
