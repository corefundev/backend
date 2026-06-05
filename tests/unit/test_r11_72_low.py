"""
tests/unit/test_r11_72_low.py

R11-#72 — LOW-severity batch. One guard per shipped fix.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_BACKEND = Path(__file__).resolve().parents[2]


# ── L16: mase_global = mean of per-SKU MASE, not ratio-of-means ───────────

def test_mase_global_averages_per_sku_ratios():
    from src.validation.metrics import aggregate_metrics
    raw_df = pd.DataFrame({
        "sku":          ["A", "B"],
        "actual":       [100.0, 10.0],
        "predicted":    [110.0, 16.0],
        # per-SKU training series → naive error = mean|diff|
        "train_values": [np.array([10., 20., 10., 20.]),   # naive = 10, mae = 10 → MASE 1.0
                         np.array([1., 2., 3., 4.])],        # naive = 1,  mae = 6  → MASE 6.0
    })
    metrics_df = pd.DataFrame({
        "mase": [1.0, 6.0], "wmape": [0.1, 0.2], "smape": [0.1, 0.2],
    })
    agg = aggregate_metrics(metrics_df, raw_df=raw_df)
    # Correct (per-SKU-averaged): mean([1.0, 6.0]) = 3.5.
    # The old biased ratio-of-means would give mean([10,6])/mean([10,1]) ≈ 1.45.
    assert agg["mase_global"] == pytest.approx(3.5)


# ── L17: /admin/legal body is a bounded pydantic model ────────────────────

def test_legal_doc_update_rejects_oversized_content():
    from pydantic import ValidationError
    from src.api.routers.legal import LegalDocUpdate
    LegalDocUpdate(title="T", content="ok")            # valid
    with pytest.raises(ValidationError):
        LegalDocUpdate(title="T", content="x" * 1_000_001)
    with pytest.raises(ValidationError):
        LegalDocUpdate(title="", content="ok")          # min_length


# ── L14: _parse_origins deduped into one shared helper ────────────────────

def test_parse_frontend_origins_helper(monkeypatch):
    from src.api._origins import parse_frontend_origins
    monkeypatch.delenv("FRONTEND_ORIGINS", raising=False)
    assert parse_frontend_origins() == ["http://localhost:5173"]
    monkeypatch.setenv("FRONTEND_ORIGINS", "https://a.com, https://b.com")
    assert parse_frontend_origins() == ["https://a.com", "https://b.com"]


def test_no_duplicate_parse_origins_definition():
    for rel in ("src/api/main.py", "src/api/routers/auth.py"):
        text = (_BACKEND / rel).read_text(encoding="utf-8")
        assert "def _parse_origins" not in text, f"{rel} still defines a local _parse_origins (R11-L14)"
        assert "parse_frontend_origins" in text, f"{rel} must use the shared helper"


# ── L1: OAuth token delivered in the URL fragment, not the query ──────────

def test_oauth_redirect_uses_fragment_not_query():
    text = (_BACKEND / "src" / "api" / "routers" / "auth.py").read_text(encoding="utf-8")
    assert 'f"{return_url}#{_up.urlencode(qs)}"' in text, (
        "OAuth redirect must deliver token/api_key in the URL fragment (R11-L1)"
    )
    assert 'f"{return_url}?{_up.urlencode(qs)}"' not in text, (
        "OAuth redirect must NOT put token/api_key in the query string (R11-L1)"
    )


# ── L6: audit chain verifier uses constant-time compare ───────────────────

def test_audit_verify_uses_compare_digest():
    text = (_BACKEND / "src" / "audit" / "log.py").read_text(encoding="utf-8")
    assert "hmac.compare_digest(str(actual_prev)" in text
    assert "hmac.compare_digest(str(expected_sig)" in text
    assert "if expected_sig != r[" not in text, "bare != on signature still present (R11-L6)"
