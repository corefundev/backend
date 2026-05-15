"""All-SKU sweep — find which SKUs make WMAPE_global blow up.

Ad-hoc ML diagnostic. Run INSIDE the api/worker container so the
Lockbox bootstrap can fetch secrets (DATABASE_URL, S3 creds):

    docker exec -it docker-worker-1 python scripts/diag_all_skus.py

Edit `CLIENT` below for the tenant you want to inspect. Output goes
to stdout — pipe to a file if you want to keep it.
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
from src.features.engineering import build_features

CLIENT = "test"
storage = ClientStorage(CLIENT)
final_model = storage.load_model()
cfg = get_config_manager("configs/config.yaml").get_effective(CLIENT, get_registry())

recs = ur.get_upload_registry().list_for_client(CLIENT, limit=10)
rec = [r for r in recs if r.status == "processed"][0]
df = load_data(up.get_processed_path(rec), cfg)
df = build_features(df, cfg)

sku_col = cfg["data"]["sku_col"]
date_col = cfg["data"]["date_col"]
target_col = cfg["data"]["target_col"]
H = cfg["model"]["horizon"]

end = df[date_col].max()
split = end - pd.Timedelta(days=H)
train_df = df[df[date_col] <= split].copy()
test_df  = df[(df[date_col] > split) & (df[date_col] <= split + pd.Timedelta(days=H))].copy()

rows = []
total_err = 0.0
total_act = 0.0
for sku, sku_train in train_df.groupby(sku_col):
    sku_test = test_df[test_df[sku_col] == sku].sort_values(date_col)
    if sku_test.empty:
        continue
    sku_train = sku_train.sort_values(date_col)
    last_row  = sku_train.iloc[[-1]]
    raw = final_model.predict(last_row)
    preds = np.clip(np.asarray(raw)[0], 0, None)
    acts = sku_test[target_col].to_numpy()[:H]
    preds = preds[:len(acts)]
    err = float(np.abs(acts - preds).sum())
    act = float(np.abs(acts).sum())
    total_err += err
    total_act += act
    wmape = err / act if act > 1e-3 else float("nan")
    rows.append({"sku": sku, "act_sum": act, "pred_sum": float(preds.sum()), "err_sum": err, "wmape": wmape})

dfm = pd.DataFrame(rows).sort_values("err_sum", ascending=False)
print(f"\nTotal: err={total_err:.0f}  act={total_act:.0f}  GLOBAL WMAPE={total_err/total_act:.3f}")
print(f"\nTop 10 worst SKUs by error contribution:")
print(dfm.head(10).to_string(index=False))
print(f"\nWMAPE distribution (excluding NaN):")
v = dfm["wmape"].dropna()
print(f"  count={len(v)}  median={v.median():.3f}  mean={v.mean():.3f}  p90={v.quantile(0.9):.3f}  p99={v.quantile(0.99):.3f}  max={v.max():.3f}")
print(f"\nSKUs with NaN WMAPE (sum(actual)≈0 in test window): {dfm['wmape'].isna().sum()}")
print(f"  but their pred contribution to total_err: {dfm[dfm['wmape'].isna()]['err_sum'].sum():.0f}")
