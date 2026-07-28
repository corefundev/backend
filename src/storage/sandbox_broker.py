"""
src/storage/sandbox_broker.py

The ONLY service holding the Docker socket for sandbox work (R11-#66).

Why this exists: process-worker used to bind-mount /var/run/docker.sock
to spawn secure_parser containers. A raw RW socket is host-root-
equivalent — and a path-filtering socket proxy can't fix that, because
container CREATE (which the worker legitimately needs) accepts an
arbitrary HostConfig (privileged, host mounts). This broker inverts the
trust: process-worker has NO Docker access at all and can only ask
"run the standard sandbox profile on a file inside the shared staging
dir". The profile itself is pinned in sandbox.spawn_and_wait — nothing
in the request can change image, mounts, caps or limits.

Deployment (compose `sandbox-broker` service):
  • worker image, command: uvicorn src.storage.sandbox_broker:app
  • mounts: docker.sock + the SAME sandbox staging dir as process-worker
  • network: dedicated internal network shared only with process-worker
  • env: SANDBOX_* knobs only — no DB, no Redis, no Lockbox SA key

Request surface is deliberately tiny: a staging job dir name + the
normalized input filename + bounded parser limits. Both names are
regex-validated and resolved strictly inside SANDBOX_WORK_DIR.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from src.storage.sandbox import SandboxError, spawn_and_wait

logger = logging.getLogger(__name__)

app = FastAPI(title="sandbox-broker", docs_url=None, redoc_url=None, openapi_url=None)

# tempfile.mkdtemp(prefix="job-") output: "job-" + 8 random chars; allow
# some slack but nothing resembling a path.
_JOB_DIR_RE = re.compile(r"^job-[A-Za-z0-9_-]{1,64}$")
# sandbox.py normalizes inputs to "original.<suffix>".
_INPUT_RE = re.compile(r"^original\.[a-z0-9]{1,8}$")

_docker_client = None


def _client():
    global _docker_client
    if _docker_client is None:
        import docker
        _docker_client = docker.from_env()    # type: ignore[attr-defined]
    return _docker_client


def _work_dir() -> Path:
    return Path(os.environ.get("SANDBOX_WORK_DIR", "/var/lib/sku-sandbox"))


class SpawnRequest(BaseModel):
    job_dir:     str
    input_name:  str
    max_rows:    int = Field(default=5_000_000, ge=1, le=50_000_000)
    max_columns: int = Field(default=64, ge=1, le=1024)
    # DP-4 (#324): parser-CLI toggles only — the hardening profile is unaffected.
    # `sniff` emits a report instead of a parquet; `mapping` is a bounded JSON
    # {canonical: source} rename applied before validation. max_length caps abuse;
    # the parser itself json.loads + validates it's a dict of canonical keys.
    sniff:       bool = False
    mapping:     Optional[str] = Field(default=None, max_length=8192)
    # #570: схема валидации — allowlist, брокер не даёт прокинуть произвольный
    # CLI (имя parse_schema: `schema` затеняет BaseModel.schema)
    parse_schema: Optional[Literal["promo-calendar"]] = None


class SpawnResponse(BaseModel):
    exit_code: int
    timed_out: bool
    stdout:    str
    stderr:    str


@app.get("/healthz")
def healthz() -> dict:
    # Docker reachability is part of broker health — a broker that can't
    # spawn is as dead as one that's down, and the compose healthcheck
    # should say so.
    try:
        _client().ping()
    except Exception as e:    # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"docker unreachable: {e}")
    return {"ok": True}


@app.post("/spawn", response_model=SpawnResponse)
def spawn(req: SpawnRequest) -> SpawnResponse:
    if not _JOB_DIR_RE.fullmatch(req.job_dir):
        raise HTTPException(status_code=400, detail="invalid job_dir")
    if not _INPUT_RE.fullmatch(req.input_name):
        raise HTTPException(status_code=400, detail="invalid input_name")

    work_dir = _work_dir().resolve()
    staging = (work_dir / req.job_dir).resolve()
    # Belt over the regex: the resolved staging dir must be a DIRECT
    # child of work_dir (no traversal, no symlink escape).
    if staging.parent != work_dir:
        raise HTTPException(status_code=400, detail="job_dir escapes work_dir")
    in_dir, out_dir = staging / "in", staging / "out"
    if not (in_dir / req.input_name).is_file() or not out_dir.is_dir():
        raise HTTPException(status_code=404, detail="staging layout not found")

    try:
        exit_code, timed_out, stdout, stderr = spawn_and_wait(
            _client(),
            image=os.environ.get("SANDBOX_IMAGE", "sku-forecasting-sandbox"),
            in_dir=in_dir,
            out_dir=out_dir,
            input_name=req.input_name,
            max_rows=req.max_rows,
            max_columns=req.max_columns,
            timeout_sec=int(os.environ.get("SANDBOX_TIMEOUT_SEC", "30")),
            mem_limit=os.environ.get("SANDBOX_MEM_LIMIT", "512m"),
            cpus=float(os.environ.get("SANDBOX_CPUS", "1.0")),
            runtime=os.environ.get("SANDBOX_RUNTIME", "runc"),
            sniff=req.sniff,
            mapping=req.mapping,
            schema=req.parse_schema,
        )
    except SandboxError as e:
        logger.warning("spawn failed for %s: %s", req.job_dir, e)
        raise HTTPException(status_code=502, detail=str(e))

    logger.info(
        "sandbox done: job=%s exit=%d timed_out=%s", req.job_dir, exit_code, timed_out
    )
    return SpawnResponse(
        exit_code=exit_code, timed_out=timed_out, stdout=stdout, stderr=stderr
    )
