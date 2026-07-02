"""
src/api/error_handlers.py

App-level exception handlers — one documented error contract (CONTRACT-2, #208).

The three response families the R14 audit flagged, and what this module does
about each:

  1. HTTPException → ``{"detail": "<string>"}``. FastAPI's default, already
     consistent everywhere — UNCHANGED (no handler registered).
  2. RequestValidationError (422) → ``detail`` keeps the standard pydantic
     list byte-for-byte (external API clients parse ``loc``/``msg``/``type``,
     and the FE's errorMessage() already renders it) **plus** an ADDITIVE
     ``message`` string, so every JSON error now carries a ready-to-display
     human string without breaking any existing consumer.
  3. Unhandled Exception → previously Starlette's text/plain
     ``Internal Server Error`` (not JSON, no trace id, breaks any client that
     parses the body). Now ``{"detail": "internal error (trace_id=<id>)"}``
     with the ``X-Request-ID`` header echoed, and the full traceback logged
     server-side only (R11-H5: nothing internal leaks to the tenant).

``/readyz``'s ``{"ready", "checks"}`` payload is NOT an error envelope — it is
the documented health-probe contract (see ops.py) and is deliberately out of
scope here.

Plumbing note: Starlette routes the ``Exception`` handler through
ServerErrorMiddleware, the OUTERMOST ring — outside the trace-id middleware,
whose ``finally`` has already reset the logging ContextVar by the time the
handler runs, and whose response-header stamping never sees this response.
Hence the handler reads ``request.state.trace_id`` (set by the middleware
before calling downstream) and sets ``X-Request-ID`` itself.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

_SUMMARY_MAX_ITEMS = 3


def _human_summary(errors: list) -> str:
    """One display-ready line from pydantic's error list.

    ``body.horizon: Input should be a valid integer; …`` — first three items,
    ``(+N more)`` for the rest. English on purpose: it matches the pydantic
    ``msg`` strings the FE already surfaces from ``detail[0].msg``.
    """
    parts = []
    for e in errors[:_SUMMARY_MAX_ITEMS]:
        loc = ".".join(str(x) for x in e.get("loc", ()))
        msg = e.get("msg", "invalid")
        parts.append(f"{loc}: {msg}" if loc else str(msg))
    more = len(errors) - _SUMMARY_MAX_ITEMS
    tail = f" (+{more} more)" if more > 0 else ""
    return "Validation error — " + "; ".join(parts) + tail


async def _validation_handler(request: Request, exc: Exception):
    # Signature takes Exception (Starlette's add_exception_handler contract);
    # registration guarantees this only ever receives RequestValidationError.
    assert isinstance(exc, RequestValidationError)
    # Mirror FastAPI's default handler exactly (jsonable_encoder over
    # exc.errors() → the same list today's consumers receive), then add the
    # supplementary string. detail's shape must never change (CONTRACT-2).
    errors = jsonable_encoder(exc.errors())
    return JSONResponse(
        status_code=422,
        content={"detail": errors, "message": _human_summary(errors)},
    )


async def _unhandled_handler(request: Request, exc: Exception):
    trace_id = getattr(request.state, "trace_id", None) or "unknown"
    # Full traceback server-side, keyed by trace_id; generic message to the
    # tenant (R11-H5 — mirrors the /predict trace-id discipline).
    logger.error("unhandled exception (trace_id=%s)", trace_id, exc_info=exc)
    response = JSONResponse(
        status_code=500,
        content={"detail": f"internal error (trace_id={trace_id})"},
    )
    response.headers["X-Request-ID"] = trace_id
    return response


def register_error_handlers(app: FastAPI) -> None:
    """Wire the CONTRACT-2 handlers. Called once from main.py."""
    app.add_exception_handler(RequestValidationError, _validation_handler)
    app.add_exception_handler(Exception, _unhandled_handler)
