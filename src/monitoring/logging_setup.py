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

import logging
import os
import sys
import time
from contextlib import contextmanager
from typing import Any

_CONFIGURED = False


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


@contextmanager
def log_duration(logger: logging.Logger, operation: str, **extra_fields: Any):
    """
    Context manager that logs start and end of an operation with duration.

    Usage:
        with log_duration(logger, "feature_engineering", client_id="acme", n_rows=5000):
            df = build_features(df, config)
    """
    t0 = time.perf_counter()
    logger.debug(f"{operation} started", extra=extra_fields)
    try:
        yield
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        logger.info(
            f"{operation} completed",
            extra={**extra_fields, "duration_ms": elapsed_ms, "status": "ok"},
        )
    except Exception as e:
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        logger.error(
            f"{operation} failed",
            extra={**extra_fields, "duration_ms": elapsed_ms, "status": "error",
                   "error": str(e)},
        )
        raise


class RequestLogger:
    """
    Middleware helper: logs every API request with standard fields.
    Used in FastAPI middleware.
    """

    def __init__(self, logger: logging.Logger):
        self._log = logger

    def log_request(
        self,
        method:     str,
        path:       str,
        status:     int,
        duration_ms: float,
        client_id:  str = "unknown",
        **extra,
    ) -> None:
        level = logging.WARNING if status >= 400 else logging.INFO
        self._log.log(level, "request", extra={
            "http_method":  method,
            "http_path":    path,
            "http_status":  status,
            "duration_ms":  round(duration_ms, 1),
            "client_id":    client_id,
            **extra,
        })
