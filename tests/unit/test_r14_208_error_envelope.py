"""
CONTRACT-2 (#208) — one app-level error contract.

Pre-fix, the API had three inconsistent error families:
  1. HTTPException        → {"detail": "<string>"}           (fine)
  2. pydantic 422         → {"detail": [ {loc,msg,type},… ]} (list, no string)
  3. unhandled Exception  → text/plain "Internal Server Error" (NOT JSON,
                            no trace id — breaks any client parsing the body)

The fix (src/api/error_handlers.py, additive — nothing breaking):
  • 422 keeps `detail` as the standard pydantic list BYTE-COMPATIBLE (external
    API clients parse loc/msg; the FE's errorMessage() renders detail[0].msg)
    and gains a `message` string — every JSON error now has a display-ready
    human string.
  • unhandled exceptions become {"detail": "internal error (trace_id=…)"} +
    X-Request-ID header; the traceback goes to the server log only (R11-H5).
  • HTTPException untouched; /readyz documented as probe-payload, not an
    error envelope.

Handlers are exercised on an isolated FastAPI app (importing main pulls the
vault bootstrap); a static test pins the main.py wiring.
"""
from __future__ import annotations

import re

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel

from src.api.error_handlers import register_error_handlers


class _Body(BaseModel):
    horizon: int
    sku: str


def _make_app() -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)

    # Replicates main.py's trace-id middleware contract: state.trace_id is set
    # BEFORE calling downstream (survives to the outermost Exception handler).
    @app.middleware("http")
    async def _trace(request: Request, call_next):
        incoming = request.headers.get("x-request-id", "").strip()
        request.state.trace_id = incoming or "gen-trace-123"
        return await call_next(request)

    @app.post("/echo")
    def echo(body: _Body):
        return {"ok": True, "horizon": body.horizon}

    @app.get("/boom")
    def boom():
        raise RuntimeError("psycopg2 dsn=postgres://user:SECRET@db/x")

    @app.get("/teapot")
    def teapot():
        raise HTTPException(418, detail="I'm a teapot")

    return app


@pytest.fixture()
def client():
    return TestClient(_make_app(), raise_server_exceptions=False)


# ── family 3: unhandled exception → JSON + trace_id, no leak ─────────────────

def test_unhandled_exception_returns_json_with_trace_id(client):
    r = client.get("/boom", headers={"X-Request-ID": "abc123def456"})
    assert r.status_code == 500
    assert r.headers["content-type"].startswith("application/json")
    assert r.json() == {"detail": "internal error (trace_id=abc123def456)"}
    assert r.headers["X-Request-ID"] == "abc123def456"


def test_unhandled_exception_never_leaks_internals(client):
    r = client.get("/boom")
    assert "SECRET" not in r.text and "psycopg2" not in r.text
    # detail is a STRING (family-1-compatible), with a generated trace id
    assert re.fullmatch(r"internal error \(trace_id=.+\)", r.json()["detail"])


# ── family 2: 422 keeps the pydantic list + gains `message` ──────────────────

def test_422_detail_list_is_byte_compatible_and_message_added(client):
    r = client.post("/echo", json={"horizon": "not-an-int"})
    assert r.status_code == 422
    body = r.json()
    # backward-compat: detail is STILL the standard pydantic list
    assert isinstance(body["detail"], list) and len(body["detail"]) >= 2
    first = body["detail"][0]
    assert "loc" in first and "msg" in first and "type" in first
    # additive: a display-ready human string
    assert isinstance(body["message"], str)
    assert body["message"].startswith("Validation error — ")
    assert "horizon" in body["message"] or "sku" in body["message"]


def test_422_message_caps_at_three_items(client):
    # 2 errors here (both fields missing) → below the cap, no "(+N more)".
    r = client.post("/echo", json={})
    m = r.json()["message"]
    assert "(+" not in m
    assert m.count(";") <= 2


def test_human_summary_truncation_branch():
    from src.api.error_handlers import _human_summary
    errs = [{"loc": ["body", f"f{i}"], "msg": "Field required"} for i in range(5)]
    s = _human_summary(errs)
    assert s.endswith("(+2 more)")
    assert s.count(";") == 2          # exactly 3 items shown
    assert s.startswith("Validation error — body.f0: Field required")


# ── family 1: HTTPException untouched ────────────────────────────────────────

def test_http_exception_envelope_unchanged(client):
    r = client.get("/teapot")
    assert r.status_code == 418
    assert r.json() == {"detail": "I'm a teapot"}


# ── FE-compat: the FE's errorMessage() contract holds for every family ──────

def test_every_family_satisfies_fe_error_message_contract(client):
    """frontend/src/shared/api/client.ts errorMessage(): uses detail if it is
    a string, else detail[0].msg if a list — both must keep working."""
    r500 = client.get("/boom").json()
    assert isinstance(r500["detail"], str)                       # path 1
    r422 = client.post("/echo", json={"horizon": "x"}).json()
    assert isinstance(r422["detail"][0]["msg"], str)             # path 2
    r418 = client.get("/teapot").json()
    assert isinstance(r418["detail"], str)                       # path 1


# ── static wiring: main.py registers the handlers + threads trace_id ─────────

def test_main_py_wires_handlers_and_state_trace_id():
    from pathlib import Path
    src = Path("src/api/main.py").read_text()
    assert "register_error_handlers(app)" in src, (
        "main.py must register the CONTRACT-2 handlers"
    )
    assert "request.state.trace_id = trace_id" in src, (
        "trace-id middleware must expose trace_id via request.state — the "
        "Exception handler runs outside the middleware and needs it"
    )
