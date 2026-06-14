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

import logging
import time
from typing import Optional

import numpy as np
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.api.loaders import CONFIG_PATH, load_service_for_client
from src.api.metrics import FALLBACK_COUNT, REQUEST_COUNT, REQUEST_LATENCY
from src.api.service_cache import get_or_load as _get_or_load_service
from src.auth.jwt_auth import AuthContext, get_current_client, require_client_access
from src.auth.signup_rate_limit import RateLimited, check_predict_attempt
from src.clients.registry import get_registry
from src.features.engineering import build_features, get_feature_columns
from src.pipeline.inference_utils import get_config as _get_cfg
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
    df = load_data(path, config)

    sku_col  = config["data"]["sku_col"]
    date_col = config["data"]["date_col"]
    target   = config["data"]["target_col"]
    if sku_col not in df.columns:
        raise HTTPException(500, detail=f"processed parquet missing column '{sku_col}'")

    sub = df[df[sku_col] == sku]
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

@router.get("/clients/{client_id}/forecasts")
async def list_forecasts(
    client_id: str,
    sku: Optional[str] = None,
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
    rows = get_forecasts_registry().list_for_client(client_id, sku=sku)
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
    by_sku: dict[str, list[tuple[str, float, "float | None", "float | None"]]] = defaultdict(list)
    for r in rows:
        by_sku[r["sku"]].append((r["forecast_date"], r["value"], r.get("p10"), r.get("p90")))

    all_dates  = sorted({fd for points in by_sku.values() for (fd, *_) in points})
    first_date = all_dates[0]
    last_date  = all_dates[-1]
    generated  = rows[0]["generated_at"]

    skus_payload = []
    for sku_name in sorted(by_sku.keys()):
        date_to_value: dict[str, float] = {}
        date_to_p10:   dict[str, "float | None"] = {}
        date_to_p90:   dict[str, "float | None"] = {}
        for (fd, v, p10, p90) in by_sku[sku_name]:
            date_to_value[fd] = v
            date_to_p10[fd]   = p10
            date_to_p90[fd]   = p90
        # Fill missing dates with None — clients can render gaps.
        values = [date_to_value.get(d) for d in all_dates]
        p10s   = [date_to_p10.get(d)   for d in all_dates]
        p90s   = [date_to_p90.get(d)   for d in all_dates]
        skus_payload.append({
            "sku":    sku_name,
            "values": values,
            "p10":    p10s,
            "p90":    p90s,
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


@router.get("/clients/{client_id}/anomalies")
async def list_anomalies(
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
    df = load_data(get_processed_path(urec), config)

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


@router.post("/clients/{client_id}/predict", response_model=PredictResponse)
async def predict(
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
        raise HTTPException(
            status_code=429, detail=str(e),
            headers={"Retry-After": str(e.retry_after_sec or 60)},
        )

    try:
        config   = _get_cfg(CONFIG_PATH)
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

        df_feat      = build_features(df, config)
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
    # No outer lock — R5-2 deadlock fix. Inner predict()->get_or_load
    # handles cache coherency.
    results = await _asyncio.gather(
        *(predict(client_id, r, auth) for r in req.requests),
        return_exceptions=False,
    )

    return {"client_id": client_id, "results": list(results), "count": len(results)}
