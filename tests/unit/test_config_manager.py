"""
tests/unit/test_config_manager.py

Tests for ClientConfigManager: deep_merge, validation,
CRUD via registry, file-based CRUD, diff, patch.
"""
from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from src.clients.config_manager import (
    ClientConfigManager,
    ConfigValidationError,
    deep_merge,
    validate_client_config,
    _has_nested,
    _set_nested,
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

    def test_valid_ru_currencies(self):
        cfg = {"features": {"external_regressors_ru": {"currencies": ["CNY", "USD"]}}}
        assert validate_client_config(cfg) == []

    def test_unsupported_ru_currency_rejected(self):
        cfg = {"features": {"external_regressors_ru": {"currencies": ["CNY", "JPY"]}}}
        errors = validate_client_config(cfg)
        assert any("currencies" in e and "JPY" in e for e in errors)

    def test_empty_ru_currencies_rejected(self):
        # empty list is a footgun (disable via .enabled instead); reject it
        cfg = {"features": {"external_regressors_ru": {"currencies": []}}}
        errors = validate_client_config(cfg)
        assert any("currencies" in e for e in errors)

    def test_ru_currencies_must_be_list_of_str(self):
        cfg = {"features": {"external_regressors_ru": {"currencies": "CNY"}}}
        errors = validate_client_config(cfg)
        assert any("currencies" in e for e in errors)

    def test_learning_rate_out_of_range(self):
        errors = validate_client_config({"model": {"learning_rate": 0.99}})
        assert any("learning_rate" in e for e in errors)

    def test_multiple_errors_reported(self):
        cfg = {"model": {"horizon": 999, "type": "bad_type"}}
        errors = validate_client_config(cfg)
        assert len(errors) >= 2


# ══════════════════════════════════════════════════════════════
# Forbidden server-managed keys (R13-1)
# ══════════════════════════════════════════════════════════════

class TestForbiddenServerManagedKeys:
    """Server-managed keys (filesystem paths, infra namespaces) must be
    rejected for EVERY plan — including Business (config_allowed_keys=None),
    whose key-whitelist bypass let an arbitrary FS-path write through before
    (R13-1: clobber another tenant's model.pkl / fill the disk)."""

    @pytest.mark.parametrize("cfg, needle", [
        # the two real FS-path sinks
        ({"features": {"weather": {"cache_path": "/srv/data/victim/model.pkl"}}}, "cache_path"),
        ({"features": {"external_regressors_ru": {"cache_dir": "/srv/data/victim"}}}, "cache_dir"),
        # operator-owned infrastructure namespaces
        ({"mlflow": {"tracking_uri": "http://attacker:5000"}}, "mlflow.tracking_uri"),
        ({"api": {"host": "0.0.0.0"}}, "api.host"),
        ({"api": {"rate_limiting": {"enabled": False}}}, "api.rate_limiting.enabled"),
        # fail-closed for a future path key via the *_path / *_dir pattern
        ({"model": {"snapshot_dir": "/tmp/x"}}, "model.snapshot_dir"),
        ({"features": {"weather": {"backup_path": "/tmp/y"}}}, "features.weather.backup_path"),
    ])
    def test_forbidden_key_rejected(self, cfg, needle):
        errors = validate_client_config(cfg)
        assert any(needle in e for e in errors), errors

    def test_forbidden_on_key_not_value(self):
        # cache_path holds a perfectly valid string, yet is still rejected —
        # the classification is on the KEY, which value-only checks missed.
        errors = validate_client_config(
            {"features": {"weather": {"cache_path": "artifacts/ok.parquet"}}}
        )
        assert any("cache_path" in e for e in errors)

    def test_legit_business_knobs_still_pass(self):
        # No legitimate tunable key ends in _path/_dir or sits under mlflow/api;
        # the normal business knobs (incl. Business-only ML hyperparams) stay allowed.
        cfg = {
            "model": {"horizon": 28, "n_estimators": 800, "learning_rate": 0.05,
                      "tweedie_variance_power": 1.4},
            "features": {"weather": {"enabled": True, "latitude": 59.93, "longitude": 30.32},
                         "external_regressors_ru": {"enabled": True, "currencies": ["CNY"]}},
        }
        assert validate_client_config(cfg) == []

    def test_forbidden_via_manager_set_raises(self, mgr, registry):
        # the Business attack path: set() → validate_client_config → 422 in the router
        with pytest.raises(ConfigValidationError, match="cache_path"):
            mgr.set("acme", {"features": {"weather": {"cache_path": "/srv/x"}}}, registry)

    def test_forbidden_via_manager_patch_raises(self, mgr, registry):
        with pytest.raises(ConfigValidationError, match="cache_dir"):
            mgr.patch("acme", "features.external_regressors_ru.cache_dir", "/srv/x", registry)

    def test_forbidden_via_save_to_file_raises(self, mgr, tmp_path):
        with pytest.raises(ConfigValidationError, match="mlflow"):
            mgr.save_to_file("acme", {"mlflow": {"tracking_uri": "http://x"}},
                             directory=str(tmp_path))


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


# ── #96: dotted-path helpers + plan-default gating semantics ──────────────

class TestDottedHelpers:
    """_has_nested / _set_nested back the generalized plan-default gating
    in train.py (#96), replacing the hand-listed user_set_* booleans."""

    def test_has_nested_present_and_absent(self):
        d = {"model": {"objective": "tweedie"}, "hpo": {"n_trials": 20}}
        assert _has_nested(d, "model.objective") is True
        assert _has_nested(d, "hpo") is True
        assert _has_nested(d, "hpo.n_trials") is True
        assert _has_nested(d, "features.external_regressors_ru") is False
        assert _has_nested(d, "model.horizon") is False

    def test_has_nested_true_for_falsy_leaf(self):
        # The whole point of #96: presence of the KEY suppresses the plan
        # default, even when the client set it to a falsy value (0/False/None).
        d = {"hpo": {"enabled": False, "n_trials": 0}, "model": {"objective": None}}
        assert _has_nested(d, "hpo.enabled") is True
        assert _has_nested(d, "hpo.n_trials") is True
        assert _has_nested(d, "model.objective") is True

    def test_has_nested_non_dict_midpath(self):
        d = {"model": "not-a-dict"}
        assert _has_nested(d, "model.objective") is False

    def test_set_nested_creates_intermediate(self):
        d: dict = {}
        _set_nested(d, "features.external_regressors_ru.enabled", True)
        assert d == {"features": {"external_regressors_ru": {"enabled": True}}}

    def test_set_nested_preserves_siblings(self):
        d = {"model": {"horizon": 14}}
        _set_nested(d, "model.objective", "ensemble")
        assert d == {"model": {"horizon": 14, "objective": "ensemble"}}

    def test_set_nested_overwrites_non_dict_midpath(self):
        d = {"hpo": 5}                       # leaf where we need to descend
        _set_nested(d, "hpo.n_trials", 30)
        assert d == {"hpo": {"n_trials": 30}}

    def test_gating_loop_does_not_clobber_explicit_choice(self):
        """End-to-end of the #96 invariant: a default is applied only when
        the gate key is ABSENT from the client override, regardless of how
        many defaults share a gate. Mirrors train.py's plan_defaults loop."""
        client_cfg = {"model": {"objective": "tweedie"}}   # user set objective
        config = {"model": {"objective": "tweedie", "horizon": 14}}
        plan_defaults = [
            ("hpo",             "hpo.enabled",     True),
            ("hpo",             "hpo.n_trials",    15),
            ("model.objective", "model.objective", "ensemble"),  # must NOT win
        ]
        for gate, target, value in plan_defaults:
            if not _has_nested(client_cfg, gate):
                _set_nested(config, target, value)

        assert config["model"]["objective"] == "tweedie"   # user choice survives
        assert config["hpo"] == {"enabled": True, "n_trials": 15}  # gate absent → defaulted


def test_train_py_uses_declarative_plan_defaults_not_user_set_flags():
    """#96 anti-regression: train.py must drive plan-tier defaults off the
    actual client override (_has_nested gate) and NOT reintroduce the fragile
    hand-listed `user_set_*` booleans that silently clobbered explicit user
    values when a new default's flag was forgotten."""
    train_py = (Path(__file__).resolve().parents[2]
                / "src" / "pipeline" / "train.py").read_text()
    assert "user_set_" not in train_py, (
        "train.py reintroduced hand-listed user_set_* flags — use the "
        "declarative _has_nested gate instead (#96)"
    )
    assert "plan_defaults" in train_py and "_has_nested(client_cfg" in train_py, (
        "train.py must gate plan defaults via _has_nested on the client override"
    )
