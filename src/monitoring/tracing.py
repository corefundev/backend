"""
src/monitoring/tracing.py

OpenTelemetry distributed tracing.
Traces every API request through features → model → storage.

Exporters:
  - Console (dev/debug)
  - OTLP/gRPC → Jaeger or Grafana Tempo (production)

Usage:
    from src.monitoring.tracing import setup_tracing, get_tracer, trace_span

    setup_tracing(app, service_name="sku-forecasting-api")

    tracer = get_tracer(__name__)

    with trace_span("feature_engineering", client_id="acme") as span:
        df = build_features(df, config)
        span.set_attribute("n_features", len(feature_cols))
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Generator, Optional

logger = logging.getLogger(__name__)


def setup_tracing(app=None, service_name: str = "sku-forecasting") -> bool:
    """
    Configure OpenTelemetry tracing.
    Returns True if tracing was configured, False if OTel not available.
    """
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (
            BatchSpanProcessor, ConsoleSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource

        resource = Resource.create({
            "service.name":    service_name,
            "service.version": "5.0.0",
        })
        provider = TracerProvider(resource=resource)

        # Choose exporter based on config
        otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        if otlp_endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
                exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
                logger.info(f"OTel: exporting to {otlp_endpoint}")
            except ImportError:
                exporter = ConsoleSpanExporter()
                logger.info("OTel: OTLP exporter not available, using Console")
        else:
            exporter = ConsoleSpanExporter()
            logger.debug("OTel: no OTEL_EXPORTER_OTLP_ENDPOINT, using Console exporter")

        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)

        # Auto-instrument FastAPI if app provided
        if app is not None:
            try:
                from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
                FastAPIInstrumentor.instrument_app(app)
                logger.info("OTel: FastAPI auto-instrumentation enabled")
            except ImportError:
                pass

        return True

    except ImportError:
        logger.debug("OpenTelemetry not installed — tracing disabled")
        return False
    except Exception as e:
        logger.warning(f"OTel setup failed: {e}")
        return False


def get_tracer(name: str = "sku-forecasting"):
    """Return an OTel tracer. Returns a no-op tracer if OTel not configured."""
    try:
        from opentelemetry import trace
        return trace.get_tracer(name)
    except ImportError:
        return _NoOpTracer()


@contextmanager
def trace_span(
    name: str,
    tracer=None,
    **attributes: Any,
) -> Generator:
    """
    Context manager for a traced span.
    Gracefully no-ops if OTel not configured.

    Usage:
        with trace_span("predict", client_id="acme", sku="SKU_001"):
            result = model.predict(X)
    """
    if tracer is None:
        tracer = get_tracer()

    try:
        with tracer.start_as_current_span(name) as span:
            for k, v in attributes.items():
                try:
                    span.set_attribute(k, str(v))
                except Exception:
                    pass
            yield span
    except AttributeError:
        # No-op tracer
        yield _NoOpSpan()


class _NoOpSpan:
    def set_attribute(self, *a, **kw): pass
    def set_status(self, *a, **kw):    pass
    def record_exception(self, *a, **kw): pass
    def add_event(self, *a, **kw):     pass


class _NoOpTracer:
    from contextlib import contextmanager as _cm

    @contextmanager
    def start_as_current_span(self, name: str, **kw):
        yield _NoOpSpan()
