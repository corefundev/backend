"""
Model load factory for `service_cache.get_or_load`.

R5-M1 slice 8 (2026-05-18) — extracted from src/api/main.py so
both `main.py` (`/reload` handler) and `routers/inference.py`
(`/predict` cold-cache path) can import the same cold-load
function without circular import on main.py.

This module pulls the ML stack (`ForecastingService`,
`ClientStorage`) — so we keep it OUT of `src/api/service_cache.py`
(which is intentionally ML-stack-free, only LRU + locks).
"""
from __future__ import annotations

import logging
import os

from fastapi import HTTPException

from src.models.fallback import ForecastingService
from src.pipeline.inference_utils import get_config as _get_cfg
from src.storage.backend import ClientStorage


logger = logging.getLogger(__name__)

CONFIG_PATH: str = os.getenv("CONFIG_PATH", "configs/config.yaml")


def load_service_for_dataset(client_id: str,
                             dataset_id: "str | None") -> ForecastingService:
    """Cold-load factory (F1 #615): модель живёт ПО-ДАТАСЕТНО (DS-1
    «датасет = отдельная модель») — читаем слот датасета, а не legacy
    клиентский. dataset_id=None — легаси-клиент без датасетов (чтение
    старого слота, обратная совместимость). Raises HTTPException 404
    (no trained model) / 503 (primary + fallback both unavailable) —
    those propagate through `service_cache.get_or_load` unchanged.
    """
    config  = _get_cfg(CONFIG_PATH)
    storage = ClientStorage(client_id, dataset_id=dataset_id)
    service = ForecastingService(config)
    if not storage.model_exists():
        # review #616 F3: клиент легаси-эпохи создал датасет, но ещё не
        # обучил его — датасетный слот пуст, а рабочая легаси-модель
        # лежит рядом. Раньше /predict её сервил; ломать работающего
        # клиента деплоем нельзя — сервим legacy с громким логом до
        # первого датасетного обучения.
        legacy = ClientStorage(client_id)
        if dataset_id is not None and legacy.model_exists():
            logger.warning(
                "dataset %s has no model for %s — serving LEGACY slot "
                "until the dataset is trained", dataset_id, client_id)
            storage = legacy
        else:
            raise HTTPException(
                status_code=404,
                detail=f"No trained model for client '{client_id}'. Run training first.",
            )
    try:
        service.load_primary(storage)
    except Exception as e:
        logger.error(f"Primary load failed for {client_id}: {e}")
        if not storage.fallback_exists():
            raise HTTPException(
                status_code=503,
                detail="Primary unavailable, no fallback",
            )
        service.load_fallback_from_storage(storage)
    return service
