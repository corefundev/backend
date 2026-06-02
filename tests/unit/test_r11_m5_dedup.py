"""
tests/unit/test_r11_m5_dedup.py

R11-M5 — the forecasts/anomalies replace_for_client paths dedup their
batch by (sku, date) before the INSERT. An intra-batch duplicate would
otherwise raise UniqueViolation against the composite PK and roll back
the whole DELETE+INSERT, leaving the client's PREVIOUS data silently
stale. (ON CONFLICT can't fix an intra-statement dup — Postgres refuses
to "affect a row a second time".)

These tests pin the pure dedup helper that both registries use; the SQL
INSERT then never sees a duplicate key.
"""
from __future__ import annotations

from src.storage._dedup import dedup_last_wins


def _key(r):
    return (r[0], r[1])  # (sku, date)


def test_no_duplicates_passthrough():
    rows = [("A", "2026-01-01", 1.0), ("A", "2026-01-02", 2.0), ("B", "2026-01-01", 3.0)]
    assert dedup_last_wins(rows, key=_key) == rows


def test_intra_batch_duplicate_collapsed_last_wins():
    # The bug case: same (sku, date) twice (e.g. two warehouses).
    rows = [
        ("A", "2026-01-01", 10.0),
        ("B", "2026-01-01", 5.0),
        ("A", "2026-01-01", 99.0),   # dup of row 0 — must win
    ]
    out = dedup_last_wins(rows, key=_key)
    assert len(out) == 2
    by_key = {(_key(r)): r for r in out}
    assert by_key[("A", "2026-01-01")][2] == 99.0   # last value won
    assert by_key[("B", "2026-01-01")][2] == 5.0
    # No duplicate keys survive → the INSERT can't UniqueViolation.
    keys = [_key(r) for r in out]
    assert len(keys) == len(set(keys))


def test_forecasts_5tuple_shape():
    # (sku, fdate, value, p10, p90) — the forecasts batch shape.
    rows = [
        ("A", "2026-01-01", 1.0, 0.5, 1.5),
        ("A", "2026-01-01", 2.0, None, None),  # dup → wins
    ]
    out = dedup_last_wins(rows, key=lambda r: (r[0], r[1]))
    assert out == [("A", "2026-01-01", 2.0, None, None)]


def test_empty():
    assert dedup_last_wins([], key=_key) == []
