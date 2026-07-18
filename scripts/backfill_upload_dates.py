"""DS-2 tail #467 — one-shot backfill: date_min/date_max for uploads
processed before migration 034. Reads each PROCESSED upload's stored
sandbox manifest (the values were always computed, just not persisted).

Run inside the api container (bootstrap_secrets hydrates DATABASE_URL/S3):
    docker exec -i docker-api-1 python3 - < scripts/backfill_upload_dates.py
Idempotent: rows with dates already set are skipped.
"""
import json

from src.auth.vault_agent import bootstrap_secrets

bootstrap_secrets()

from src.storage import upload_registry as ur  # noqa: E402
from src.storage import zones as z  # noqa: E402

reg = ur.get_upload_registry()
backend = z.get_zone_backend(z.Zone.PROCESSED)

done = skipped = failed = 0
for rec in reg.list_recent(limit=1000):
    if rec.status != ur.PROCESSED or rec.date_min:
        skipped += 1
        continue
    try:
        mkey = z.processed_manifest_key(rec.client_id, rec.upload_id)
        manifest = json.loads(backend.download_bytes(mkey).decode("utf-8"))
        dmin, dmax = manifest.get("date_min"), manifest.get("date_max")
        if not (dmin and dmax):
            skipped += 1
            continue
        reg.update_fields(rec.upload_id,
                          date_min=str(dmin)[:10], date_max=str(dmax)[:10])
        done += 1
    except Exception as e:  # noqa: BLE001 — per-row best effort, счёт в итоге
        print(f"FAIL {rec.upload_id}: {e}")
        failed += 1

print(f"backfilled={done} skipped={skipped} failed={failed}")
