"""
tests/unit/test_config_manager.py

Tests for ClientConfigManager: deep_merge, validation,
CRUD via registry, file-based CRUD, diff, patch.
"""
from __future__ import annotations

import copy
import tempfile
from pathlib import Path

import pytest
import yaml

from src.clients.config_manager import (
    ClientConfigManager,
    ConfigValidationError,
    deep_merge,
    validate_client_config,
    _compute_diff,
)
from src.clients.registry import ClientRecord, LocalFileRegistry


# ── fixtures ──────────────────────────────────────────────────

@pytest.fixture
def system_config(tmp_path) -> Path:
    cfg = {
        "data": {"date_col": "date", "sku_col": "sku", "target_col": "sales"},
        "model": {"type": "mimo", "horizon": 14, "n_estimators": 500,
                  "learning_rate": 0.05, "num_leaves": 64},
        "features": {
            "lags": [1, 7, 14],
            "weather": {"enabled": False, "latitude": 55.75, "longitude": 37.62},
            "holidays": {"enabled": True, "country": "RU"},
        },
        "cold_start": {"min_history_days": 28, "n_neighbors": 5},
        "validation": {"type": "walk_forward", "n_splits": 3},
    }
    p = tmp_path / "config.yaml"
    p.write_text(yaml.dump(cfg))
    return p


@pytest.fixture
def registry(tmp_path) -> LocalFileRegistry:
    reg = LocalFileRegistry(str(tmp_path / "reg.json"))
    reg.register(ClientRecord("acme",  {}, "s3://b/acme/"))
    reg.register(ClientRecord("omega", {}, "s3://b/omega/"))
    return reg


@pytest.fixture
def mgr(system_config) -> ClientConfigManager:
    return ClientConfigManager(str(system_config))


# ══════════════════════════════════════════════════════════════
# deep_merge
# ══════════════════════════════════════════════════════════════

class TestDeepMerge:

    def test_scalar_override(self):
        base     = {"model": {"horizon": 14, "type": "lgbm"}}
        override = {"model": {"horizon": 28}}
        result   = deep_merge(base, override)
        assert result["model"]["horizon"] == 28
        assert result["model"]["type"]    == "lgbm"   # preserved

    def test_nested_merge(self):
        base     = {"features": {"weather": {"enabled": False, "latitude": 55.75}}}
        override = {"features": {"weather": {"enabled": True}}}
        result   = deep_merge(base, override)
        assert result["features"]["weather"]["enabled"]  is True
        assert result["features"]["weather"]["latitude"] == 55.75  # preserved

    def test_list_replacement(self):
        """Lists are replaced, not merged."""
        base     = {"features": {"lags": [1, 7, 14, 28]}}
        override = {"features": {"lags": [1, 7]}}
        result   = deep_merge(base, override)
        assert result["features"]["lags"] == [1, 7]

    def test_new_key_added(self):
        base     = {"model": {"horizon": 14}}
        override = {"api": {"max_latency_ms": 100}}
        result   = deep_merge(base, override)
        assert result["api"]["max_latency_ms"] == 100
        assert result["model"]["horizon"]      == 14

    def test_base_not_mutated(self):
        base     = {"model": {"horizon": 14}}
        override = {"model": {"horizon": 28}}
        original = copy.deepcopy(base)
        deep_merge(base, override)
        assert base == original

    def test_override_not_mutated(self):
        base     = {"model": {"horizon": 14}}
        override = {"model": {"horizon": 28, "type": "mimo"}}
        original = copy.deepcopy(override)
        deep_merge(base, override)
        assert override == original

    def test_empty_override_returns_base_copy(self):
        base   = {"model": {"horizon": 14}}
        result = deep_merge(base, {})
        assert result == base
        assert result is not base

    def test_empty_base(self):
        override = {"model": {"horizon": 28}}
        result   = deep_merge({}, override)
        assert result == override


# ══════════════════════════════════════════════════════════════
# validate_client_config
# ══════════════════════════════════════════════════════════════

