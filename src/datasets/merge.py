"""DS-1 #466 — deterministic dataset merge.

Owner rule (2026-07-16): when an appended file overlaps the existing
history on (sku, date), the NEW file wins — this gives retro-corrections
for free (re-upload the fixed month). Every merge yields a report the
UI shows to the client («добавлено N / заменено M»).

Pure pandas, no I/O — unit-testable; the pipeline layer feeds it frames
from the PROCESSED zone.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

import pandas as pd

#: canonical processed-parquet columns (the prep pipeline guarantees them)
DATE_COL, SKU_COL = "date", "sku"


@dataclass(frozen=True)
class MergeResult:
    frame: pd.DataFrame
    added: int          # rows that did not exist in the base
    replaced: int       # (sku, date) pairs the new file overwrote


def merge_frames(base: pd.DataFrame | None, new: pd.DataFrame) -> MergeResult:
    """Merge `new` into `base`; new wins on (sku, date) overlap.

    Output is sorted by (sku, date) and index-reset — deterministic, so
    the snapshot fingerprint is stable for identical content.
    """
    new = new.copy()
    new[DATE_COL] = pd.to_datetime(new[DATE_COL])
    new[SKU_COL] = new[SKU_COL].astype(str)
    # A single file may carry duplicate (sku, date) rows — last one wins
    # inside the file too (matches «новый побеждает» end-to-end).
    new = new.drop_duplicates([SKU_COL, DATE_COL], keep="last")

    if base is None or base.empty:
        out = new.sort_values([SKU_COL, DATE_COL]).reset_index(drop=True)
        return MergeResult(out, added=len(out), replaced=0)

    base = base.copy()
    base[DATE_COL] = pd.to_datetime(base[DATE_COL])
    base[SKU_COL] = base[SKU_COL].astype(str)

    key_base = pd.MultiIndex.from_frame(base[[SKU_COL, DATE_COL]])
    key_new = pd.MultiIndex.from_frame(new[[SKU_COL, DATE_COL]])
    overlap = key_base.isin(key_new)

    kept = base.loc[~overlap]
    out = (pd.concat([kept, new], ignore_index=True)
             .sort_values([SKU_COL, DATE_COL]).reset_index(drop=True))
    replaced = int(overlap.sum())
    return MergeResult(out, added=len(new) - replaced, replaced=replaced)


def fingerprint(df: pd.DataFrame, target_col: str = "sales") -> str:
    """Content sha12 over (sku, date, target) — the same recipe as the
    bench harness, so ledger/provenance language is shared."""
    key = (df[[SKU_COL, DATE_COL, target_col]]
           .sort_values([SKU_COL, DATE_COL]).reset_index(drop=True))
    return hashlib.sha256(
        pd.util.hash_pandas_object(key, index=False).to_numpy().tobytes()
    ).hexdigest()[:12]
