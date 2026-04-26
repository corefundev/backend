"""
src/storage/zones.py

Multi-zone storage for the secure upload pipeline.

Three isolated S3 buckets (or prefixes on the same bucket — configurable):

    ┌──────────────┐   scan   ┌──────────────┐  parse  ┌──────────────┐
    │  untrusted   │ ───────▶ │  quarantine  │ ──────▶ │  processed   │
    └──────────────┘          └──────────────┘         └──────────────┘
       write-only              read+delete              read+write
       from API                from scanner             from sandbox
                               read-only from           + read from
                               sandbox                  training

Access policies (enforced by IAM on the bucket side — this module just
exposes the right client for each zone):

    UNTRUSTED   — API user:    s3:PutObject  only
                  ScanWorker:  s3:GetObject, s3:DeleteObject
    QUARANTINE  — ScanWorker:  s3:PutObject
                  Sandbox:     s3:GetObject (read-only)
                  Cleanup:     s3:DeleteObject
    PROCESSED   — Sandbox:     s3:PutObject
                  Training:    s3:GetObject

If S3_*_BUCKET env vars are not set, falls back to S3_BUCKET with per-zone
prefixes — useful for dev (single MinIO bucket).

Env vars
────────
    S3_UNTRUSTED_BUCKET      or  S3_BUCKET + prefix=_untrusted/
    S3_QUARANTINE_BUCKET     or  S3_BUCKET + prefix=_quarantine/
    S3_PROCESSED_BUCKET      or  S3_BUCKET  (already the default)
    STORAGE_BACKEND=s3|local
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .backend import LocalStorageBackend, S3StorageBackend, StorageBackend

logger = logging.getLogger(__name__)


# ── Safe key validation ────────────────────────────────────────────────────────
#
# User-controlled path components must match this regex. We use it for
# client_id, upload_id, filename — anything that ends up in an S3 key or
# filesystem path.
#
# Rationale: S3 accepts almost anything, but the same key may be mirrored to
# a local filesystem during scan/parse; we must not let "../" or absolute
# paths escape the zone prefix.

SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def validate_component(name: str, field: str = "component") -> str:
    """Validate a single path component. Raises ValueError on bad input."""
    if not isinstance(name, str):
        raise ValueError(f"{field} must be a string, got {type(name).__name__}")
    if not SAFE_COMPONENT_RE.match(name):
        raise ValueError(
            f"Invalid {field}: {name!r}. Must match ^[A-Za-z0-9][A-Za-z0-9._-]{{0,127}}$"
        )
    return name


def safe_join(*parts: str) -> str:
    """Join validated components into an S3 key. Never uses os.path.join."""
    return "/".join(validate_component(p, field=f"path[{i}]") for i, p in enumerate(parts))


# ── Zone enum ──────────────────────────────────────────────────────────────────

class Zone(str, Enum):
    UNTRUSTED  = "untrusted"
    QUARANTINE = "quarantine"
    PROCESSED  = "processed"


@dataclass(frozen=True)
class ZoneConfig:
    """
    Resolved, ready-to-use config for one storage zone. Every zone has its
    own keypair on Beget (their Object Storage issues one access key per
    bucket owner, and we keep the three buckets on separate accounts so a
    breach of one doesn't compromise the other two).
    """
    bucket:            str
    prefix:            str
    endpoint_url:      str | None
    access_key_id:     str | None
    secret_access_key: str | None
    region:            str | None


# ── Factory ────────────────────────────────────────────────────────────────────

def _env(*names: str, default: str | None = None) -> str | None:
    """Return the first env var in `names` that has a non-empty value."""
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return default


def _zone_config(zone: Zone) -> ZoneConfig:
    """
    Resolve per-zone S3 config.

    Lookup order (first non-empty wins):
        S3_<ZONE>_BUCKET         →  S3_BUCKET
        S3_<ZONE>_KEY_PREFIX     →  "" (or "_<zone>" fallback in single-bucket mode)
        S3_<ZONE>_ENDPOINT_URL   →  S3_ENDPOINT_URL
        S3_<ZONE>_ACCESS_KEY_ID  →  AWS_ACCESS_KEY_ID
        S3_<ZONE>_SECRET_ACCESS_KEY → AWS_SECRET_ACCESS_KEY
        S3_<ZONE>_REGION         →  AWS_DEFAULT_REGION

    Missing everything → RuntimeError (explicit; we do NOT silently share
    credentials across zones).
    """
    Z = zone.value.upper()
    bucket = _env(f"S3_{Z}_BUCKET", "S3_BUCKET")
    if not bucket:
        raise RuntimeError(
            f"Cannot resolve zone {zone.value}: set S3_{Z}_BUCKET or S3_BUCKET"
        )

    # When all three zones share one bucket (dev with MinIO), prefix them
    # so they can't see each other's keys. In per-bucket mode the prefix
    # is usually empty.
    explicit_zone_bucket = bool(os.environ.get(f"S3_{Z}_BUCKET"))
    if explicit_zone_bucket:
        prefix = os.environ.get(f"S3_{Z}_KEY_PREFIX", "")
    else:
        prefix = os.environ.get(f"S3_{Z}_KEY_PREFIX", f"_{zone.value}")

    return ZoneConfig(
        bucket            = bucket,
        prefix            = prefix.strip("/"),
        endpoint_url      = _env(f"S3_{Z}_ENDPOINT_URL", "S3_ENDPOINT_URL"),
        access_key_id     = _env(f"S3_{Z}_ACCESS_KEY_ID",  "AWS_ACCESS_KEY_ID"),
        secret_access_key = _env(f"S3_{Z}_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY"),
        region            = _env(f"S3_{Z}_REGION", "AWS_DEFAULT_REGION"),
    )


def get_zone_backend(zone: Zone) -> StorageBackend:
    """
    Return a storage backend scoped to the given zone.

    In S3 mode: an S3StorageBackend constructed with the zone's explicit
    credentials (NO env mutation — safe under concurrency).
    In local mode: a LocalStorageBackend under {ARTIFACTS_DIR}/_{zone}/.
    """
    backend = os.environ.get("STORAGE_BACKEND", "local")

    if backend == "s3":
        cfg = _zone_config(zone)
        return S3StorageBackend(
            bucket            = cfg.bucket,
            prefix            = cfg.prefix,
            endpoint_url      = cfg.endpoint_url,
            access_key_id     = cfg.access_key_id,
            secret_access_key = cfg.secret_access_key,
            region            = cfg.region,
        )

    # local fallback
    base = Path(os.environ.get("ARTIFACTS_DIR", "artifacts")) / f"_{zone.value}"
    return LocalStorageBackend(base_dir=str(base))


# ── Key builders ───────────────────────────────────────────────────────────────
#
# Canonical key shape for all zones:
#
#     <client_id>/<upload_id>/<filename>
#
# All three components are validated against SAFE_COMPONENT_RE before
# interpolation. There is no way to produce a key that escapes the
# client's namespace.

def upload_key(client_id: str, upload_id: str, filename: str) -> str:
    return safe_join(client_id, upload_id, filename)


def processed_key(client_id: str, upload_id: str) -> str:
    """Where the sandbox writes the clean parquet output."""
    return safe_join(client_id, upload_id, "data.parquet")


def processed_manifest_key(client_id: str, upload_id: str) -> str:
    """Metadata about the processed file (row count, schema, hash)."""
    return safe_join(client_id, upload_id, "manifest.json")
