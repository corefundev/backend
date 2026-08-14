"""DS-1 #466 (epic #463) — клиентские датасеты.

Датасет = именованная коллекция обработанных загрузок с версионируемым
слитым снапшотом (правило владельца: новый файл побеждает на пересечении
(sku, date); отчёт «добавлено/заменено» на каждую докладку). Датасет =
своя модель и прогнозы — обучение/serve подключаются следующим срезом;
здесь — слой данных.

Пофайловый конвейер (#320 скан→подготовка) не тронут: к датасету
прикрепляются только PROCESSED-загрузки.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.auth.jwt_auth import (
    AuthContext,
    get_current_client,
    require_client_access,
)
from src.plans.plans import get_plan_spec
from src.storage import upload_registry as ur
from src.storage.dataset_pipeline import MaterializeError, materialize
from src.storage.datasets import (
    ACTIVE,
    DEFAULT_DATASET_NAME,
    DatasetRecord,
    get_datasets_registry,
    new_dataset_id,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["datasets"])

_NAME_MAX = 80


class DatasetCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=_NAME_MAX)


class DatasetFileAttachRequest(BaseModel):
    upload_id: str = Field(..., min_length=1, max_length=64)


def _dataset_or_404(dataset_id: str, client_id: str):
    ds = get_datasets_registry().get(dataset_id)
    # R4-7: cross-tenant/deleted → одинаковый 404, без утечки существования
    if ds is None or ds.client_id != client_id or ds.status != ACTIVE:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return ds


def _ds_view(ds, reg) -> dict:
    cur = (reg.get_version(ds.dataset_id, ds.current_version)
           if ds.current_version > 0 else None)
    return {
        "dataset_id": ds.dataset_id, "name": ds.name,
        "current_version": ds.current_version,
        "created_at": ds.created_at, "updated_at": ds.updated_at,
        "files": len(reg.list_files(ds.dataset_id)),
        "row_count": cur.row_count if cur else 0,
        "sku_count": cur.sku_count if cur else 0,
        "date_min": cur.date_min if cur else None,
        "date_max": cur.date_max if cur else None,
        "fingerprint": cur.fingerprint if cur else None,
    }


def _model_blocks(client_id: str) -> Optional[dict]:
    """DS-2 #467: per-dataset model card — newest FINISHED run with an
    artifact, keyed by dataset_id. One runs query per request (not per
    dataset). Best-effort: an unavailable registry degrades to None
    (call sites `or {}` → «модель не обучена»), never a 5xx."""
    try:
        from src.storage.training_runs import (
            FINISHED,
            get_training_runs_registry,
        )
        out: dict = {}
        for r in get_training_runs_registry().list_for_client(
                client_id, limit=100):
            if (r.status != FINISHED or not r.model_path
                    or not r.dataset_id or r.dataset_id in out):
                continue    # list is newest-first — first hit wins
            improvement = None
            if (r.baseline_wmape and r.wmape is not None
                    and r.baseline_wmape > 0):
                improvement = (r.baseline_wmape - r.wmape) / r.baseline_wmape
            out[r.dataset_id] = {
                "trained_at": r.ended_at,
                "dataset_version": r.dataset_version,
                "wmape": r.wmape,
                "wmape_order_7": r.wmape_order_7,
                "wmape_order_14": r.wmape_order_14,
                "baseline_wmape": r.baseline_wmape,
                "improvement_vs_naive": improvement,
                # MA-2 #520: честная подача — типичный SKU и хвост боли.
                "wmape_median": r.wmape_median,
                "wmape_p90": r.wmape_p90,
                "eval_coverage": r.eval_coverage,
            }
        return out
    except Exception as e:    # noqa: BLE001 — degrade, don't 5xx
        logger.warning("model blocks unavailable for %s: %s", client_id, e)
        return None


def _with_model(view: dict, blocks: dict) -> dict:
    m = blocks.get(view["dataset_id"])
    if m is not None:
        m = dict(m)
        m["up_to_date"] = (m["dataset_version"] == view["current_version"]
                           if m["dataset_version"] is not None else None)
    view["model"] = m
    return view


def _migrate_default_dataset(client_id: str) -> None:
    """Ленивая миграция: у клиента есть processed-загрузки, но нет ни
    одного датасета → создаём «Основной» с ПОСЛЕДНЕЙ processed-загрузкой.
    Идемпотентно: гонка ловится unique-индексом (client, name, live)."""
    reg = get_datasets_registry()
    uploads = [u for u in ur.get_upload_registry().list_for_client(
        client_id, limit=100) if u.status == ur.PROCESSED]
    if not uploads:
        return
    latest = max(uploads, key=lambda u: u.created_at)
    ds = DatasetRecord(dataset_id=new_dataset_id(), client_id=client_id,
                       name=DEFAULT_DATASET_NAME)
    try:
        reg.create(ds)
    except Exception:    # noqa: BLE001 — гонка создания: датасет уже есть
        return
    reg.attach_file(ds.dataset_id, latest.upload_id)
    try:
        materialize(ds.dataset_id, new_upload_id=latest.upload_id)
    except MaterializeError as e:
        logger.warning(
            f"default-dataset migration for {client_id}: materialize "
            f"failed: {e}")


@router.post("/clients/{client_id}/datasets", status_code=201)
def create_dataset(client_id: str, req: DatasetCreateRequest,
                   auth: AuthContext = Depends(get_current_client)):
    require_client_access(client_id, auth)
    reg = get_datasets_registry()
    existing = reg.list_for_client(client_id)

    from src.clients.registry import get_registry
    rec = get_registry().get(client_id)
    limit = get_plan_spec(rec.plan if rec else None).datasets_limit
    if len(existing) >= limit:
        raise HTTPException(
            status_code=403,
            detail=f"Лимит датасетов вашего тарифа — {limit}. "
                   "Удалите неиспользуемый или повысьте тариф.")

    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Пустое имя датасета")
    if any(d.name == name for d in existing):
        raise HTTPException(status_code=409,
                            detail="Датасет с таким именем уже есть")
    ds = DatasetRecord(dataset_id=new_dataset_id(), client_id=client_id,
                       name=name)
    try:
        reg.create(ds)
    except Exception:    # noqa: BLE001 — unique-гонка → тот же 409
        raise HTTPException(status_code=409,
                            detail="Датасет с таким именем уже есть")
    return _ds_view(ds, reg)


@router.get("/clients/{client_id}/datasets")
def list_datasets(client_id: str,
                  auth: AuthContext = Depends(get_current_client)):
    require_client_access(client_id, auth)
    reg = get_datasets_registry()
    rows = reg.list_for_client(client_id)
    if not rows:
        _migrate_default_dataset(client_id)
        rows = reg.list_for_client(client_id)
    blocks = _model_blocks(client_id) or {}
    return {"datasets": [_with_model(_ds_view(d, reg), blocks)
                         for d in rows]}


@router.get("/clients/{client_id}/datasets/{dataset_id}")
def get_dataset(client_id: str, dataset_id: str,
                auth: AuthContext = Depends(get_current_client)):
    require_client_access(client_id, auth)
    ds = _dataset_or_404(dataset_id, client_id)
    reg = get_datasets_registry()
    ureg = ur.get_upload_registry()
    files = []
    for f in reg.list_files(dataset_id):
        u = ureg.get(f["upload_id"])
        files.append({
            "upload_id": f["upload_id"], "added_at": f["added_at"],
            "filename": u.filename if u else None,
            "status": u.status if u else "deleted",
            "row_count": u.row_count if u else None,
            # DS-2 #467: «+N · заменено M» (None = pre-DS-2 attach)
            "merge_added": f.get("merge_added"),
            "merge_replaced": f.get("merge_replaced"),
            "date_min": u.date_min if u else None,
            "date_max": u.date_max if u else None,
        })
    versions = [{
        "version": v.version, "status": v.status,
        "fingerprint": v.fingerprint, "row_count": v.row_count,
        "sku_count": v.sku_count, "date_min": v.date_min,
        "date_max": v.date_max, "merge_report": v.merge_report,
        "created_at": v.created_at,
    } for v in reg.list_versions(dataset_id, limit=20)]
    # DS-2 #467: uploads aimed at this dataset that have not attached yet
    # (awaiting scan/prep, or failed) — the files table shows them as
    # «Ждёт подготовки»/error rows next to the merged ones.
    attached = {f["upload_id"] for f in reg.list_files(dataset_id)}
    pending = [{
        "upload_id": u.upload_id, "filename": u.filename,
        "status": u.status, "row_count": u.row_count,
        "date_min": u.date_min, "date_max": u.date_max,
        "created_at": u.created_at,
    } for u in ureg.list_for_client(client_id, limit=100)
        if u.dataset_id == dataset_id and u.upload_id not in attached
        # #570 PC-3: календарные загрузки живут в секции «Календарь акций»,
        # в списке ФАЙЛОВ ДАННЫХ датасета им не место (kind-ветвление)
        and u.kind != "promo_calendar"]

    out = _with_model(_ds_view(ds, reg), _model_blocks(client_id) or {})
    out["files_detail"] = files
    out["versions"] = versions
    out["pending_uploads"] = pending
    return out


@router.post("/clients/{client_id}/datasets/{dataset_id}/files",
             status_code=201)
def attach_file(client_id: str, dataset_id: str,
                req: DatasetFileAttachRequest,
                auth: AuthContext = Depends(get_current_client)):
    require_client_access(client_id, auth)
    _dataset_or_404(dataset_id, client_id)
    reg = get_datasets_registry()

    urec = ur.get_upload_registry().get(req.upload_id)
    if urec is None or urec.client_id != client_id:
        raise HTTPException(status_code=404, detail="Upload not found")
    if urec.status != ur.PROCESSED:
        raise HTTPException(
            status_code=409,
            detail=f"Файл ещё не обработан (статус: {urec.status}). "
                   "Дождитесь подготовки.")
    if any(f["upload_id"] == req.upload_id
           for f in reg.list_files(dataset_id)):
        raise HTTPException(status_code=409,
                            detail="Файл уже в этом датасете")

    reg.attach_file(dataset_id, req.upload_id)
    try:
        v = materialize(dataset_id, new_upload_id=req.upload_id)
    except MaterializeError as e:
        reg.detach_file(dataset_id, req.upload_id)
        raise HTTPException(status_code=422, detail=str(e))
    return {"dataset_id": dataset_id, "version": v.version,
            "fingerprint": v.fingerprint, "row_count": v.row_count,
            "sku_count": v.sku_count, "date_min": v.date_min,
            "date_max": v.date_max, "merge_report": v.merge_report}


@router.delete("/clients/{client_id}/datasets/{dataset_id}/files/{upload_id}")
def detach_file(client_id: str, dataset_id: str, upload_id: str,
                auth: AuthContext = Depends(get_current_client)):
    require_client_access(client_id, auth)
    _dataset_or_404(dataset_id, client_id)
    reg = get_datasets_registry()
    if not reg.detach_file(dataset_id, upload_id):
        raise HTTPException(status_code=404, detail="Файл не в датасете")
    remaining = reg.list_files(dataset_id)
    version: Optional[int] = None
    if remaining:
        try:
            version = materialize(dataset_id).version
        except MaterializeError as e:
            raise HTTPException(status_code=422, detail=str(e))
    return {"dataset_id": dataset_id, "detached": upload_id,
            "rebuilt_version": version}


def _create_run_row(client_id: str, plan: str, data_path: str,
                    dataset_id: str, dataset_version: int):
    """Best-effort строка истории обучений; None при сбое — обучение
    важнее истории (тот же контракт, что у /train)."""
    try:
        from src.storage.training_runs import (
            TrainingRunRecord,
            get_training_runs_registry,
            new_run_id,
        )
        run_id = new_run_id()
        get_training_runs_registry().create(TrainingRunRecord(
            run_id=run_id, client_id=client_id, plan=plan,
            data_path=data_path, dataset_id=dataset_id,
            dataset_version=dataset_version))
        return run_id
    except Exception as e:    # noqa: BLE001 — история best-effort
        logger.warning("could not create training_runs row: %s", e)
        return None


def _release_training_claim(registry, client_id: str) -> None:
    """R5-4: освободить claim 'training' при сбое enqueue. Fail-silent —
    rollback-of-rollback не должен ломать сам запрос."""
    try:
        registry.update(client_id, status="ready")
    except Exception as e:    # noqa: BLE001
        logger.warning("training claim release failed: %s", e)


@router.post("/clients/{client_id}/datasets/{dataset_id}/train",
             status_code=202)
def train_dataset(client_id: str, dataset_id: str,
                  auth: AuthContext = Depends(get_current_client)):
    """Кнопка «Обучить модель» (решение владельца: ретрейн ПО КНОПКЕ).

    Гейты — те же, что у /train (R11-H4 single-in-flight, квота плана,
    fail-closed SKU-кап), источник данных — снапшот ТЕКУЩЕЙ версии
    датасета; модель уезжает в неймспейс датасета (+ легаси-слот, если
    датасет дефолтный), прогнозы — в его партицию."""
    require_client_access(client_id, auth)
    ds = _dataset_or_404(dataset_id, client_id)
    reg = get_datasets_registry()

    if ds.current_version < 1:
        raise HTTPException(status_code=409,
                            detail="В датасете ещё нет данных — доложите файл")
    version = reg.get_version(dataset_id, ds.current_version)
    if version is None or version.status != "ready" or not version.snapshot_key:
        raise HTTPException(status_code=409,
                            detail="Текущая версия датасета не готова")

    from src.clients.registry import get_registry
    from src.plans.quota import (
        QuotaExceeded,
        REASON_IN_FLIGHT,
        TrainingInProgress,
        check_training_quota,
        denial_envelope,
        denial_envelope_from_exc,
        record_training_started,
    )
    from src.plans.plans import get_plan_spec as _spec

    registry = get_registry()
    record = registry.get(client_id)
    if record is None:
        raise HTTPException(404, detail=f"Client '{client_id}' not found")
    if record.status == "training":
        # #466 battle-fix: рукописный reason-code ронял ветку 500-кой —
        # denial_envelope ассертит членство в QUOTA_REASON_CODES; только
        # константа (как в легаси /train).
        raise HTTPException(status_code=409, detail=denial_envelope(
            REASON_IN_FLIGHT,
            "A training run is already in progress for this client."))
    try:
        check_training_quota(record)
    except QuotaExceeded as e:
        headers = ({"Retry-After": str(e.retry_after_sec)}
                   if e.retry_after_sec else None)
        raise HTTPException(status_code=429,
                            detail=denial_envelope_from_exc(e),
                            headers=headers)

    # SKU-кап — fail-closed по счётчику версии (R11-H3-класс).
    spec = _spec(record.plan)
    if spec.max_skus is not None:
        if not version.sku_count:
            raise HTTPException(status_code=409,
                                detail="Версия без счётчика SKU — "
                                       "пересоберите датасет")
        if version.sku_count > spec.max_skus:
            raise HTTPException(
                status_code=403,
                detail=f"В датасете {version.sku_count} SKU — больше "
                       f"лимита тарифа ({spec.max_skus})")

    from src.storage import zones as z
    data_path = z.get_zone_backend(z.Zone.PROCESSED).path(
        version.snapshot_key)
    try:
        record = record_training_started(registry, record)
    except TrainingInProgress as e:
        raise HTTPException(status_code=409,
                            detail=denial_envelope_from_exc(e))
    except QuotaExceeded as e:
        headers = ({"Retry-After": str(e.retry_after_sec)}
                   if e.retry_after_sec else None)
        raise HTTPException(status_code=429,
                            detail=denial_envelope_from_exc(e),
                            headers=headers)

    run_id = _create_run_row(client_id, record.plan, data_path,
                             dataset_id, version.version)

    try:
        from src.pipeline.task_queue import enqueue_training
        job_id = enqueue_training(
            client_id=client_id, data_path=data_path, run_id=run_id,
            dataset_id=dataset_id)
    except Exception as e:    # noqa: BLE001 — освобождаем claim (R5-4)
        _release_training_claim(registry, client_id)
        logger.error("dataset train enqueue failed: %s", e)
        raise HTTPException(status_code=503,
                            detail="Очередь обучения недоступна — "
                                   "повторите позже")

    return {"dataset_id": dataset_id, "dataset_version": version.version,
            "run_id": run_id, "job_id": job_id, "status": "queued"}


@router.delete("/clients/{client_id}/datasets/{dataset_id}")
def delete_dataset(client_id: str, dataset_id: str,
                   auth: AuthContext = Depends(get_current_client)):
    require_client_access(client_id, auth)
    _dataset_or_404(dataset_id, client_id)
    get_datasets_registry().soft_delete(dataset_id)
    return {"dataset_id": dataset_id, "status": "deleted"}
