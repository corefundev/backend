"""
src/clients/config_manager.py

Per-client configuration management.

Architecture:
  ┌─────────────────────┐
  │  configs/config.yaml │  ← системный дефолт (не трогать)
  └──────────┬──────────┘
             │  deep merge
  ┌──────────▼──────────┐
  │  client config       │  ← только переопределения клиента
  │  (in registry DB     │    хранится в registry.config
  │   or YAML file)      │
  └──────────┬──────────┘
             │
  ┌──────────▼──────────┐
  │  effective config    │  ← то, что реально использует pipeline
  └─────────────────────┘

Правила мерджа:
  - Клиентский конфиг может переопределить любой ключ
  - Системный конфиг — это дефолт, он всегда применяется первым
  - Клиент не может выйти за пределы допустимых значений (валидация)
  - Если у клиента нет своего конфига — используется системный дефолт

Пример:
  # Системный дефолт (configs/config.yaml):
  model:
    horizon: 14
    type: mimo
  features:
    weather:
      enabled: false

  # Конфиг клиента "acme" (только то, что отличается):
  model:
    horizon: 28          ← переопределяет горизонт
  features:
    weather:
      enabled: true      ← включает погоду только для этого клиента
      latitude: 59.93    ← Санкт-Петербург
      longitude: 30.32

  # Итоговый effective config для "acme":
  model:
    horizon: 28          ← из клиента
    type: mimo           ← из дефолта
  features:
    weather:
      enabled: true      ← из клиента
      latitude: 59.93    ← из клиента
      longitude: 30.32   ← из клиента
      cache_path: ...    ← из дефолта
"""
from __future__ import annotations

import copy
import logging
import os
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# ── Допустимые диапазоны значений для валидации ──────────────
_ALLOWED_RANGES: dict[str, tuple] = {
    "model.horizon":                   (1, 90),
    "model.n_estimators":              (50, 5000),
    "model.learning_rate":             (0.001, 0.5),
    "model.num_leaves":                (4, 512),
    "model.min_child_samples":         (1, 500),
    "model.feature_fraction":          (0.1, 1.0),
    "model.bagging_fraction":          (0.1, 1.0),
    "validation.n_splits":             (1, 10),
    "cold_start.min_history_days":     (7, 365),
    "cold_start.n_neighbors":          (1, 20),
    "anomaly_detection.contamination": (0.01, 0.3),
    "anomaly_detection.iqr_factor":    (1.0, 10.0),
    "hpo.n_trials":                    (1, 500),
    "hpo.timeout_sec":                 (30, 86400),
}

_ALLOWED_MODEL_TYPES = {"lgbm", "mimo"}
_ALLOWED_COUNTRIES = {
    "RU", "US", "DE", "FR", "CN", "GB", "JP", "BR",
    "IN", "IT", "ES", "PL", "NL", "SE", "NO", "FI",
    "DK", "AU", "CA", "KZ", "UA", "BY",
}


# ── Core merge logic ──────────────────────────────────────────

