"""
src/monitoring/logging_setup.py

Structured JSON logging for the entire platform.
Every log line is a JSON object with consistent fields:
  timestamp, level, logger, message, client_id?, sku?, duration_ms?, ...

Output goes to stdout → Docker logs → Grafana Loki via promtail.

Usage:
    from src.monitoring.logging_setup import configure_structured_logging, get_logger

    configure_structured_logging()   # call once at startup

    logger = get_logger(__name__)
    logger.info("prediction done", extra={
        "client_id": "acme",
        "sku":       "SKU_001",
        "duration_ms": 23,
        "model_type": "lgbm",
    })
"""
from __future__ import annotations

import contextvars
import logging
import os
import sys
import uuid

_CONFIGURED = False


# ── Request correlation (audit R2-13, 2026-05-15) ────────────────
#
# Every log record emitted inside an HTTP request handler picks up the
# request's `trace_id` automatically via this ContextVar. The FastAPI
# middleware in `src.api.main` sets the value at request entry; the
# JsonFormatter pulls it back out and stamps it into every JSON line.
#
# Why ContextVar and not threading.local: ContextVars propagate
# correctly across `asyncio.create_task`, `asyncio.gather`, etc., which
# threading.local does not. Async handlers spawning background tasks
# will still tag their logs with the originating request's trace_id.
#
# For RQ workers: the job payload carries trace_id explicitly (set by
# the enqueue call); workers stamp it into the ContextVar before
# running the job body. Cross-process correlation = FE → API → RQ.
_trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "trace_id", default=""
)


def get_trace_id() -> str:
    """Current request's trace_id, or '' if outside a request."""
    return _trace_id_var.get()


def set_trace_id(trace_id: str) -> contextvars.Token:
    """Set the trace_id for the current async/sync task. Returns a
    Token the caller can pass to `_trace_id_var.reset(token)` to clean
    up — middleware does this on response."""
    return _trace_id_var.set(trace_id)


def reset_trace_id(token: contextvars.Token) -> None:
    """Pair with `set_trace_id()` — clears the value back to the
    previous binding."""
    _trace_id_var.reset(token)


def new_trace_id() -> str:
    """Generate a new short trace_id (12 hex chars from a UUID4)."""
    return uuid.uuid4().hex[:12]


def configure_structured_logging(level: str | None = None) -> None:
    """
    Configure root logger to emit JSON via python-json-logger.
    Call once at application startup.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    log_level = getattr(logging, (level or os.environ.get("LOG_LEVEL", "INFO")).upper(), logging.INFO)

    try:
        from pythonjsonlogger import jsonlogger

        class SKUJsonFormatter(jsonlogger.JsonFormatter):
            def add_fields(self, log_record: dict, record: logging.LogRecord, message_dict: dict) -> None:
                super().add_fields(log_record, record, message_dict)
                log_record["timestamp"] = log_record.pop("asctime", None) or \
                    __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
                log_record["level"]     = record.levelname
                log_record["logger"]    = record.name
                log_record["service"]   = "sku-forecasting"
                # Stamp the current request's trace_id (audit R2-13).
                # Empty string outside a request — Promtail's derived-
                # field rule treats empty trace_id as "no trace" rather
                # than trying to link to Tempo. Manually-set extras
                # (`logger.info(..., extra={"trace_id": "..."})`) win
                # over the ContextVar — that's how RQ workers stamp
                # their inherited trace_id without ever entering an
                # API request context.
                if "trace_id" not in log_record:
                    tid = _trace_id_var.get()
                    if tid:
                        log_record["trace_id"] = tid
                log_record.pop("exc_info", None)

        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(SKUJsonFormatter(
            "%(timestamp)s %(level)s %(logger)s %(message)s"
        ))

        root = logging.getLogger()
        root.setLevel(log_level)
        # Replace all existing handlers
        root.handlers.clear()
        root.addHandler(handler)

        # Quieten noisy third-party loggers
        for noisy in ("uvicorn.access", "urllib3", "botocore", "boto3",
                       "s3transfer", "httpx", "httpcore", "mlflow"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

        _CONFIGURED = True
        logging.getLogger(__name__).info(
            "Structured JSON logging configured",
            extra={"log_level": logging.getLevelName(log_level)},
        )

    except ImportError:
        # Fallback to standard format if python-json-logger not installed
        logging.basicConfig(
            level=log_level,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            stream=sys.stdout,
        )
        _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a configured logger. Use instead of logging.getLogger() for consistency."""
    return logging.getLogger(name)
