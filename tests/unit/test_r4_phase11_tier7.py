"""
Regression tests for Round-4 Phase 11 Tier 7 (2026-05-17).

R4-15 — HierarchicalReconciler.method='top_down' and
        method='middle_out' were silent mathematical no-ops:

            totals = df.groupby([...])[fc].transform("sum")
            sums   = df.groupby([...])[fc].transform("sum")  # ← identical
            df[fc] = (df[fc] / (sums + 1e-8)) * totals       # ← == df[fc]

        Callers got bottom-up math while believing they had real
        top-down / middle-out reconciliation. Fixed by raising
        NotImplementedError with clear documentation of the API
        change required to ship real implementations.

These tests pin the fail-fast contract so future refactors can't
silently revive the no-op shape.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest


_BACKEND = Path(__file__).resolve().parents[2]


# ── Source-level invariants ──────────────────────────────────────────────

def _executable_body(text: str, method_name: str) -> str:
    """Return the code AFTER the closing triple-quote of the docstring,
    excluding the docstring itself. This is what actually executes —
    docstring text (which legitimately mentions the old no-op pattern
    as historical context) must not trigger source-level guards."""
    start = text.find(f"    def {method_name}(self")
    assert start > 0
    next_method = text.find("\n    def ", start + 1)
    end = next_method if next_method > 0 else len(text)
    block = text[start:end]
    # Find the second triple-quote — closing docstring.
    first_q = block.find('"""')
    second_q = block.find('"""', first_q + 3)
    if second_q < 0:
        return block  # no docstring, whole body is code
    return block[second_q + 3:]


def test_hierarchical_top_down_source_raises_not_implemented():
    """The _top_down EXECUTABLE BODY (post-docstring) must raise
    NotImplementedError. Guards against future refactor that
    re-introduces the no-op."""
    text = (_BACKEND / "src" / "models" / "hierarchical.py").read_text()
    body = _executable_body(text, "_top_down")
    assert "raise NotImplementedError" in body, (
        "_top_down executable body must raise NotImplementedError (R4-15)"
    )


def test_hierarchical_middle_out_source_raises_not_implemented():
    """Same fail-fast invariant for _middle_out executable body."""
    text = (_BACKEND / "src" / "models" / "hierarchical.py").read_text()
    body = _executable_body(text, "_middle_out")
    assert "raise NotImplementedError" in body, (
        "_middle_out executable body must raise NotImplementedError (R4-15)"
    )


def test_hierarchical_no_silent_no_op_division_pattern():
    """The specific no-op math pattern must not reappear as
    EXECUTABLE CODE: `(x / sum) * sum == x`. Docstring may mention
    it as historical context — only the post-docstring body is
    checked."""
    text = (_BACKEND / "src" / "models" / "hierarchical.py").read_text()
    for method_name in ("_top_down", "_middle_out"):
        body = _executable_body(text, method_name)
        assert "sums + 1e-8" not in body, (
            f"{method_name} executable body must not contain the original "
            "no-op epsilon-division pattern (R4-15)"
        )
        assert "sku_cat_sum + 1e-8" not in body, (
            f"{method_name} executable body must not contain the no-op pattern"
        )
        # And no `df[fc] =` assignment in the body (the no-op shape
        # always assigned the no-op result back).
        assert "df[fc]" not in body or "raise" in body, (
            f"{method_name} executable body must not mutate df[fc] without raising"
        )


def test_hierarchical_module_docstring_references_r4_15():
    """The module docstring must call out R4-15 so future readers
    learn the history without diving into git blame."""
    text = (_BACKEND / "src" / "models" / "hierarchical.py").read_text()
    # First triple-quote block is the module docstring.
    first_q = text.find('"""')
    second_q = text.find('"""', first_q + 3)
    module_doc = text[first_q + 3:second_q]
    assert "R4-15" in module_doc, (
        "module docstring must reference R4-15 for traceability"
    )


# ── Functional: NotImplementedError raised ──────────────────────────────

def test_reconcile_top_down_raises():
    """Calling reconcile() with method='top_down' must raise
    NotImplementedError end-to-end."""
    from src.models.hierarchical import HierarchicalReconciler, HierarchyConfig
    lookup = pd.DataFrame({
        "sku":      ["SKU_001", "SKU_002"],
        "category": ["A",       "B"],
        "region":   ["N",       "S"],
    })
    forecasts = pd.DataFrame([
        {"sku": "SKU_001", "date": "2026-01-01", "predicted_sales": 10.0, "step": 1},
        {"sku": "SKU_002", "date": "2026-01-01", "predicted_sales": 20.0, "step": 1},
    ])
    rec = HierarchicalReconciler(HierarchyConfig(), method="top_down")
    rec.fit(lookup)
    with pytest.raises(NotImplementedError, match="top_down"):
        rec.reconcile(forecasts)


def test_reconcile_middle_out_raises():
    """Same end-to-end check for middle_out."""
    from src.models.hierarchical import HierarchicalReconciler, HierarchyConfig
    lookup = pd.DataFrame({
        "sku":      ["SKU_001", "SKU_002"],
        "category": ["A",       "B"],
        "region":   ["N",       "S"],
    })
    forecasts = pd.DataFrame([
        {"sku": "SKU_001", "date": "2026-01-01", "predicted_sales": 10.0, "step": 1},
        {"sku": "SKU_002", "date": "2026-01-01", "predicted_sales": 20.0, "step": 1},
    ])
    rec = HierarchicalReconciler(HierarchyConfig(), method="middle_out")
    rec.fit(lookup)
    with pytest.raises(NotImplementedError, match="middle_out"):
        rec.reconcile(forecasts)


def test_bottom_up_remains_working():
    """The fix must NOT regress bottom_up — it's the only correct
    method and is what production should use."""
    from src.models.hierarchical import HierarchicalReconciler, HierarchyConfig
    lookup = pd.DataFrame({
        "sku":      ["SKU_001", "SKU_002"],
        "category": ["A",       "A"],
        "region":   ["N",       "N"],
    })
    forecasts = pd.DataFrame([
        {"sku": "SKU_001", "date": "2026-01-01", "predicted_sales": 10.0, "step": 1},
        {"sku": "SKU_002", "date": "2026-01-01", "predicted_sales": 15.0, "step": 1},
    ])
    rec = HierarchicalReconciler(HierarchyConfig(), method="bottom_up")
    rec.fit(lookup)
    result = rec.reconcile(forecasts)

    # Category A total must equal 25 (10 + 15).
    cat_a = result[
        (result["hierarchy_level"] == "category")
        & (result["hierarchy_id"] == "A")
        & (result["step"] == 1)
    ]
    assert len(cat_a) == 1
    assert cat_a["predicted_sales"].values[0] == pytest.approx(25.0)
