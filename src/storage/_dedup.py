"""
src/storage/_dedup.py

Intra-batch row dedup for the DELETE-then-INSERT `replace_for_client`
paths (forecasts, anomalies). R11-M5.

Those tables have a composite PK (client_id, sku, date). `replace_for_client`
DELETEs all of a client's rows then bulk-INSERTs the new batch, so there
are no EXISTING rows to collide with — the only collision source is a
DUPLICATE (sku, date) WITHIN the incoming batch (multi-warehouse /
multi-channel uploads flag the same (sku, date) more than once). An
unhandled duplicate raises `UniqueViolation` and rolls back the whole
DELETE+INSERT, leaving the client's PREVIOUS data silently stale.

`ON CONFLICT DO UPDATE` does NOT fix this: Postgres rejects a single
statement that would "affect a row a second time", so an intra-batch
duplicate fails regardless. Deduping before the INSERT is the fix.
"""
from __future__ import annotations

from typing import Callable, Hashable, Sequence, TypeVar

_T = TypeVar("_T")


def dedup_last_wins(rows: Sequence[_T], key: Callable[[_T], Hashable]) -> list[_T]:
    """Collapse rows sharing the same `key`; the LAST occurrence wins.

    Deterministic given input order. Insertion order of surviving keys is
    preserved (dict preserves it), so output ordering is stable.
    """
    return list({key(r): r for r in rows}.values())
