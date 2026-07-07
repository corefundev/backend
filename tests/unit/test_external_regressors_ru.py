"""
tests/unit/test_external_regressors_ru.py

Mocked-HTTP coverage for the ЦБ РФ FX fetcher: parser correctness on a
sample XML response, cache hit/refresh semantics, safe-fallback to
stale cache on network failure, and config-toggled feature merging.
The actual HTTP layer is patched out — no real cbr.ru traffic.
"""
from __future__ import annotations

import os
from datetime import date
from unittest import mock

import pandas as pd
import pytest

from src.features import external_regressors_ru as mod


SAMPLE_CBR_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<ValCurs ID="R01375" DateRange1="01.01.2024" DateRange2="03.01.2024" name="Foreign Currency Market Dynamic">
  <Record Date="01.01.2024" Id="R01375">
    <Nominal>10</Nominal>
    <Value>132,4500</Value>
  </Record>
  <Record Date="02.01.2024" Id="R01375">
    <Nominal>10</Nominal>
    <Value>133,1100</Value>
  </Record>
  <Record Date="03.01.2024" Id="R01375">
    <Nominal>10</Nominal>
    <Value>134,0000</Value>
  </Record>
</ValCurs>"""


def _fake_urlopen(xml_bytes: bytes):
    """Return a context-manager-compatible fake urlopen response."""
    class _Resp:
        def __enter__(self_): return self_
        def __exit__(self_, *a): return False
        def read(self_): return xml_bytes
    return _Resp()


# ─── XML parsing ─────────────────────────────────────────────────
def test_fetch_cbr_xml_parses_three_rows_with_nominal_division():
    with mock.patch.object(mod, "urlopen", return_value=_fake_urlopen(SAMPLE_CBR_XML)):
        df = mod._fetch_cbr_xml("R01375", date(2024, 1, 1), date(2024, 1, 3))
    assert list(df.columns) == ["date", "rate_rub"]
    assert len(df) == 3
    # 132.45 / 10 == 13.245 (nominal=10 for CNY pre-renomination basis)
    assert df.iloc[0]["rate_rub"] == pytest.approx(13.245)
    assert df.iloc[1]["rate_rub"] == pytest.approx(13.311)
    assert df.iloc[2]["rate_rub"] == pytest.approx(13.400)
    # Dates parsed as Timestamps and sorted ascending
    assert df["date"].is_monotonic_increasing


def test_fetch_cbr_xml_empty_response_raises_valueerror():
    empty_xml = b'<?xml version="1.0"?><ValCurs></ValCurs>'
    with mock.patch.object(mod, "urlopen", return_value=_fake_urlopen(empty_xml)):
        with pytest.raises(ValueError, match="empty series"):
            mod._fetch_cbr_xml("R01375", date(2024, 1, 1), date(2024, 1, 3))


# ─── Cache behaviour ─────────────────────────────────────────────
def test_safe_fetch_caches_then_reuses(tmp_path):
    call_count = {"n": 0}

    def fetcher():
        call_count["n"] += 1
        return pd.DataFrame({"date": [pd.Timestamp("2024-01-01")], "rate_rub": [13.0]})

    # First call: cache miss → fetcher runs
    out1 = mod._safe_fetch("cbr_cny", fetcher, str(tmp_path))
    assert out1 is not None and call_count["n"] == 1
    assert os.path.exists(tmp_path / "cbr_cny.parquet")

    # Second call: cache fresh → fetcher does NOT run
    out2 = mod._safe_fetch("cbr_cny", fetcher, str(tmp_path), cache_ttl_hours=24)
    assert out2 is not None and call_count["n"] == 1
    pd.testing.assert_frame_equal(out1.reset_index(drop=True), out2.reset_index(drop=True))


def test_safe_fetch_stale_cache_served_when_fetcher_fails(tmp_path):
    # Seed a parquet with a known value, then force its mtime old, then
    # make fetcher raise — we expect the stale parquet back, not None.
    seed = pd.DataFrame({"date": [pd.Timestamp("2023-12-31")], "rate_rub": [12.5]})
    seed_path = tmp_path / "cbr_eur.parquet"
    seed.to_parquet(seed_path, index=False)
    # Force expired
    old = pd.Timestamp.now().timestamp() - 99 * 3600
    os.utime(seed_path, (old, old))

    def boom():
        raise OSError("network down")

    out = mod._safe_fetch("cbr_eur", boom, str(tmp_path), cache_ttl_hours=24)
    assert out is not None
    assert out.iloc[0]["rate_rub"] == 12.5


def test_safe_fetch_returns_none_when_no_cache_and_fetcher_fails(tmp_path):
    def boom():
        raise OSError("network down")

    out = mod._safe_fetch("cbr_kzt", boom, str(tmp_path))
    assert out is None


# ─── End-to-end merge ────────────────────────────────────────────
def test_build_ru_regressor_features_disabled_passthrough():
    df = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=3)})
    config = {"features": {"external_regressors_ru": {"enabled": False}}}
    out = mod.build_ru_regressor_features(df, config, date_col="date")
    pd.testing.assert_frame_equal(out, df)


def test_build_ru_regressor_features_adds_fx_columns(tmp_path):
    df = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=5)})
    config = {
        "features": {
            "external_regressors_ru": {
                "enabled": True,
                "currencies": ["CNY"],
                "cache_dir": str(tmp_path),
                "cache_ttl_hours": 24,
            }
        }
    }
    with mock.patch.object(mod, "urlopen", return_value=_fake_urlopen(SAMPLE_CBR_XML)):
        out = mod.build_ru_regressor_features(df, config, date_col="date")
    assert "cny_rub_lag_1" in out.columns
    assert "cny_rub_change" in out.columns
    assert "cny_rub_rolling_7" in out.columns
    # 5 rows kept, all FX columns non-null after ffill/bfill
    assert len(out) == 5
    assert out["cny_rub_lag_1"].notna().all()
    assert out["cny_rub_change"].notna().all()
    assert out["cny_rub_rolling_7"].notna().all()


def test_unknown_currency_skipped(tmp_path):
    df = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=3)})
    config = {
        "features": {
            "external_regressors_ru": {
                "enabled": True,
                "currencies": ["NEVERLAND"],
                "cache_dir": str(tmp_path),
                "cache_ttl_hours": 24,
            }
        }
    }
    out = mod.build_ru_regressor_features(df, config, date_col="date")
    # No FX columns added when no currency is resolvable
    assert "neverland_rub_lag_1" not in out.columns
    # Original frame preserved
    assert len(out) == 3