class TestValidation:

    def test_valid_empty_config(self):
        assert validate_client_config({}) == []

    def test_valid_horizon(self):
        assert validate_client_config({"model": {"horizon": 28}}) == []

    def test_horizon_out_of_range(self):
        errors = validate_client_config({"model": {"horizon": 0}})
        assert any("horizon" in e for e in errors)

    def test_horizon_too_large(self):
        errors = validate_client_config({"model": {"horizon": 999}})
        assert any("horizon" in e for e in errors)

    def test_invalid_model_type(self):
        errors = validate_client_config({"model": {"type": "xgboost_v2"}})
        assert any("model.type" in e for e in errors)

    def test_valid_model_types(self):
        for mt in ("lgbm", "mimo"):
            assert validate_client_config({"model": {"type": mt}}) == []

    def test_invalid_country(self):
        errors = validate_client_config({"features": {"holidays": {"country": "XX"}}})
        assert any("country" in e for e in errors)

    def test_valid_country(self):
        assert validate_client_config({"features": {"holidays": {"country": "RU"}}}) == []

    def test_invalid_latitude(self):
        errors = validate_client_config({"features": {"weather": {"latitude": 999}}})
        assert any("latitude" in e for e in errors)

    def test_invalid_longitude(self):
        errors = validate_client_config({"features": {"weather": {"longitude": -999}}})
        assert any("longitude" in e for e in errors)

    def test_valid_lat_lon(self):
        cfg = {"features": {"weather": {"latitude": 59.93, "longitude": 30.32}}}
        assert validate_client_config(cfg) == []

    def test_unsorted_quantiles(self):
        errors = validate_client_config({"model": {"quantiles": [0.9, 0.5, 0.1]}})
        assert any("quantile" in e for e in errors)

    def test_quantile_out_of_range(self):
        errors = validate_client_config({"model": {"quantiles": [0.0, 0.5, 1.0]}})
        assert any("quantile" in e for e in errors)

    def test_valid_quantiles(self):
        assert validate_client_config({"model": {"quantiles": [0.1, 0.5, 0.9]}}) == []

    def test_learning_rate_out_of_range(self):
        errors = validate_client_config({"model": {"learning_rate": 0.99}})
        assert any("learning_rate" in e for e in errors)

    def test_multiple_errors_reported(self):
        cfg = {"model": {"horizon": 999, "type": "bad_type"}}
        errors = validate_client_config(cfg)
        assert len(errors) >= 2


# ══════════════════════════════════════════════════════════════
# ClientConfigManager — registry-based
# ══════════════════════════════════════════════════════════════

class TestClientConfigManagerRegistry:

    def test_get_effective_no_override(self, mgr, registry):
        """Client with no override → pure system defaults."""
        effective = mgr.get_effective("acme", registry)
        assert effective["model"]["horizon"] == 14
        assert effective["model"]["type"]    == "mimo"

    def test_set_override_merges_correctly(self, mgr, registry):
        override = {"model": {"horizon": 28}}
        effective = mgr.set("acme", override, registry)
        assert effective["model"]["horizon"]      == 28      # from client
        assert effective["model"]["type"]         == "mimo"  # from system
        assert effective["model"]["n_estimators"] == 500     # from system

    def test_override_stored_in_registry(self, mgr, registry):
        mgr.set("acme", {"model": {"horizon": 28}}, registry)
        stored = mgr.get_override("acme", registry)
        assert stored == {"model": {"horizon": 28}}

    def test_patch_single_key(self, mgr, registry):
        mgr.patch("acme", "model.horizon", 7, registry)
        eff = mgr.get_effective("acme", registry)
        assert eff["model"]["horizon"] == 7
        # Other model keys preserved from system
        assert eff["model"]["n_estimators"] == 500

    def test_patch_nested_key(self, mgr, registry):
        mgr.patch("acme", "features.weather.enabled", True, registry)
        eff = mgr.get_effective("acme", registry)
        assert eff["features"]["weather"]["enabled"]  is True
        assert eff["features"]["weather"]["latitude"] == 55.75  # system default

    def test_patch_preserves_other_overrides(self, mgr, registry):
        mgr.set("acme", {"model": {"horizon": 28}}, registry)
        mgr.patch("acme", "model.n_estimators", 200, registry)
        eff = mgr.get_effective("acme", registry)
        assert eff["model"]["horizon"]      == 28   # still there
        assert eff["model"]["n_estimators"] == 200  # patched

    def test_reset_removes_overrides(self, mgr, registry):
        mgr.set("acme", {"model": {"horizon": 28}}, registry)
        mgr.reset("acme", registry)
        eff = mgr.get_effective("acme", registry)
        assert eff["model"]["horizon"] == 14  # back to system default

    def test_different_clients_isolated(self, mgr, registry):
        mgr.set("acme",  {"model": {"horizon": 28}}, registry)
        mgr.set("omega", {"model": {"horizon": 7}},  registry)
        eff_acme  = mgr.get_effective("acme",  registry)
        eff_omega = mgr.get_effective("omega", registry)
        assert eff_acme["model"]["horizon"]  == 28
        assert eff_omega["model"]["horizon"] == 7

    def test_invalid_override_raises(self, mgr, registry):
        with pytest.raises(ConfigValidationError):
            mgr.set("acme", {"model": {"horizon": 999}}, registry)

    def test_unknown_client_raises(self, mgr, registry):
        with pytest.raises(ValueError, match="not registered"):
            mgr.set("unknown_client", {"model": {"horizon": 7}}, registry)

    def test_diff_shows_changed_keys(self, mgr, registry):
        mgr.set("acme", {"model": {"horizon": 28}}, registry)
        diff = mgr.diff("acme", registry)
        assert "model.horizon" in diff
        assert diff["model.horizon"]["system"] == 14
        assert diff["model.horizon"]["client"] == 28

    def test_diff_empty_when_no_overrides(self, mgr, registry):
        diff = mgr.diff("acme", registry)
        assert diff == {}

    def test_get_effective_without_registry(self, mgr):
        """No registry → returns system defaults."""
        eff = mgr.get_effective("any_client", registry=None)
        assert eff["model"]["horizon"] == 14


