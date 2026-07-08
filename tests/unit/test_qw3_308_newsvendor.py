"""
QW3-2 (#308): service-level order-quantity helper + config validation.

The value (quantile beats mean on newsvendor cost) is proven on the stand
(ab_newsvendor_1c.py: −4…−35% at Cu:Co ≥ 2:1). Here we lock the production
wiring: the interpolation is monotone, clamped, and None-safe; and the
service-level override is range-validated.
"""
from __future__ import annotations

from src.models.newsvendor import order_quantity


def test_interpolates_between_p10_p90():
    # τ=0.7 over [8,12]: 8 + (0.7-0.1)/0.8 · 4 = 8 + 0.75·4 = 11.0
    assert order_quantity(8.0, 12.0, 0.7) == 11.0


def test_median_is_midpoint():
    # τ=0.5 → halfway: 8 + 0.5·4 = 10.0
    assert order_quantity(8.0, 12.0, 0.5) == 10.0


def test_monotone_in_service_level():
    prev = -1.0
    for tau in (0.5, 0.6, 0.7, 0.8, 0.9):
        q = order_quantity(8.0, 12.0, tau)
        assert q >= prev, f"not monotone at τ={tau}"
        prev = q


def test_clamped_outside_grid():
    assert order_quantity(8.0, 12.0, 0.05) == 8.0    # ≤ p10-level → p10
    assert order_quantity(8.0, 12.0, 0.99) == 12.0   # ≥ p90-level → p90


def test_none_band_gives_no_recommendation():
    assert order_quantity(None, 12.0, 0.7) is None
    assert order_quantity(8.0, None, 0.7) is None


def test_swapped_band_is_reordered():
    # defensive: p10 > p90 shouldn't produce a backward result
    assert order_quantity(12.0, 8.0, 0.7) == 11.0


def test_service_level_override_range_validated():
    from src.clients.config_manager import validate_client_config
    assert validate_client_config({"model": {"service_level": 0.7}}) == []
    errs = validate_client_config({"model": {"service_level": 1.5}})
    assert any("service_level" in e for e in errs)
    errs_lo = validate_client_config({"model": {"service_level": 0.3}})
    assert any("service_level" in e for e in errs_lo)


def test_service_level_is_a_paid_override_key():
    from src.plans.plans import _START_CONFIG_KEYS
    assert "model.service_level" in _START_CONFIG_KEYS
