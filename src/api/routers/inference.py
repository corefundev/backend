"""
Inference API — `/predict`, `/predict/batch`, plus the read-side
endpoints (forecasts catalogue, anomalies catalogue, upload SKU
listing).

R5-M1 (2026-05-18) slice 8: the largest single slice — extracted
from src/api/main.py. 5 routes + 3 schemas + 1 helper (`_load_
processed_for_sku`).

Why slice 8 ships LAST among the route-heavy domains
────────────────────────────────────────────────────
`/predict` is the hottest production code path. The slice depends
on two prep extractions:

  1. slice-5 prep (`80d1d86`) — service_cache state + invalidate API
  2. slice-8 prep (`9da7a82`) — service_cache.get_or_load + the
     `load_factory` callback pattern

…plus two slice-8 supporting modules:

  - `src/api/metrics.py` — prometheus_client counters
    (REQUEST_COUNT, REQUEST_LATENCY, FALLBACK_COUNT). Module-level
    counter registration must happen exactly once per process, so
    centralising them avoids "Duplicated timeseries" on import.
  - `src/api/loaders.py` — `load_service_for_client` factory.
    Pulls the ML stack (`ForecastingService` + `ClientStorage`);
    kept out of `service_cache.py` so the cache module stays
    ML-stack-free.

Preserves verbatim
──────────────────
* R2-6 PredictRequest validators (NaN/negative sales, sku length)
* R2-10 per-client rate-limit via `check_predict_attempt`
* R4-7 cross-tenant 404 (not 403) on upload_id mismatch
* R5-2 batch deadlock fix: NO outer batch-wide lock —
  `service_cache.get_or_load` handles per-client coalescing.
* Pydantic `model_config = ConfigDict(protected_namespaces=())`
  on PredictResponse (R6-2 — `model_source`/`model_name` collide
  with pydantic reserved prefix).
* Same Prometheus emission (`REQUEST_COUNT`, `REQUEST_LATENCY`,
  `FALLBACK_COUNT`) for SLO dashboard continuity.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

import numpy as np
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.api.loaders import CONFIG_PATH, load_service_for_client
from src.api.metrics import FALLBACK_COUNT, REQUEST_COUNT, REQUEST_LATENCY
from src.api.service_cache import get_or_load as _get_or_load_service
from src.auth.jwt_auth import AuthContext, get_current_client, require_client_access
from src.auth.signup_rate_limit import RateLimited, check_predict_attempt
from src.clients.registry import get_registry
from src.features.engineering import build_features, get_feature_columns
from src.pipeline.inference_utils import get_config as _get_cfg
from src.pipeline.inference_utils import serve_feature_set
from src.plans.enforcement import clip_horizon_to_plan, model_display_name
from src.plans.plans import get_plan_spec


logger = logging.getLogger(__name__)
router = APIRouter(tags=["inference"])


# ── Request / response schemas ──────────────────────────────────────────

class PredictRequest(BaseModel):
    # SKU must be a real catalog identifier — anything >256 chars is
    # someone abusing the parser. Audit R2-6.
    sku: str = Field(..., min_length=1, max_length=256)
    # Either inline history (legacy CLI / external API users) OR
    # an upload_id (UI flow — backend reads history from the processed
    # parquet for the given upload). Validated post-init below.
    history: list[dict] = Field(default_factory=list)
    upload_id: Optional[str] = None
    # The autoregressive predict loop supports arbitrary horizons; plan clip
    # keeps each tier to its own ceiling (Free 7, Start 90, Business 365).
    horizon: Optional[int] = Field(None, ge=1, le=365)

    # Pydantic validators — before this was added, junk payloads with the
    # right shape (list of dicts, ≥30 items) passed through to pandas which
    # then failed with cryptic NaN/date errors inside the predict handler.
    @field_validator("history")
    @classmethod
    def _validate_history(cls, v: list[dict]) -> list[dict]:
        # Empty list is accepted here — exclusivity check with upload_id
        # happens in the model_validator below.
        if not v:
            return v
        if not isinstance(v, list):
            raise ValueError("history must be a list of records")
        for i, row in enumerate(v):
            if not isinstance(row, dict):
                raise ValueError(f"history[{i}] is not an object")
            if "date" not in row:
                raise ValueError(f"history[{i}] missing required field 'date'")
            if "sales" not in row:
                raise ValueError(f"history[{i}] missing required field 'sales'")
            # sales must be a finite number — pandas silently coerces
            # booleans and strings to float, hiding bad data.
            sales = row["sales"]
            if isinstance(sales, bool):
                raise ValueError(f"history[{i}].sales cannot be a boolean")
            if not isinstance(sales, (int, float)):
                raise ValueError(
                    f"history[{i}].sales must be a number, got {type(sales).__name__}"
                )
            if sales != sales:  # NaN check without importing math
                raise ValueError(f"history[{i}].sales is NaN")
            if sales < 0:
                raise ValueError(f"history[{i}].sales is negative ({sales})")
        return v

    @model_validator(mode="after")
    def _need_history_or_upload(self):
        if not self.history and not self.upload_id:
            raise ValueError(
                "either 'history' (>=30 rows) or 'upload_id' is required"
            )
        if self.history and len(self.history) < 30:
            raise ValueError(
                f"history must contain at least 30 rows (got {len(self.history)})"
            )
        return self

    @field_validator("sku")
    @classmethod
    def _validate_sku(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("sku cannot be empty")
        if len(v) > 128:
            raise ValueError("sku is too long (max 128 chars)")
        return v


class PredictResponse(BaseModel):
    # R6-2 — `model_source` / `model_name` fields collide with
    # pydantic's reserved `model_` namespace. Disable the check —
    # these refer to the ML model artefact, not pydantic's data-model
    # concept; the field names are public API and renaming would break
    # frontend + grpc clients.
    model_config = ConfigDict(protected_namespaces=())

    sku: str
    client_id: str
    forecast: list[float]
    forecast_dates: list[str]
    horizon: int                             # effective horizon actually used
    model_source: str                        # "primary" | "fallback" | "zero"
    model_name: str                          # "Notus" | "Aether" | "Chronos"
    horizon_limited_by_plan: bool = False    # True if requested horizon was clipped


class BatchPredictRequest(BaseModel):
    requests: list[PredictRequest] = Field(..., max_length=100)


# ── Helpers ─────────────────────────────────────────────────────────────

def _load_processed_for_sku(
    client_id: str, upload_id: str, sku: str,
) -> list[dict]:
    """
    Read the processed parquet for an upload, filter rows where the
    SKU column equals `sku`, and return them as a list of dicts shaped
    like the legacy `history` payload (date + sales + optional
    price/promo/stock).

    Raises HTTPException with the right status code so the predict
    handler can re-raise unmodified.
    """
    from src.storage import upload_registry as ur
    from src.storage.upload_pipeline import get_processed_path
    from src.data.loader import load_data
    # Audit R4-7 — collapse "not found" + "cross-tenant" → 404 (mirrors R3-1).
    urec = ur.get_upload_registry().get(upload_id)
    if urec is None or urec.client_id != client_id:
        raise HTTPException(404, detail=f"upload_id {upload_id!r} not found")
    if urec.status != ur.PROCESSED:
        raise HTTPException(
            409, detail=f"upload is not processed (status={urec.status})"
        )

    path = get_processed_path(urec)
    config = _get_cfg(CONFIG_PATH)
    # PERF-1 (#186): push the SKU filter into the parquet read instead of loading
    # the whole multi-SKU frame and filtering in memory on every /predict.
    df = load_data(path, config, sku=sku)

    sku_col  = config["data"]["sku_col"]
    date_col = config["data"]["date_col"]
    target   = config["data"]["target_col"]
    if sku_col not in df.columns:
        raise HTTPException(500, detail=f"processed parquet missing column '{sku_col}'")

    sub = df[df[sku_col] == sku]   # defensive no-op: the read above already filtered
    if len(sub) == 0:
        raise HTTPException(
            404, detail=f"SKU {sku!r} not found in upload {upload_id!r}"
        )
    if len(sub) < 30:
        raise HTTPException(
            422,
            detail=f"SKU {sku!r} has only {len(sub)} rows (min 30 required)",
        )

    sub = sub.sort_values(date_col)
    rows: list[dict] = []
    for _, r in sub.iterrows():
        row = {
            "date":  pd.Timestamp(r[date_col]).strftime("%Y-%m-%d"),
            "sales": float(r[target]),
        }
        for col in ("price", "promo", "stock"):
            if col in sub.columns and pd.notna(r[col]):
                row[col] = float(r[col]) if col != "promo" else int(r[col])
        rows.append(row)
    return rows


# ── Routes ──────────────────────────────────────────────────────────────

# PERF-4 (#186): `def` (not async) — the body is a synchronous psycopg2 read, so
# FastAPI runs it in the threadpool instead of blocking the event loop. (Connection
# pooling is handled by pgbouncer in front of postgres; no app-level pool needed.)
def _default_dataset_id(client_id: str):
    """DS-1 #466 / #469: дефолтный датасет клиента.

    Приоритет — датасет ПОСЛЕДНЕГО promoted-обучения (там свежие прогнозы
    и объяснения; «обучил → сразу вижу результат»), фолбэк — первый
    активный по created_at. Прежний вариант (только created_at) после
    удаления старого датасета показывал ПУСТУЮ страницу прогнозов при
    только что успешном обучении соседнего. None — датасетов нет или
    реестр недоступен (легаси-чтение без фильтра)."""
    try:
        from src.storage.datasets import get_datasets_registry
        ds_rows = get_datasets_registry().list_for_client(client_id)
        if not ds_rows:
            return None
        active_ids = {r.dataset_id for r in ds_rows}
        try:
            from src.storage.training_runs import get_training_runs_registry
            for run in get_training_runs_registry().list_for_client(
                    client_id, limit=20):
                if (run.status == "finished" and run.model_path
                        and run.dataset_id in active_ids):
                    return run.dataset_id
        except Exception as e:    # noqa: BLE001 — фолбэк на created_at
            logger.warning("default-dataset run lookup failed: %s", e)
        return ds_rows[0].dataset_id
    except Exception as e:    # noqa: BLE001 — деградация до легаси-чтения
        logger.warning("default-dataset resolve failed: %s", e)
        return None


@router.get("/clients/{client_id}/forecasts")
def list_forecasts(
    client_id: str,
    sku: Optional[str] = None,
    dataset_id: Optional[str] = None,
    auth: AuthContext = Depends(get_current_client),
):
    """
    Return the auto-generated batch forecasts produced after the
    latest training run. Each SKU's forecast is a flat list of
    values aligned to the date range [first_date..last_date].

    No input form needed — the worker filled this in already, the
    UI just renders.
    """
    require_client_access(client_id, auth)
    from src.storage.forecasts import get_forecasts_registry
    # DS-1 #466: прогнозы партиционированы по датасету. Без параметра —
    # дефолтный (первый активный) датасет клиента; если датасетов нет —
    # легаси-поведение (все строки клиента).
    if dataset_id is None:
        dataset_id = _default_dataset_id(client_id)
    rows = get_forecasts_registry().list_for_client(
        client_id, sku=sku, dataset_id=dataset_id)
    if not rows:
        return {
            "client_id":     client_id,
            "generated_at":  None,
            "first_date":    None,
            "last_date":     None,
            "horizon_days":  0,
            "count":         0,
            "skus":          [],
        }

    # Group by SKU, preserve date order from the SQL ORDER BY clause.
    # Each entry holds (date, value, p10, p90); p10/p90 may be None for
    # rows produced by older training runs (before the v0.8.26 schema
    # migration) or by models without quantile sub-models.
    from collections import defaultdict
    by_sku: dict[str, list[tuple[str, float, "float | None", "float | None", "float | None"]]] = defaultdict(list)
    for r in rows:
        by_sku[r["sku"]].append((r["forecast_date"], r["value"], r.get("p10"), r.get("p90"), r.get("order_qty")))

    all_dates  = sorted({fd for points in by_sku.values() for (fd, *_) in points})
    first_date = all_dates[0]
    last_date  = all_dates[-1]
    generated  = rows[0]["generated_at"]

    skus_payload = []
    for sku_name in sorted(by_sku.keys()):
        date_to_value: dict[str, float] = {}
        date_to_p10:   dict[str, "float | None"] = {}
        date_to_p90:   dict[str, "float | None"] = {}
        date_to_order: dict[str, "float | None"] = {}
        for (fd, v, p10, p90, oq) in by_sku[sku_name]:
            date_to_value[fd] = v
            date_to_p10[fd]   = p10
            date_to_p90[fd]   = p90
            date_to_order[fd] = oq
        # Fill missing dates with None — clients can render gaps.
        values = [date_to_value.get(d) for d in all_dates]
        p10s   = [date_to_p10.get(d)   for d in all_dates]
        p90s   = [date_to_p90.get(d)   for d in all_dates]
        order_qty = [date_to_order.get(d) for d in all_dates]
        skus_payload.append({
            "sku":       sku_name,
            "values":    values,
            "p10":       p10s,
            "p90":       p90s,
            "order_qty": order_qty,   # #308 newsvendor: recommended order quantity
        })

    return {
        "client_id":     client_id,
        "generated_at":  generated,
        "first_date":    first_date,
        "last_date":     last_date,
        "horizon_days":  len(all_dates),
        "count":         len(skus_payload),
        "skus":          skus_payload,
    }


# PERF-4 (#186): `def` — synchronous psycopg2 read → threadpool, off the loop.
@router.get("/clients/{client_id}/order-recommendations")
def order_recommendations(
    client_id: str,
    window: int = 14,
    dataset_id: Optional[str] = None,
    service_level: Optional[float] = None,
    format: str = "json",
    auth: AuthContext = Depends(get_current_client),
):
    """AZ-1 (#465): рекомендации к заказу — агрегат окна заказа по SKU.

    Закупщик заказывает СУММУ периода: по каждому SKU суммируем первые
    `window` дней прогноза — спрос (point), рекомендацию (newsvendor-
    квантиль на τ) и границы интервала. Рекомендация ПЕРЕСЧИТЫВАЕТСЯ на
    лету из хранимых калиброванных p10/p90 той же формулой
    order_quantity(), которой post_training считал сохранённый order_qty
    — поэтому смена τ не требует переобучения. `service_level` override —
    платная ручка (та же, что config-ключ model.service_level, Start+;
    Free всегда получает effective-τ конфига). Дни без калиброванной
    полосы (рекурсивный хвост) в рекомендацию не входят —
    `days_covered`/`days` делает пробел видимым, как скрытая лента p10/p90.

    format=csv — выгрузка для 1С: UTF-8 с BOM, разделитель ';',
    количество к заказу округлено ВВЕРХ до штук (безопасный запас).
    """
    require_client_access(client_id, auth)
    import math

    if not (1 <= window <= 60):
        raise HTTPException(422, detail="window must be 1..60 days")
    if format not in ("json", "csv"):
        raise HTTPException(422, detail="format must be json or csv")

    from src.clients.registry import get_registry
    record = get_registry().get(client_id)
    if record is None:
        raise HTTPException(404, detail=f"Client '{client_id}' not found")

    # ── τ: платный override / effective-конфиг ────────────────────────
    from src.clients.config_manager import get_config_manager
    try:
        cfg = get_config_manager().get_effective(client_id, get_registry())
        cfg_tau = float(cfg.get("model", {}).get("service_level", 0.7))
    except Exception as e:    # noqa: BLE001 — конфиг-блип ≠ отказ страницы
        logger.warning("order-recommendations: effective config failed: %s", e)
        cfg_tau = 0.7
    if service_level is not None:
        if record.plan not in ("start", "business"):
            raise HTTPException(
                403, detail="Настройка сервис-уровня доступна на платных тарифах")
        if not (0.5 <= service_level <= 0.95):
            raise HTTPException(422, detail="service_level must be in [0.5, 0.95]")
        tau = float(service_level)
    else:
        tau = cfg_tau

    # ── строки прогноза (та же датасет-резолюция, что /forecasts) ────
    from src.models.newsvendor import order_quantity
    from src.storage.forecasts import get_forecasts_registry
    if dataset_id is None:
        dataset_id = _default_dataset_id(client_id)
    rows = get_forecasts_registry().list_for_client(client_id, dataset_id=dataset_id)

    # ── остатки: последний известный stock per SKU из snapshot'а датасета
    # (прототип: «Заказ = прогноз + страховой запас − остаток»). Best-effort:
    # нет stock-колонки у клиента / нет snapshot'а / блип чтения → остатки
    # отсутствуют, страница живёт (колонка «—», заказ без вычитания).
    stock_by_sku: dict[str, float] = {}
    if dataset_id:
        try:
            from src.storage import zones as z
            from src.storage.datasets import get_datasets_registry
            ds_reg = get_datasets_registry()
            ds = ds_reg.get(dataset_id)
            if ds is not None and ds.client_id == client_id and ds.current_version >= 1:
                ver = ds_reg.get_version(dataset_id, ds.current_version)
                if ver is not None and ver.snapshot_key:
                    snap = z.get_zone_backend(z.Zone.PROCESSED).load_dataframe(
                        ver.snapshot_key)
                    if "stock" in snap.columns:
                        last = (snap.dropna(subset=["stock"])
                                    .sort_values("date")
                                    .groupby("sku")["stock"].last())
                        stock_by_sku = {str(k): float(v) for k, v in last.items()}
        except Exception as e:    # noqa: BLE001 — enrichment, не отказ страницы
            logger.warning("order-recommendations: stock read failed: %s", e)

    empty_meta: dict = {
        "client_id": client_id, "dataset_id": dataset_id,
        "service_level": tau, "window": window,
        "generated_at": None, "first_date": None, "last_date": None,
        "count": 0, "items": [],
    }
    if not rows:
        if format == "csv":
            raise HTTPException(404, detail="Прогнозов ещё нет — обучите модель")
        return empty_meta

    window_dates = sorted({r["forecast_date"] for r in rows})[:window]
    in_window = set(window_dates)

    from collections import defaultdict
    per_sku: dict[str, dict] = defaultdict(
        lambda: {"demand": 0.0, "order": 0.0, "p10": 0.0, "p90": 0.0, "covered": 0})
    for r in rows:
        if r["forecast_date"] not in in_window:
            continue
        s = per_sku[r["sku"]]
        if r["value"] is not None:
            s["demand"] += float(r["value"])
        oq = order_quantity(r.get("p10"), r.get("p90"), tau)
        if oq is not None:
            s["order"] += oq
            s["p10"] += float(r["p10"])
            s["p90"] += float(r["p90"])
            s["covered"] += 1

    items = []
    for sku_name in sorted(per_sku.keys()):
        s = per_sku[sku_name]
        covered = s["covered"]
        stock = stock_by_sku.get(sku_name)
        # Прототип: страховой запас = квантиль − точечный прогноз окна;
        # заказ = прогноз + запас − остаток, не ниже нуля, вверх до штук.
        order_final = None
        if covered:
            order_final = int(math.ceil(max(0.0, s["order"] - (stock or 0.0))))
        items.append({
            "sku":            sku_name,
            "stock":          stock,
            "demand_sum":     round(s["demand"], 2),
            "safety_sum":     round(s["order"] - s["demand"], 2) if covered else None,
            "order_sum":      round(s["order"], 2) if covered else None,
            "order_qty":      order_final,
            "p10_sum":        round(s["p10"], 2) if covered else None,
            "p90_sum":        round(s["p90"], 2) if covered else None,
            "days":           len(window_dates),
            "days_covered":   covered,
        })

    if format == "csv":
        from starlette.responses import Response
        lines = ["sku;Остаток;Спрос (прогноз);Страховой запас;К заказу;Мин (p10);Макс (p90);Дней в окне;Дней с рекомендацией"]
        for i in items:
            lines.append(";".join([
                str(i["sku"]),
                "" if i["stock"] is None else f"{i['stock']:.0f}",
                f"{i['demand_sum']:.2f}".replace(".", ","),
                "" if i["safety_sum"] is None else f"{i['safety_sum']:.2f}".replace(".", ","),
                "" if i["order_qty"] is None else str(i["order_qty"]),
                "" if i["p10_sum"] is None else f"{i['p10_sum']:.2f}".replace(".", ","),
                "" if i["p90_sum"] is None else f"{i['p90_sum']:.2f}".replace(".", ","),
                str(i["days"]), str(i["days_covered"]),
            ]))
        body = "\ufeff" + "\r\n".join(lines) + "\r\n"  # BOM — 1С/Excel-RU
        return Response(
            content=body.encode("utf-8"),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition":
                     f'attachment; filename="order-recommendations-{window}d.csv"'},
        )

    return {
        **empty_meta,
        "generated_at": rows[0]["generated_at"],
        "first_date": window_dates[0],
        "last_date": window_dates[-1],
        "count": len(items),
        "items": items,
    }


@router.get("/clients/{client_id}/explanation/{sku}")
def get_explanation(
    client_id: str,
    sku: str,
    dataset_id: Optional[str] = None,
    auth: AuthContext = Depends(get_current_client),
):
    """XP-1 #469: «почему такой прогноз» для SKU — top-4 клиентских групп
    факторов (доля |вклада| + направление) и дифф с прошлой моделью.

    Business-фича: остальные тарифы получают 403 — фронт рисует upsell-блок
    (утверждённый эскиз). Вклады пересчитываются post-training'ом; модели,
    обученные до XP-1, объяснений не имеют → 404 с честной причиной (уйдёт
    после следующего обучения; on-demand пересчёт в API-процессе — это
    worker-класс работы, сознательно НЕ делаем).
    """
    require_client_access(client_id, auth)
    record = get_registry().get(client_id)
    if (record.plan if record else None) != "business":
        raise HTTPException(
            403, detail="Объяснение прогноза доступно на тарифе Business")
    if dataset_id is None:
        dataset_id = _default_dataset_id(client_id)
    from src.storage.backend import ClientStorage
    storage = ClientStorage(client_id, dataset_id=dataset_id)
    if not storage.explanations_exist():
        raise HTTPException(
            404, detail="Объяснения появятся после следующего обучения модели")
    df = storage.load_explanations()
    cur = df[df["sku"] == sku]
    if cur.empty:
        raise HTTPException(404, detail="Для этого SKU нет объяснения")

    total_abs = float(cur["contribution"].abs().sum())
    # порог «почти не влияет»: <2% суммарного |вклада|
    eps = 0.02 * total_abs if total_abs > 0 else 0.0

    def _direction(c: float) -> str:
        if c > eps:
            return "up"
        if c < -eps:
            return "down"
        return "flat"

    ranked = cur.reindex(
        cur["contribution"].abs().sort_values(ascending=False).index)
    factors = [{
        "group": r["group"],
        "direction": _direction(float(r["contribution"])),
        "share": (round(abs(float(r["contribution"])) / total_abs, 4)
                  if total_abs > 0 else 0.0),
    } for _, r in ranked.head(4).iterrows()]

    prediction_sum = float(cur["prediction_sum"].iloc[0])
    change = None
    if storage.explanations_prev_exist():
        try:
            prev = storage.load_explanations_prev()
            p = prev[prev["sku"] == sku]
            prev_sum = float(p["prediction_sum"].iloc[0]) if not p.empty else 0.0
            if prev_sum > 0:
                merged = (
                    cur.set_index("group")["contribution"]
                    .subtract(p.set_index("group")["contribution"],
                              fill_value=0.0))
                main = merged.abs().idxmax() if not merged.empty else None
                change = {
                    "pct": round(100.0 * (prediction_sum - prev_sum)
                                 / prev_sum, 1),
                    "main_group": main,
                    "direction": ("up" if main is not None
                                  and float(merged[main]) > 0 else "down"),
                }
        except Exception as e:    # noqa: BLE001 — дифф опционален, факторы важнее
            logger.warning("explanation diff failed for %s/%s: %s",
                           client_id, sku, e)

    return {
        "sku": sku,
        "factors": factors,
        "prediction_sum": round(prediction_sum, 2),
        "heads": int(cur["heads"].iloc[0]),
        "run_id": (str(cur["run_id"].iloc[0])
                   if "run_id" in cur.columns else None),
        "generated_at": (str(cur["generated_at"].iloc[0])
                         if "generated_at" in cur.columns else None),
        "change": change,
    }


@router.get("/clients/{client_id}/anomalies")
def list_anomalies(
    client_id: str,
    sku: Optional[str] = None,
    auth: AuthContext = Depends(get_current_client),
):
    """
    Return historical anomalies (per-SKU IQR + global IsolationForest)
    flagged during the latest training run. Capped to the last 90 days
    of dataset history — older anomalies aren't actionable.
    """
    require_client_access(client_id, auth)
    from src.storage.anomalies import get_anomalies_registry
    rows = get_anomalies_registry().list_for_client(client_id, sku=sku)
    return {
        "client_id": client_id,
        "count":     len(rows),
        "anomalies": rows,
    }


@router.get("/clients/{client_id}/uploads/{upload_id}/skus")
async def list_upload_skus(
    client_id: str,
    upload_id: str,
    auth: AuthContext = Depends(get_current_client),
):
    """
    Return the SKU catalogue of a processed upload — used by the
    forecast UI to populate a dropdown without making the user paste
    history by hand.
    """
    require_client_access(client_id, auth)
    from src.storage import upload_registry as ur
    from src.storage.upload_pipeline import get_processed_path
    from src.data.loader import load_data

    # Audit R4-7 — collapse "not found" + "cross-tenant" → 404 (mirrors R3-1).
    urec = ur.get_upload_registry().get(upload_id)
    if urec is None or urec.client_id != client_id:
        raise HTTPException(404, detail=f"upload_id {upload_id!r} not found")
    if urec.status != ur.PROCESSED:
        raise HTTPException(
            409, detail=f"upload is not processed (status={urec.status})"
        )

    config   = _get_cfg(CONFIG_PATH)
    sku_col  = config["data"]["sku_col"]
    date_col = config["data"]["date_col"]
    # PERF-1 (#186): only the sku + date columns are needed to list SKUs —
    # project them instead of parsing every feature column.
    df = load_data(get_processed_path(urec), config, columns=[sku_col, date_col])

    grouped = df.groupby(sku_col)
    skus: list[dict] = []
    for sku_value, group in grouped:
        skus.append({
            "sku":         str(sku_value),
            "row_count":   int(len(group)),
            "first_date":  pd.Timestamp(group[date_col].min()).strftime("%Y-%m-%d"),
            "last_date":   pd.Timestamp(group[date_col].max()).strftime("%Y-%m-%d"),
        })
    skus.sort(key=lambda s: s["sku"])
    return {"upload_id": upload_id, "count": len(skus), "skus": skus}


# ── DP-4b (#324): «Подготовка данных» — trigger + read-only preview ───────────
# The system auto-maps columns (authoritative); the user only TRIGGERS prep and
# then SEES the result. There is deliberately NO user column-editing surface.

@router.post("/clients/{client_id}/uploads/{upload_id}/prepare")
async def prepare_upload(
    client_id: str,
    upload_id: str,
    auth: AuthContext = Depends(get_current_client),
):
    """«Подготовить» trigger: queue the prep job (sniff + system auto-mapping +
    parse) for a clean upload. Valid only from SCANNED_CLEAN. Idempotent-ish —
    a second call while already processing/processed is a 409."""
    require_client_access(client_id, auth)
    from src.storage import upload_registry as ur
    from src.pipeline.upload_workers import enqueue_prepare

    urec = ur.get_upload_registry().get(upload_id)
    if urec is None or urec.client_id != client_id:
        raise HTTPException(404, detail=f"upload_id {upload_id!r} not found")
    if urec.status != ur.SCANNED_CLEAN:
        raise HTTPException(
            409, detail=f"upload is not ready to prepare (status={urec.status})")

    job_id = enqueue_prepare(upload_id, client_id=client_id)
    return {"upload_id": upload_id, "status": "preparing", "job_id": job_id}


@router.get("/clients/{client_id}/uploads/{upload_id}/prep")
async def get_upload_prep(
    client_id: str,
    upload_id: str,
    auth: AuthContext = Depends(get_current_client),
):
    """READ-ONLY preview for «Подготовка данных». Reports the prep status and,
    once PROCESSED, a small sample of the parsed CANONICAL data + a detection
    summary — so the user can eyeball «это мои данные» and click «Готово,
    использовать». No mapping-editing surface (the system is authoritative)."""
    require_client_access(client_id, auth)
    from src.storage import upload_registry as ur

    urec = ur.get_upload_registry().get(upload_id)
    if urec is None or urec.client_id != client_id:
        raise HTTPException(404, detail=f"upload_id {upload_id!r} not found")

    sniff = urec.sniff_report or {}
    out: dict = {
        "upload_id": upload_id,
        "status": urec.status,
        "filename": urec.filename,
        "detected": {
            "format": sniff.get("format"),
            "encoding": sniff.get("encoding"),
            "delimiter": sniff.get("delimiter"),
        },
        "row_count": urec.row_count,
        "sku_count": urec.sku_count,
        "error_message": urec.error_message,
        "sample": None,
    }
    if urec.status == ur.PROCESSED:
        # Serve the small CANONICAL sample the parser embedded in the manifest
        # (bounded JSON GET) — never re-load the full processed parquet here.
        from src.storage import zones as z
        manifest: dict = {}
        try:
            mkey = z.processed_manifest_key(client_id, upload_id)
            manifest = json.loads(z.get_zone_backend(z.Zone.PROCESSED).download_bytes(mkey))
        except Exception as e:    # noqa: BLE001 — sample is optional; degrade gracefully
            logger.warning("prep preview: manifest read failed for %s (%s)", upload_id, e)
        out["sample"] = {
            "columns": [str(c) for c in manifest.get("columns", [])],
            "rows": manifest.get("sample_rows", []),
        }
    return out


# #184: declared `def` (NOT async) on purpose — the body is entirely synchronous
# (psycopg2 registry read, S3 model load, bcrypt-free, CPU-heavy pandas/ML), so
# FastAPI runs it in the threadpool and the event loop stays free for other
# tenants. predict_batch invokes it via run_in_threadpool for real concurrency.
@router.post("/clients/{client_id}/predict", response_model=PredictResponse)
def predict(
    client_id: str,
    req: PredictRequest,
    auth: AuthContext = Depends(get_current_client),
):
    require_client_access(client_id, auth)
    t_start = time.perf_counter()

    registry = get_registry()
    record   = registry.get(client_id)
    if record is None:
        raise HTTPException(404, detail=f"Client '{client_id}' not found")

    # Per-client rate-limit (audit R2-10, 2026-05-15). Plan-tied:
    # FREE=100/h, START=5000/h, BUSINESS=unlimited. Check happens BEFORE
    # we spend any model-cache work — Redis fails open so a Redis hiccup
    # doesn't brick inference, only the limit silently doesn't apply.
    try:
        plan_spec = get_plan_spec(record.plan)
        check_predict_attempt(client_id, plan_spec.predict_requests_per_hour)
    except RateLimited as e:
        # NC-5 (#250): one inbox row per day (dedup) — friction visible,
        # not a flood; best-effort before the raise.
        from datetime import datetime, timezone
        from src.storage.notifications import emit_notification
        emit_notification(
            client_id, type="quota_warning", severity="warning",
            title="Ограничение тарифа: лимит запросов прогноза",
            body=str(e)[:300],
            dedup_key=f"quota_predict_{datetime.now(timezone.utc).date()}",
        )
        raise HTTPException(
            status_code=429, detail=str(e),
            headers={"Retry-After": str(e.retry_after_sec or 60)},
        )

    try:
        # Модель-аудит H1: /predict строил фичи по СИСТЕМНОМУ конфигу —
        # без клиентских override и план-дефолтов (FX у платных) → mismatch
        # с feature_cols модели → тихий SeasonalNaive-фолбэк.
        from src.clients.config_manager import get_config_manager as _gcm
        config   = _gcm(CONFIG_PATH).get_effective_serving(
            client_id, get_registry())
        requested_horizon = req.horizon or config["model"]["horizon"]
        horizon, clipped  = clip_horizon_to_plan(record, requested_horizon)
        service  = _get_or_load_service(client_id, load_factory=load_service_for_client)

        # Resolve history: either inline payload or pulled from the
        # processed parquet for the given upload_id.
        history = req.history
        if not history and req.upload_id:
            history = _load_processed_for_sku(client_id, req.upload_id, req.sku)

        df = pd.DataFrame(history)
        df["sku"]  = req.sku
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        for col, default in [("price", np.nan), ("promo", 0), ("stock", np.nan)]:
            if col not in df.columns:
                df[col] = default

        # R12-#100 — pin the lag/rolling set to what the loaded model was
        # trained with. /predict is single-SKU: a short-history SKU would
        # otherwise auto-drop the long lags the model expects → KeyError.
        _pin_lags, _pin_rw = serve_feature_set(service.primary)
        df_feat      = build_features(
            df, config,
            pin_lags=_pin_lags or None, pin_rolling=_pin_rw, drop_warmup=False,
        )
        # #229: market-колонки из хвоста модели (no-op для старых pickle) —
        # именно этот single-SKU путь и требует model-carried state:
        # из среза одного SKU «рынок» невычислим.
        from src.features.market import apply_model_market
        df_feat = apply_model_market(service.primary, df_feat,
                                     config["data"]["date_col"])
        # #307: static-колонки (velocity_band, price_tier) — ДО get_feature_columns,
        # иначе feature_cols не включит их, а модель обучена с ними.
        from src.features.static_features import apply_model_static
        df_feat = apply_model_static(service.primary, df_feat,
                                     config["data"]["sku_col"])
        feature_cols = get_feature_columns(df_feat, config)

        # Shared serve dispatcher (R11-#59 / H2): for MIMO/Ensemble it reads
        # the model's direct heads (no recursive compounding); for a true
        # single-step model — or a horizon past the model's trained one, or if
        # direct prediction errors — it drops to recursive_forecast, which
        # recomputes lag/rolling/calendar per step and carries the SeasonalNaive
        # fallback via predict_fn below.
        from src.pipeline.inference_utils import serve_forecast

        model_source = "primary"

        def _wrap_predict(features_df):
            preds, source = service.predict(features_df)
            nonlocal model_source
            if source != "primary":
                model_source = source
                FALLBACK_COUNT.labels(client_id=client_id).inc()
            return preds, source

        forecast_rows = serve_forecast(
            model=service.primary,     # direct path uses the loaded MIMO/Ensemble
            history=df_feat,
            feature_cols=feature_cols,
            horizon=horizon,
            sku=req.sku,
            predict_fn=_wrap_predict,  # recursive-path fallback (single-step / overflow / direct-fail)
            config=config,             # R12-#91: recompute holidays per forecast date
        )

        forecasts      = [round(float(r["predicted_sales"]), 4) for r in forecast_rows]
        forecast_dates = [pd.Timestamp(r["date"]).strftime("%Y-%m-%d") for r in forecast_rows]

        latency = time.perf_counter() - t_start
        REQUEST_LATENCY.labels(client_id=client_id).observe(latency)
        REQUEST_COUNT.labels(client_id=client_id, status="success").inc()

        return PredictResponse(
            sku=req.sku, client_id=client_id,
            forecast=forecasts, forecast_dates=forecast_dates,
            horizon=horizon, model_source=model_source,
            model_name=model_display_name(record),
            horizon_limited_by_plan=clipped,
        )

    except HTTPException:
        REQUEST_COUNT.labels(client_id=client_id, status="error").inc()
        raise
    except Exception:
        REQUEST_COUNT.labels(client_id=client_id, status="error").inc()
        # NEVER put the raw exception into the response — it can leak paths,
        # versions and internals. Full trace goes to logs under a request id.
        import uuid as _uuid
        trace_id = _uuid.uuid4().hex[:12]
        logger.exception(
            f"Predict failed client={client_id} sku={req.sku} trace_id={trace_id}"
        )
        raise HTTPException(
            status_code=500,
            detail=f"internal error (trace_id={trace_id})",
        )


@router.post("/clients/{client_id}/predict/batch")
async def predict_batch(
    client_id: str,
    req: BatchPredictRequest,
    auth: AuthContext = Depends(get_current_client),
):
    """
    Batch inference: many predict requests for one client in parallel.

    Contract (CONTRACT-batch, #186): PARTIAL SUCCESS. The response is
    always 200 with per-item results in request order — a successful item
    carries the same payload as single /predict; a failed item carries
    {"error": {"status", "detail"}} in its slot. Top-level `succeeded` /
    `failed` counters summarise. Rate limiting is work-based (N items =
    N tokens); a rate-limited item errors individually.

    Concurrency: N predict calls run concurrently via `asyncio.gather`
    (vs sequential awaits which made batch latency = N × single).

    Audit R5-2 (2026-05-17): the previous implementation acquired the
    per-client model-cache lock via `await asyncio.to_thread(lk.acquire)`
    around the gather call. That deadlocked on cold-cache batches:
    the outer acquire ran on the executor thread, but each inner
    `predict()` → `_get_service()` → `with _client_lock(client_id):`
    ran on the event-loop thread and blocked forever waiting for the
    same non-reentrant `threading.Lock`. RLock wouldn't help because
    lock ownership is per-thread and the two threads are different.

    Fix: drop the outer batch-wide lock. Each inner predict()
    already takes the per-client lock via service_cache.get_or_load
    with double-checked locking, so:
      - concurrent batches for the same client coalesce on the first
        cold load (subsequent calls hit the populated cache);
      - a racing `/reload` may invalidate the cache mid-batch, in
        which case the next predict in the batch re-fetches —
        same model unless ops explicitly reloaded.
    """
    require_client_access(client_id, auth)

    import asyncio as _asyncio
    # #184: each predict() now runs in the threadpool (run_in_threadpool), so
    # gather gives REAL parallelism across worker threads — not the previous
    # illusory concurrency where the gathered sync coroutines ran serially on a
    # blocked event loop. R5-2 (no outer lock) still holds: inner predict() →
    # get_or_load handles per-client cache coherency across threads.
    #
    # CONTRACT-batch (#186): partial success, not all-or-nothing. With
    # return_exceptions=False the FIRST failing item (a 422 horizon, a 404
    # sku, a 429 rate-limit) destroyed the WHOLE batch — including the items
    # that had already succeeded and the rate-limit tokens they consumed
    # (work spent, nothing returned). Now each item resolves independently:
    # success keeps the exact single-predict payload; failure becomes
    # {"error": {"status", "detail"}} in the same slot (order preserved).
    # Rate limiting stays work-based — N items cost N tokens — but a 429 on
    # item k no longer voids items 1..k-1.
    results = await _asyncio.gather(
        *(run_in_threadpool(predict, client_id, r, auth) for r in req.requests),
        return_exceptions=True,
    )

    payload: list[dict] = []
    succeeded = 0
    for i, res in enumerate(results):
        if isinstance(res, HTTPException):
            payload.append({"error": {"status": res.status_code, "detail": res.detail}})
        elif isinstance(res, BaseException):
            # predict() already maps internal errors to a trace_id'd 500, so
            # this branch is unexpected machinery failure (R11-H5: log the
            # real error server-side, return a generic message).
            logger.error("predict_batch item %d unexpected error: %s",
                         i, res, exc_info=res)
            payload.append({"error": {"status": 500, "detail": "internal error"}})
        else:
            succeeded += 1
            payload.append(res)

    return {
        "client_id": client_id,
        "results":   payload,
        "count":     len(payload),
        "succeeded": succeeded,
        "failed":    len(payload) - succeeded,
    }
