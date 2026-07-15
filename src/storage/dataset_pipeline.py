"""DS-1 #466 — dataset materialization: merge processed uploads into a
versioned snapshot in the PROCESSED zone.

Runs synchronously in the API process: inputs are TRUSTED parquets the
sandboxed prep pipeline already produced (≤50 MiB per source file), so
a merge is seconds of pandas — no new worker plumbing for v1. The size
guard below keeps a pathological dataset from tying up a request.
"""
from __future__ import annotations

import io
import logging
from typing import Optional

import pandas as pd

from src.datasets.merge import fingerprint, merge_frames
from src.storage import upload_registry as ur
from src.storage import zones as z
from src.storage.datasets import (
    DatasetVersion,
    V_FAILED,
    V_READY,
    get_datasets_registry,
)

logger = logging.getLogger(__name__)

#: hard row ceiling for a materialized snapshot (fail-closed guard; the
#: biggest real dataset today is ~220k rows — 5M is far above any plan).
MAX_SNAPSHOT_ROWS = 5_000_000


class MaterializeError(RuntimeError):
    """User-addressable materialization failure (maps to 4xx)."""


def snapshot_key(client_id: str, dataset_id: str, version: int) -> str:
    return z.safe_join(client_id, "datasets", dataset_id,
                       f"v{version:04d}", "data.parquet")


def _read_parquet(key: str) -> pd.DataFrame:
    backend = z.get_zone_backend(z.Zone.PROCESSED)
    return pd.read_parquet(io.BytesIO(backend.download_bytes(key)))


def _load_upload_frame(upload_id: str, client_id: str) -> pd.DataFrame:
    reg = ur.get_upload_registry()
    rec = reg.get(upload_id)
    if rec is None or rec.client_id != client_id:
        raise MaterializeError("upload not found")           # R4-7: no leak
    if rec.status != ur.PROCESSED or not rec.processed_key:
        raise MaterializeError(
            f"файл ещё не обработан (статус: {rec.status})")
    return _read_parquet(rec.processed_key)


def materialize(dataset_id: str, *,
                new_upload_id: Optional[str] = None) -> DatasetVersion:
    """Build version current+1: append one upload (new wins on overlap)
    or full rebuild from the attached files when new_upload_id is None.

    The version row is persisted even on failure (status=failed with the
    error in merge_report) — tamper-evident history over silent retries.
    """
    reg = get_datasets_registry()
    ds = reg.get(dataset_id)
    if ds is None:
        raise MaterializeError("dataset not found")
    version = ds.current_version + 1

    try:
        if new_upload_id is not None:
            base = None
            if ds.current_version > 0:
                cur = reg.get_version(dataset_id, ds.current_version)
                if cur and cur.status == V_READY and cur.snapshot_key:
                    base = _read_parquet(cur.snapshot_key)
            res = merge_frames(base,
                               _load_upload_frame(new_upload_id, ds.client_id))
            report = {"kind": "append", "source_upload_id": new_upload_id,
                      "added": res.added, "replaced": res.replaced}
        else:
            files = reg.list_files(dataset_id)
            if not files:
                raise MaterializeError("в датасете нет файлов")
            merged, added, replaced = None, 0, 0
            for f in files:                     # added_at order: later wins
                res = merge_frames(merged,
                                   _load_upload_frame(f["upload_id"],
                                                      ds.client_id))
                merged, added, replaced = res.frame, added + res.added, \
                    replaced + res.replaced
            res = merge_frames(None, merged)    # normalize container
            report = {"kind": "rebuild", "added": added,
                      "replaced": replaced, "files": len(files)}

        frame = res.frame
        if len(frame) > MAX_SNAPSHOT_ROWS:
            raise MaterializeError(
                f"снапшот слишком велик ({len(frame)} строк, "
                f"потолок {MAX_SNAPSHOT_ROWS})")

        key = snapshot_key(ds.client_id, dataset_id, version)
        buf = io.BytesIO()
        frame.to_parquet(buf, index=False)
        z.get_zone_backend(z.Zone.PROCESSED).upload_bytes(buf.getvalue(), key)

        v = DatasetVersion(
            dataset_id=dataset_id, version=version, status=V_READY,
            fingerprint=fingerprint(frame), snapshot_key=key,
            row_count=int(len(frame)),
            sku_count=int(frame["sku"].nunique()),
            date_min=str(pd.Timestamp(frame["date"].min()).date()),
            date_max=str(pd.Timestamp(frame["date"].max()).date()),
            merge_report=report)
        reg.add_version(v)
        logger.info(
            f"dataset {dataset_id} v{version}: {v.row_count} rows, "
            f"{v.sku_count} skus, fp={v.fingerprint} ({report['kind']})")
        return v
    except MaterializeError as e:
        reg.add_version(DatasetVersion(
            dataset_id=dataset_id, version=version, status=V_FAILED,
            merge_report={"error": str(e)}))
        raise