def deep_merge(base: dict, override: dict) -> dict:
    """
    Deep merge two dicts. Override wins on scalar conflicts.
    Lists are replaced (not concatenated).
    Neither input is mutated.
    """
    result = copy.deepcopy(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = deep_merge(result[key], val)
        else:
            result[key] = copy.deepcopy(val)
    return result


def _get_nested(d: dict, dotted_key: str) -> Any:
    keys = dotted_key.split(".")
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


# ── Validation ────────────────────────────────────────────────

class ConfigValidationError(ValueError):
    pass


def validate_client_config(client_override: dict) -> list[str]:
    """
    Validate a client config override dict.
    Returns list of error messages (empty = valid).
    Does NOT require all keys to be present — only validates what's there.
    """
    errors = []

    # Numeric range checks
    for dotted, (lo, hi) in _ALLOWED_RANGES.items():
        val = _get_nested(client_override, dotted)
        if val is not None:
            try:
                v = float(val)
                if not (lo <= v <= hi):
                    errors.append(f"{dotted}={val} must be in [{lo}, {hi}]")
            except (TypeError, ValueError):
                errors.append(f"{dotted}={val!r} must be a number")

    # model.type
    mt = _get_nested(client_override, "model.type")
    if mt is not None and mt not in _ALLOWED_MODEL_TYPES:
        errors.append(f"model.type={mt!r} must be one of {sorted(_ALLOWED_MODEL_TYPES)}")

    # features.holidays.country
    country = _get_nested(client_override, "features.holidays.country")
    if country is not None and country not in _ALLOWED_COUNTRIES:
        errors.append(
            f"features.holidays.country={country!r} not supported. "
            f"Supported: {sorted(_ALLOWED_COUNTRIES)}"
        )

    # features.weather lat/lon
    lat = _get_nested(client_override, "features.weather.latitude")
    lon = _get_nested(client_override, "features.weather.longitude")
    if lat is not None and not (-90 <= float(lat) <= 90):
        errors.append(f"features.weather.latitude={lat} must be in [-90, 90]")
    if lon is not None and not (-180 <= float(lon) <= 180):
        errors.append(f"features.weather.longitude={lon} must be in [-180, 180]")

    # quantiles must be sorted list of floats in (0,1)
    qs = _get_nested(client_override, "model.quantiles")
    if qs is not None:
        if not isinstance(qs, list) or not all(isinstance(q, (int, float)) for q in qs):
            errors.append("model.quantiles must be a list of floats")
        elif not all(0 < q < 1 for q in qs):
            errors.append("model.quantiles values must be between 0 and 1")
        elif qs != sorted(qs):
            errors.append("model.quantiles must be sorted ascending")

    return errors


# ── ClientConfigManager ───────────────────────────────────────

class ClientConfigManager:
    """
    Manages per-client configs on top of the system default.

    Usage:
        mgr = ClientConfigManager("configs/config.yaml")

        # Create client override
        mgr.set("acme", {"model": {"horizon": 28}, "features": {"weather": {"enabled": True}}})

        # Get merged effective config
        cfg = mgr.get_effective("acme")
        # cfg["model"]["horizon"] == 28
        # cfg["model"]["type"] == "mimo"  ← from system default

        # Update one key
        mgr.patch("acme", "model.horizon", 14)

        # Get only the client's overrides (not merged)
        override = mgr.get_override("acme")
    """

    def __init__(self, system_config_path: str = "configs/config.yaml"):
        self._system_path = Path(system_config_path)
        self._system: dict = {}
        self._reload_system()

    def _reload_system(self) -> None:
        if self._system_path.exists():
            with open(self._system_path) as f:
                self._system = yaml.safe_load(f) or {}
        else:
            logger.warning(f"System config not found: {self._system_path}")
            self._system = {}

    # ── Registry integration ──────────────────────────────────

    def get_effective(self, client_id: str, registry=None) -> dict:
        """
        Return merged (system + client) config for client_id.
        If registry is None — returns system default.
        """
        self._reload_system()
        base = copy.deepcopy(self._system)

        if registry is None:
            return base

        record = registry.get(client_id)
        if record is None:
            logger.debug(f"Client {client_id} not found — using system defaults")
            return base

        override = record.config or {}
        if not override:
            return base

        effective = deep_merge(base, override)
        logger.debug(f"Config for {client_id}: merged {len(override)} override keys")
        return effective

    # ── CRUD for client overrides ─────────────────────────────

    def set(
        self,
        client_id: str,
        override: dict,
        registry=None,
        validate: bool = True,
    ) -> dict:
        """
        Set (replace) the client override config.
        Returns the new effective config.
        Raises ConfigValidationError if override is invalid.
        """
        if validate:
            errors = validate_client_config(override)
            if errors:
                raise ConfigValidationError(
                    f"Config validation failed for '{client_id}':\n"
                    + "\n".join(f"  - {e}" for e in errors)
                )

        if registry is not None:
            record = registry.get(client_id)
            if record is None:
                raise ValueError(f"Client '{client_id}' not registered. Register first.")
            registry.update(client_id, config=override)
            logger.info(f"Config updated for client={client_id}: {list(override.keys())}")

        return self.get_effective(client_id, registry)

    def patch(
        self,
        client_id: str,
        dotted_key: str,
        value: Any,
        registry=None,
    ) -> dict:
        """
        Update a single key using dot notation.
        Example: patch("acme", "model.horizon", 28)
        Returns new effective config.
        """
        # Build nested dict from dotted key
        parts = dotted_key.split(".")
        patch_dict: dict = {}
        cur = patch_dict
        for part in parts[:-1]:
            cur[part] = {}
            cur = cur[part]
        cur[parts[-1]] = value

        # Get current override and merge patch into it
        if registry is not None:
            record = registry.get(client_id)
            current_override = (record.config or {}) if record else {}
        else:
            current_override = {}

        new_override = deep_merge(current_override, patch_dict)
        return self.set(client_id, new_override, registry, validate=True)

    def get_override(self, client_id: str, registry=None) -> dict:
        """Return only the client's overrides (not merged with system)."""
        if registry is None:
            return {}
        record = registry.get(client_id)
        return (record.config or {}) if record else {}

    def reset(self, client_id: str, registry=None) -> dict:
        """
        Remove all client overrides — revert to system defaults.
        Returns the system config.
        """
        if registry is not None:
            registry.update(client_id, config={})
            logger.info(f"Config reset to system defaults for client={client_id}")
        return copy.deepcopy(self._system)

    def diff(self, client_id: str, registry=None) -> dict:
        """
        Show what the client overrides vs system defaults.
        Returns {key: {system: val, client: val}} for changed keys.
        """
        override = self.get_override(client_id, registry)
        return _compute_diff(self._system, override)

    # ── File-based override (alternative to registry) ─────────

    def save_to_file(
        self,
        client_id: str,
        override: dict,
        directory: str = "configs/clients",
        validate: bool = True,
    ) -> Path:
        """
        Save client override to a YAML file.
        Path: configs/clients/{client_id}.yaml
        """
        if validate:
            errors = validate_client_config(override)
            if errors:
                raise ConfigValidationError(
                    f"Config validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
                )
        dir_path = Path(directory)
        dir_path.mkdir(parents=True, exist_ok=True)
        file_path = dir_path / f"{client_id}.yaml"
        with open(file_path, "w") as f:
            yaml.dump(override, f, default_flow_style=False, allow_unicode=True)
        logger.info(f"Client config saved: {file_path}")
        return file_path

    def load_from_file(
        self,
        client_id: str,
        directory: str = "configs/clients",
    ) -> dict:
        """Load client override from YAML file."""
        file_path = Path(directory) / f"{client_id}.yaml"
        if not file_path.exists():
            return {}
        with open(file_path) as f:
            override = yaml.safe_load(f) or {}
        return override

    def get_effective_from_file(
        self,
        client_id: str,
        directory: str = "configs/clients",
    ) -> dict:
        """Get effective config merging system + file-based override."""
        self._reload_system()
        override = self.load_from_file(client_id, directory)
        return deep_merge(self._system, override) if override else copy.deepcopy(self._system)

    def list_client_configs(self, directory: str = "configs/clients") -> list[str]:
        """List all clients that have file-based configs."""
        path = Path(directory)
        if not path.exists():
            return []
        return [f.stem for f in sorted(path.glob("*.yaml"))]


# ── Diff helper ───────────────────────────────────────────────

def _compute_diff(base: dict, override: dict, prefix: str = "") -> dict:
    """Recursively compute what override changes vs base."""
    result = {}
    for key, val in override.items():
        full_key = f"{prefix}.{key}" if prefix else key
        base_val = base.get(key)
        if isinstance(val, dict) and isinstance(base_val, dict):
            nested = _compute_diff(base_val, val, full_key)
            result.update(nested)
        elif val != base_val:
            result[full_key] = {"system": base_val, "client": val}
    return result


# ── Module-level singleton ────────────────────────────────────

_manager: ClientConfigManager | None = None


def get_config_manager(system_config_path: str | None = None) -> ClientConfigManager:
    """Return global ClientConfigManager singleton."""
    global _manager
    if _manager is None or system_config_path:
        path = system_config_path or os.environ.get(
            "CONFIG_PATH", "configs/config.yaml"
        )
        _manager = ClientConfigManager(path)
    return _manager