# ══════════════════════════════════════════════════════════════
# ClientConfigManager — file-based
# ══════════════════════════════════════════════════════════════

class TestClientConfigManagerFiles:

    def test_save_and_load_file(self, mgr, tmp_path):
        override = {"model": {"horizon": 28}, "features": {"holidays": {"country": "US"}}}
        path = mgr.save_to_file("acme", override, directory=str(tmp_path))
        assert path.exists()
        loaded = mgr.load_from_file("acme", directory=str(tmp_path))
        assert loaded == override

    def test_save_validates_config(self, mgr, tmp_path):
        with pytest.raises(ConfigValidationError):
            mgr.save_to_file("acme", {"model": {"horizon": 0}}, directory=str(tmp_path))

    def test_load_missing_returns_empty(self, mgr, tmp_path):
        result = mgr.load_from_file("nonexistent", directory=str(tmp_path))
        assert result == {}

    def test_get_effective_from_file(self, mgr, tmp_path):
        override = {"model": {"horizon": 7}}
        mgr.save_to_file("acme", override, directory=str(tmp_path))
        eff = mgr.get_effective_from_file("acme", directory=str(tmp_path))
        assert eff["model"]["horizon"]      == 7    # from file
        assert eff["model"]["n_estimators"] == 500  # from system

    def test_list_client_configs(self, mgr, tmp_path):
        mgr.save_to_file("acme",  {"model": {"horizon": 7}},  directory=str(tmp_path))
        mgr.save_to_file("omega", {"model": {"horizon": 14}}, directory=str(tmp_path))
        clients = mgr.list_client_configs(directory=str(tmp_path))
        assert "acme"  in clients
        assert "omega" in clients

    def test_file_is_valid_yaml(self, mgr, tmp_path):
        mgr.save_to_file("acme", {"model": {"horizon": 28}}, directory=str(tmp_path))
        raw = (tmp_path / "acme.yaml").read_text()
        parsed = yaml.safe_load(raw)
        assert parsed["model"]["horizon"] == 28


# ══════════════════════════════════════════════════════════════
# Multi-client isolation scenario
# ══════════════════════════════════════════════════════════════

class TestMultiClientIsolation:

    def test_three_clients_all_different(self, mgr, registry):
        """Simulate 3 concurrent clients with different configs."""
        registry.register(ClientRecord("client_c", {}, "s3://b/c/"))

        mgr.set("acme",     {"model": {"horizon": 28, "type": "mimo"}}, registry)
        mgr.set("omega",    {"model": {"horizon": 7,  "type": "lgbm"}}, registry)
        mgr.set("client_c", {"features": {"holidays": {"country": "US"}}}, registry)

        eff_a = mgr.get_effective("acme",     registry)
        eff_b = mgr.get_effective("omega",    registry)
        eff_c = mgr.get_effective("client_c", registry)

        # Each gets its own settings
        assert eff_a["model"]["horizon"] == 28
        assert eff_b["model"]["horizon"] == 7
        assert eff_c["model"]["horizon"] == 14  # system default

        # Country isolation
        assert eff_a["features"]["holidays"]["country"] == "RU"  # system
        assert eff_c["features"]["holidays"]["country"] == "US"  # overridden

        # Models don't bleed between clients
        assert eff_a["model"]["type"] == "mimo"
        assert eff_b["model"]["type"] == "lgbm"

    def test_patch_one_client_doesnt_affect_others(self, mgr, registry):
        mgr.set("acme",  {"model": {"horizon": 14}}, registry)
        mgr.set("omega", {"model": {"horizon": 14}}, registry)

        mgr.patch("acme", "model.horizon", 28, registry)

        eff_a = mgr.get_effective("acme",  registry)
        eff_b = mgr.get_effective("omega", registry)

        assert eff_a["model"]["horizon"] == 28   # changed
        assert eff_b["model"]["horizon"] == 14   # unchanged

    def test_reset_one_doesnt_affect_others(self, mgr, registry):
        mgr.set("acme",  {"model": {"horizon": 28}}, registry)
        mgr.set("omega", {"model": {"horizon": 7}},  registry)

        mgr.reset("acme", registry)

        eff_a = mgr.get_effective("acme",  registry)
        eff_b = mgr.get_effective("omega", registry)

        assert eff_a["model"]["horizon"] == 14  # reset to system
        assert eff_b["model"]["horizon"] == 7   # still overridden
