"""
#570 PC-1: «Календарь акций» — API загрузки/применения файла-календаря.

Позиционирование (решение владельца): «Прогноз знает историю продаж, но не
знает ваших планов — загрузите план акций». Никаких обещаний «+X% точности».

Флоу: POST файл → конвейер загрузок (AV-скан → promo-валидация в воркере,
kind='promo_calendar') → кандидат pending_review с честным отчётом →
POST /apply (atomic swap: прежний active → replaced) → влияние на прогноз
ПОСЛЕ следующего обучения (говорим клиенту явно).

Fail-open: нет активного календаря → нулевые promo-фичи, поведение как
раньше. DELETE возвращает датасет ровно в это состояние.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from src.audit import record_event
from src.auth.jwt_auth import AuthContext, get_current_client, require_client_access
from src.auth.signup_rate_limit import client_ip

logger = logging.getLogger(__name__)
router = APIRouter(tags=["promo-calendar"])

EVT = "promo_calendar"


class ApplyRequest(BaseModel):
    calendar_id: str = Field(..., min_length=1, max_length=64)


def _promo_calendar_allowed(client_id: str) -> None:
    """ЕДИНАЯ точка тариф-гейта календаря акций.

    Решение владельца по гейту ОТКРЫТО (эпик #570): пока доступно всем
    тарифам. Когда решение будет принято — проверка тарифа добавляется
    здесь и только здесь (плюс тест test_570_gate_single_point)."""
    return None


def _dataset_or_404(dataset_id: str, client_id: str):
    from src.storage.datasets import ACTIVE, get_datasets_registry
    ds = get_datasets_registry().get(dataset_id)
    # R4-7: cross-tenant/deleted → одинаковый 404, без утечки существования
    if ds is None or ds.client_id != client_id or ds.status != ACTIVE:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return ds


def _audit(action: str, client_id: str, http_req: Request,
           meta: dict | None = None) -> None:
    try:
        record_event(
            event_type=EVT, event_subtype=action, client_id=client_id,
            ip=client_ip(http_req),
            user_agent=http_req.headers.get("user-agent"),
            target_type="promo_calendar", target_id=client_id,
            metadata=meta or {},
        )
    except Exception as e:    # noqa: BLE001 — аудит best-effort, действие важнее
        logger.warning("promo-calendar audit failed (%s): %s", action, e)


def _cal_view(rec) -> dict:
    return {
        "calendar_id": rec.calendar_id,
        "status": rec.status,
        "filename": rec.filename,
        "rows_accepted": rec.rows_accepted,
        "date_min": rec.date_min,
        "date_max": rec.date_max,
        "report": rec.report,
        "uploaded_at": rec.uploaded_at,
        "applied_at": rec.applied_at,
    }


@router.post("/clients/{client_id}/datasets/{dataset_id}/promo-calendar",
             status_code=202)
async def promo_calendar_upload(
    client_id: str,
    dataset_id: str,
    http_req: Request,
    file: UploadFile = File(...),
    auth: AuthContext = Depends(get_current_client),
):
    """Принять файл календаря в конвейер загрузок (kind='promo_calendar').
    202 + upload_id; готовность и отчёт — GET promo-calendar (poll)."""
    require_client_access(client_id, auth)
    _promo_calendar_allowed(client_id)
    _dataset_or_404(dataset_id, client_id)

    from src.pipeline.upload_workers import enqueue_scan
    from src.storage.upload_pipeline import UploadRejected, accept_upload

    data = await file.read()
    try:
        record = accept_upload(
            client_id, file.filename or "calendar.csv", data,
            dataset_id=dataset_id, kind="promo_calendar")
    except UploadRejected as e:
        raise HTTPException(status_code=422, detail=str(e))

    enqueue_scan(record.upload_id, client_id)
    _audit("uploaded", client_id, http_req,
           {"dataset_id": dataset_id, "upload_id": record.upload_id,
            "filename": record.filename, "size": record.size_bytes})
    return {"upload_id": record.upload_id, "status": record.status}


@router.get("/clients/{client_id}/datasets/{dataset_id}/promo-calendar")
def promo_calendar_state(
    client_id: str,
    dataset_id: str,
    auth: AuthContext = Depends(get_current_client),
):
    """Состояние календаря датасета: активный + кандидат (превью/отчёт) +
    статус последней загрузки (для поллинга обработки)."""
    require_client_access(client_id, auth)
    _promo_calendar_allowed(client_id)
    _dataset_or_404(dataset_id, client_id)

    from src.storage import upload_registry as ur
    from src.storage.promo_calendar import get_promo_calendar_registry

    reg = get_promo_calendar_registry()
    active = reg.get_active(dataset_id)
    candidate = reg.get_candidate(dataset_id)

    # последняя promo-загрузка датасета — для поллинга scanning/processing
    last_upload = None
    for u in ur.get_upload_registry().list_for_client(client_id, limit=100):
        if u.kind == "promo_calendar" and u.dataset_id == dataset_id:
            last_upload = {"upload_id": u.upload_id, "status": u.status,
                           "error_message": u.error_message,
                           "filename": u.filename}
            break

    return {
        "active": _cal_view(active) if active else None,
        "candidate": _cal_view(candidate) if candidate else None,
        "last_upload": last_upload,
        # решение владельца: влияние календаря — после следующего обучения
        "note": "Календарь влияет на прогноз после следующего обучения модели.",
    }


@router.post("/clients/{client_id}/datasets/{dataset_id}/promo-calendar/apply")
def promo_calendar_apply(
    client_id: str,
    dataset_id: str,
    req: ApplyRequest,
    http_req: Request,
    auth: AuthContext = Depends(get_current_client),
):
    """Применить кандидата: atomic swap (прежний active → replaced)."""
    require_client_access(client_id, auth)
    _promo_calendar_allowed(client_id)
    _dataset_or_404(dataset_id, client_id)

    from src.storage.promo_calendar import get_promo_calendar_registry

    reg = get_promo_calendar_registry()
    rec = reg.get(req.calendar_id)
    if rec is None or rec.client_id != client_id or rec.dataset_id != dataset_id:
        raise HTTPException(status_code=404, detail="Календарь не найден")
    if rec.status != "pending_review":
        raise HTTPException(
            status_code=409,
            detail=f"Календарь уже в состоянии «{rec.status}» — применить "
                   "можно только свежезагруженный")
    applied = reg.apply(req.calendar_id)
    _audit("applied", client_id, http_req,
           {"dataset_id": dataset_id, "calendar_id": req.calendar_id,
            "rows": applied.rows_accepted})
    return {"calendar": _cal_view(applied),
            "note": "Календарь применён. Прогноз учтёт его после следующего "
                    "обучения модели."}


@router.delete("/clients/{client_id}/datasets/{dataset_id}/promo-calendar")
def promo_calendar_delete(
    client_id: str,
    dataset_id: str,
    http_req: Request,
    auth: AuthContext = Depends(get_current_client),
):
    """Снять активный календарь: датасет возвращается к «календаря нет»
    (fail-open, нулевые promo-фичи после следующего обучения)."""
    require_client_access(client_id, auth)
    _promo_calendar_allowed(client_id)
    _dataset_or_404(dataset_id, client_id)

    from src.storage.promo_calendar import get_promo_calendar_registry

    removed = get_promo_calendar_registry().remove_active(dataset_id)
    if removed:
        _audit("removed", client_id, http_req, {"dataset_id": dataset_id})
    return {"removed": removed}


@router.get("/clients/{client_id}/promo-calendar/upcoming")
def promo_calendar_upcoming(
    client_id: str,
    sku: str,
    auth: AuthContext = Depends(get_current_client),
):
    """#570 PC-3: предстоящие акции SKU для бейджа на странице прогноза.

    Датасет — дефолтный (как у прогнозов: датасет последнего promoted-
    обучения). «Предстоящие» = события активного календаря, чьё date_to
    не раньше конца истории данных (окно прогноза начинается после него).
    v1 — только sku-события; категорийные акции в бейдже — v2
    (резолв категории SKU требует чтения снапшота)."""
    require_client_access(client_id, auth)
    _promo_calendar_allowed(client_id)

    from src.api.routers.inference import _default_dataset_id
    from src.storage.datasets import get_datasets_registry
    from src.storage.promo_calendar import get_promo_calendar_registry

    dataset_id = _default_dataset_id(client_id)
    if not dataset_id:
        return {"events": []}
    reg = get_promo_calendar_registry()
    active = reg.get_active(dataset_id)
    if active is None:
        return {"events": []}

    # конец истории данных = начало окна прогноза
    date_max = None
    try:
        ds_reg = get_datasets_registry()
        ds = ds_reg.get(dataset_id)
        if ds is not None and ds.current_version > 0:
            v = ds_reg.get_version(dataset_id, ds.current_version)
            date_max = getattr(v, "date_max", None)
    except Exception as e:    # noqa: BLE001 — бейдж вспомогательный, не роняем
        logger.warning("upcoming: dataset date_max unavailable (%s): %s",
                       dataset_id, e)

    events = []
    for ev in reg.list_events(active.calendar_id):
        if ev.sku != sku:
            continue
        if date_max and str(ev.date_to) < str(date_max)[:10]:
            continue                      # акция целиком в прошлом данных
        events.append({"date_from": ev.date_from, "date_to": ev.date_to,
                       "name": ev.name})
    events.sort(key=lambda x: x["date_from"])
    return {"events": events[:5]}


_TEMPLATE_CSV = (
    "﻿"    # BOM: Excel-RU открывает UTF-8 корректно
    "sku;category;date_from;date_to;depth;name\n"
    "SKU-00123;;01.09.2026;14.09.2026;15;Осенняя распродажа\n"
    ";Молочные продукты;05.09.2026;07.09.2026;10;Выходные -10%\n"
    "SKU-00456;;20.09.2026;20.09.2026;;День бренда\n"
)


@router.get("/clients/{client_id}/promo-calendar/template")
def promo_calendar_template(
    client_id: str,
    auth: AuthContext = Depends(get_current_client),
):
    """Шаблон файла календаря: ';' + BOM (Excel-RU), примеры строк.
    Правила: заполняется РОВНО одно из sku|category; даты включительно;
    depth (процент скидки) опционален. Прошлые акции тоже загружаются —
    без них модели не на чем оценить эффект."""
    require_client_access(client_id, auth)
    _promo_calendar_allowed(client_id)
    return Response(
        content=_TEMPLATE_CSV.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 'attachment; filename="sprosly-promo-calendar-template.csv"'},
    )
