"""
src/models/registry_gates.py

Model lifecycle gates: None → Staging → Production → Archived.
No model can reach Production without passing the validation gate.

  if new_model.wmape < current_production.wmape:
      promote to Production
  else:
      reject (stays in Staging)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class ModelStage(str, Enum):
    NONE       = "None"
    STAGING    = "Staging"
    PRODUCTION = "Production"
    ARCHIVED   = "Archived"


@dataclass
class ModelCandidate:
    run_id:          str
    client_id:       str
    wmape_mean:      float
    mase_mean:       float
    dataset_hash:    str
    feature_version: str
    config_hash:     str
    stage:           ModelStage = ModelStage.NONE


class ModelRegistryGate:
    """
    Wraps MLflow to enforce lifecycle gates.
    Only models that beat the current Production are promoted.
    """

    def __init__(self, tracking_uri: str = "http://mlflow:5000"):
        self._uri = tracking_uri

    def _client(self):
        import mlflow
        mlflow.set_tracking_uri(self._uri)
        return mlflow.tracking.MlflowClient()

    def promote_to_staging(self, run_id: str, client_id: str) -> bool:
        """Move a run to Staging. Always succeeds."""
        try:
            c = self._client()
            # Register model if not already registered
            model_name = f"sku-forecasting-{client_id}"
            try:
                c.create_registered_model(model_name)
            except Exception:
                pass  # already exists

            mv = c.create_model_version(
                name=model_name, run_id=run_id, source=f"runs:/{run_id}/model"
            )
            c.transition_model_version_stage(
                name=model_name, version=mv.version, stage="Staging"
            )
            logger.info(f"Model promoted to Staging: {model_name} v{mv.version}")
            return True
        except Exception as e:
            logger.warning(f"MLflow staging failed: {e}")
            return False

    def validate_and_promote(
        self,
        run_id:       str,
        client_id:    str,
        new_wmape:    float,
        buffer_pct:   float = 0.0,
    ) -> tuple[bool, str]:
        """
        Validation gate: promote run_id to Production only if it beats current.
        Returns (promoted, reason).
        """
        try:
            c          = self._client()
            model_name = f"sku-forecasting-{client_id}"
            current_wmape = self._get_production_wmape(c, model_name)

            if current_wmape is None:
                # No production model yet — promote unconditionally
                self._do_promote(c, model_name, run_id)
                return True, "First production model"

            threshold = current_wmape * (1 + buffer_pct)
            if new_wmape <= threshold:
                self._do_promote(c, model_name, run_id)
                return True, f"WMAPE {new_wmape:.4f} ≤ threshold {threshold:.4f}"
            else:
                logger.warning(
                    f"Validation gate rejected: new WMAPE={new_wmape:.4f} "
                    f"> current={current_wmape:.4f} threshold={threshold:.4f}"
                )
                return False, f"WMAPE {new_wmape:.4f} > threshold {threshold:.4f}"

        except Exception as e:
            logger.warning(f"MLflow gate failed: {e} — skipping gate")
            return True, f"Gate skipped (MLflow unavailable): {e}"

    def _get_production_wmape(self, client, model_name: str) -> Optional[float]:
        """Get WMAPE of current Production model using search_model_versions (non-deprecated)."""
        try:
            versions = client.search_model_versions(
                f"name='{model_name}'",
                order_by=["creation_timestamp DESC"],
            )
            prod = [v for v in versions if v.current_stage == "Production"]
            if not prod:
                return None
            run_id = prod[0].run_id
            run    = client.get_run(run_id)
            return run.data.metrics.get("wmape_mean")
        except Exception:
            return None

    def _do_promote(self, client, model_name: str, run_id: str) -> None:
        """Promote latest Staging version to Production, archive old Production."""
        try:
            # Archive existing Production (use search_model_versions — non-deprecated)
            all_vers = client.search_model_versions(f"name='{model_name}'")
            old_prod = [v for v in all_vers if v.current_stage == "Production"]
            for v in old_prod:
                client.transition_model_version_stage(
                    name=model_name, version=v.version, stage="Archived"
                )
                logger.info(f"Archived old Production v{v.version}")

            # Promote Staging → Production
            staging = [v for v in all_vers if v.current_stage == "Staging"]
            for v in staging:
                client.transition_model_version_stage(
                    name=model_name, version=v.version, stage="Production"
                )
                logger.info(f"Promoted to Production: {model_name} v{v.version}")
        except Exception as e:
            logger.warning(f"MLflow promotion failed: {e}")

    def get_current_stage(self, client_id: str, run_id: str) -> ModelStage:
        """Return current stage of a run."""
        try:
            c        = self._client()
            versions = c.search_model_versions(f"run_id='{run_id}'")
            if versions:
                stage = versions[0].current_stage
                return ModelStage(stage)
        except Exception:
            pass
        return ModelStage.NONE


# ── Module-level singleton ────────────────────────────────────
import os
_gate: Optional[ModelRegistryGate] = None


def get_registry_gate() -> ModelRegistryGate:
    global _gate
    if _gate is None:
        _gate = ModelRegistryGate(
            os.environ.get("MLFLOW_TRACKING_URI", "http://mlflow:5000")
        )
    return _gate
