#!/usr/bin/env bash
# scripts/run_pipeline.sh
# Convenience wrapper: generate data → train → batch inference
set -euo pipefail

SKUS=${1:-20}
DAYS=${2:-180}
CLIENT=${3:-default}
CONFIG="configs/config.yaml"
DATA="data/sample.csv"
ARTIFACTS="artifacts"

echo "======================================"
echo " SKU Forecasting Pipeline"
echo " SKUs=$SKUS  DAYS=$DAYS  CLIENT=$CLIENT"
echo "======================================"

# 1. Generate sample data
echo ""
echo "[1/3] Generating synthetic data..."
python scripts/generate_sample_data.py --skus "$SKUS" --days "$DAYS" --output "$DATA"

# 2. Train
echo ""
echo "[2/3] Running training pipeline..."
python -m src.pipeline.train \
  --data "$DATA" \
  --config "$CONFIG" \
  --client "$CLIENT" \
  --output "$ARTIFACTS"

# 3. Batch inference
echo ""
echo "[3/3] Running batch inference..."
python -c "
from src.pipeline.batch_inference import run_batch_inference
df = run_batch_inference(
    data_path='$DATA',
    model_path='$ARTIFACTS/$CLIENT/model.pkl',
    config_path='$CONFIG',
    client_id='$CLIENT',
    output_path='$ARTIFACTS/$CLIENT/forecasts.parquet',
)
print(f'Forecasts generated: {len(df)} rows')
print(df.head(10).to_string())
"

echo ""
echo "======================================"
echo " Pipeline complete!"
echo " Model:     $ARTIFACTS/$CLIENT/model.pkl"
echo " Forecasts: $ARTIFACTS/$CLIENT/forecasts.parquet"
echo " Metrics:   $ARTIFACTS/$CLIENT/per_sku_metrics.csv"
echo "======================================"
