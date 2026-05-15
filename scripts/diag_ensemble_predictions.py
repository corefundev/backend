"""
Print actual ensemble predictions for several SKUs on the most recent
fold to understand why direct multi-step WMAPE is catastrophic.

Ad-hoc ML diagnostic. Run inside the api/worker container so secrets
bootstrap loads:

    docker exec -it docker-worker-1 python scripts/diag_ensemble_predictions.py

Edit `CLIENT` below for the tenant. Output: per-SKU prediction
breakdown for the last validation fold.
"""
from src.auth.vault_agent import bootstrap_secrets
bootstrap_secrets()

import numpy as np
import pandas as pd

from src.storage.backend import ClientStorage
from src.storage import upload_registry as ur, upload_pipeline as up
from src.clients.config_manager import get_config_manager
from src.clients.registry import get_registry
from src.data.loader import load_data
from src.features.engineering import build_features, get_feature_columns

CLIENT = "test"

storage = ClientStorage(CLIENT)
final_model = storage.load_model()
print("final model class:", type(final_model).__name__)

cfg = get_config_manager("configs/config.yaml").get_effective(CLIENT, get_registry())
recs = ur.get_upload_registry().list_for_client(CLIENT, limit=10)
rec = [r for r in recs if r.status == "processed"][0]
print("dataset:", vars(rec))

df = load_data(up.get_processed_path(rec), cfg)
df = build_features(df, cfg)
fcols = get_feature_columns(df, cfg)
sku_col, date_col, target_col = cfg["data"]["sku_col"], cfg["data"]["date_col"], cfg["data"]["target_col"]
H = cfg["model"]["horizon"]

end = df[date_col].max()
split = end - pd.Timedelta(days=H)
train_df = df[df[date_col] <= split].copy()
test_df  = df[(df[date_col] > split) & (df[date_col] <= split + pd.Timedelta(days=H))].copy()
print(f"split={split.date()}  train={len(train_df)}  test={len(test_df)}")

print("\nblend weights count:", len(getattr(final_model, "weights_", {})))
print("default weights:", getattr(final_model, "default_weights", "?"))

for sku in ["SKU_0000", "SKU_0010", "SKU_0030", "SKU_0050"]:
    sku_train = train_df[train_df[sku_col] == sku].sort_values(date_col)
    sku_test  = test_df[test_df[sku_col] == sku].sort_values(date_col)
    if sku_train.empty or sku_test.empty:
        print(f"\n{sku}: skip (empty)")
        continue
    last_row  = sku_train.iloc[[-1]]
    last_date = pd.Timestamp(last_row[date_col].iloc[0]).normalize()

    raw = final_model.predict(last_row)
    preds_h = np.clip(np.asarray(raw)[0], 0, None)

    actuals = sku_test[target_col].to_numpy()
    last_train_sales = float(sku_train[target_col].iloc[-1])
    weights_for_sku = final_model.weights_.get(sku, final_model.default_weights) if hasattr(final_model, "weights_") else None

    print(f"\n=== {sku}  weights={weights_for_sku}")
    print(f"  last_train_date={last_date.date()}  last_train_sales={last_train_sales:.1f}")
    print(f"  actuals: {[round(a,1) for a in actuals[:14]]}")
    print(f"  preds:   {[round(p,1) for p in preds_h[:14]]}")

    # Per-child predictions (ensemble only)
    if hasattr(final_model, "models_") and isinstance(final_model.models_, dict):
        for obj, child in final_model.models_.items():
            cp = np.clip(child.predict(last_row), 0, None)[0]
            print(f"    {obj:14s}: {[round(p,1) for p in cp[:14]]}")

    err = np.abs(actuals[:H] - preds_h[:H]).sum()
    act = np.abs(actuals[:H]).sum()
    wmape = err / act if act > 0 else float("nan")
    print(f"  WMAPE={wmape:.3f}")
