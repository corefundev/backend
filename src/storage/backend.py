"""
src/storage/backend.py

Unified storage abstraction: local filesystem OR S3-compatible storage.
Switch via STORAGE_BACKEND env var: "local" (default) or "s3".

S3 configuration via env vars:
    AWS_ACCESS_KEY_ID
    AWS_SECRET_ACCESS_KEY
    AWS_DEFAULT_REGION       (default: us-east-1)
    S3_ENDPOINT_URL          (optional: for MinIO / custom S3)
    S3_BUCKET                (required when backend=s3)

Usage:
    storage = get_storage()
    storage.upload(local_path, "client_a/models/model.pkl")
    storage.download("client_a/models/model.pkl", local_path)
    storage.exists("client_a/models/model.pkl")
    url = storage.path("client_a/models/model.pkl")
    # → s3://my-bucket/client_a/models/model.pkl  or  /artifacts/client_a/models/model.pkl
"""
from __future__ import annotations

import hashlib
import hmac
import io
import logging
import os
import pickle
import shutil
from abc import ABC, abstractmethod
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# HMAC-signed artifacts
#
# pickle.loads on attacker-controlled bytes is RCE. We never want to deserialize
# a model file unless we wrote it ourselves. To prove provenance without adding
# PKI, every save_pickle() attaches an HMAC-SHA256 over the payload:
#
#     stored_bytes = b"SKUSIG1" + signature (32 bytes) + pickle_data
#
# load_pickle() verifies the signature before passing bytes to pickle.loads.
# If MODEL_SIGNING_KEY is unset, we log a loud warning and fall back to
# unsigned IO — this keeps dev ergonomics sane but NEVER ship prod without it.
# ══════════════════════════════════════════════════════════════════════════════

_SIG_MAGIC = b"SKUSIG1"
_SIG_LEN   = 32   # SHA-256 digest length


def _signing_key() -> bytes | None:
    key = os.environ.get("MODEL_SIGNING_KEY") or os.environ.get("JWT_SECRET_KEY")
    if not key:
        return None
    # Allow hex-encoded keys too (common output of secrets.token_hex).
    try:
        return bytes.fromhex(key)
    except ValueError:
        return key.encode("utf-8")


def _sign(payload: bytes) -> bytes:
    key = _signing_key()
    if key is None:
        logger.warning(
            "MODEL_SIGNING_KEY not set — storing unsigned pickle. "
            "Set MODEL_SIGNING_KEY for tamper protection."
        )
        return payload
    digest = hmac.new(key, payload, hashlib.sha256).digest()
    return _SIG_MAGIC + digest + payload


def _verify(data: bytes) -> bytes:
    """
    Return the raw pickle payload if the HMAC checks out (or if signing is
    disabled and the blob is unsigned). Raises ValueError on bad signature.
    """
    if not data.startswith(_SIG_MAGIC):
        # Unsigned blob — permitted only if no key configured (legacy / dev).
        if _signing_key() is not None:
            raise ValueError("refusing to load unsigned pickle — expected SKUSIG1 header")
        return data

    key = _signing_key()
    if key is None:
        raise ValueError("signed pickle found but MODEL_SIGNING_KEY not configured")

    sig     = data[len(_SIG_MAGIC) : len(_SIG_MAGIC) + _SIG_LEN]
    payload = data[len(_SIG_MAGIC) + _SIG_LEN :]
    expected = hmac.new(key, payload, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        raise ValueError("HMAC mismatch — pickle has been tampered with")
    return payload


# ── Abstract interface ────────────────────────────────────────────────────────

class StorageBackend(ABC):

    @abstractmethod
    def upload(self, local_path: str | Path, remote_key: str) -> None:
        """Upload local file to storage at remote_key."""

    @abstractmethod
    def download(self, remote_key: str, local_path: str | Path) -> None:
        """Download remote_key to local_path."""

    @abstractmethod
    def upload_bytes(self, data: bytes, remote_key: str) -> None:
        """Upload raw bytes to remote_key."""

    @abstractmethod
    def download_bytes(self, remote_key: str) -> bytes:
        """Return content of remote_key as bytes."""

    @abstractmethod
    def exists(self, remote_key: str) -> bool:
        """Return True if remote_key exists."""

    @abstractmethod
    def list_keys(self, prefix: str) -> list[str]:
        """List all keys under prefix."""

    @abstractmethod
    def delete(self, remote_key: str) -> None:
        """Delete remote_key."""

    @abstractmethod
    def path(self, remote_key: str) -> str:
        """Return human-readable full path (s3://... or /abs/path)."""

    # ── Convenience helpers ───────────────────────────────────────────────────

    def save_pickle(self, obj: object, remote_key: str) -> None:
        """
        Serialize `obj` with pickle, wrap with an HMAC-SHA256 header, upload.
        Requires MODEL_SIGNING_KEY in env for signed output; without it,
        writes legacy unsigned blob + logs a warning.
        """
        payload = pickle.dumps(obj)
        self.upload_bytes(_sign(payload), remote_key)
        logger.info(f"Saved pickle → {self.path(remote_key)}")

    def load_pickle(self, remote_key: str) -> object:
        """
        Verify HMAC (when MODEL_SIGNING_KEY is set), then pickle.loads.
        Raises ValueError on tampered or missing signature in signed mode.
        """
        data = self.download_bytes(remote_key)
        verified = _verify(data)
        return pickle.loads(verified)

    def save_dataframe(self, df: pd.DataFrame, remote_key: str) -> None:
        buf = io.BytesIO()
        df.to_parquet(buf, index=False)
        self.upload_bytes(buf.getvalue(), remote_key)
        logger.info(f"Saved DataFrame ({len(df)} rows) → {self.path(remote_key)}")

    def load_dataframe(self, remote_key: str) -> pd.DataFrame:
        data = self.download_bytes(remote_key)
        return pd.read_parquet(io.BytesIO(data))


# ── Local filesystem backend ──────────────────────────────────────────────────

class LocalStorageBackend(StorageBackend):

    def __init__(self, base_dir: str = "artifacts"):
        self.base = Path(base_dir).resolve()
        self.base.mkdir(parents=True, exist_ok=True)
        logger.info(f"LocalStorageBackend: base={self.base}")

    def _full(self, remote_key: str) -> Path:
        # Path traversal guard: resolve() collapses ".." segments, then we
        # require the result to live inside self.base. Rejects remote_key
        # values like "../etc/passwd" or "/absolute/path".
        if not remote_key or remote_key.startswith("/"):
            raise ValueError(f"Invalid remote_key: {remote_key!r}")
        candidate = (self.base / remote_key).resolve()
        try:
            candidate.relative_to(self.base)
        except ValueError:
            raise ValueError(
                f"remote_key {remote_key!r} escapes storage root"
            ) from None
        candidate.parent.mkdir(parents=True, exist_ok=True)
        return candidate

    def upload(self, local_path: str | Path, remote_key: str) -> None:
        shutil.copy2(local_path, self._full(remote_key))

    def download(self, remote_key: str, local_path: str | Path) -> None:
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self._full(remote_key), local_path)

    def upload_bytes(self, data: bytes, remote_key: str) -> None:
        self._full(remote_key).write_bytes(data)

    def download_bytes(self, remote_key: str) -> bytes:
        return self._full(remote_key).read_bytes()

    def exists(self, remote_key: str) -> bool:
        return self._full(remote_key).exists()

    def list_keys(self, prefix: str) -> list[str]:
        base = self.base / prefix
        if not base.exists():
            return []
        return [
            str(p.relative_to(self.base))
            for p in base.rglob("*")
            if p.is_file()
        ]

    def delete(self, remote_key: str) -> None:
        p = self._full(remote_key)
        if p.exists():
            p.unlink()

    def path(self, remote_key: str) -> str:
        return str(self.base / remote_key)


# ── S3 backend ────────────────────────────────────────────────────────────────

class S3StorageBackend(StorageBackend):
    """
    AWS S3 or S3-compatible (MinIO, Beget) backend.

    Credentials / endpoint / bucket can be supplied three ways (in priority
    order):

      1. Constructor kwargs (`S3StorageBackend(bucket=..., access_key_id=...)`).
         Used by `zones.get_zone_backend()` so each zone can point at an
         independent Beget S3 account — Beget issues ONE keypair per bucket,
         so we need per-zone creds.

      2. Env vars (classic path, matches docker-compose x-common-env):
            S3_BUCKET, S3_KEY_PREFIX, S3_ENDPOINT_URL,
            AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_DEFAULT_REGION.

      3. Defaults (boto3 instance-profile / ~/.aws/credentials). For a
         cluster with IAM role-based auth.

    Requires: boto3, botocore
    """

    def __init__(
        self,
        *,
        bucket:             str | None = None,
        prefix:             str | None = None,
        endpoint_url:       str | None = None,
        access_key_id:      str | None = None,
        secret_access_key:  str | None = None,
        region:             str | None = None,
    ):
        try:
            import boto3
        except ImportError:
            raise ImportError("boto3 is required for S3 backend. pip install boto3")

        self.bucket = bucket or os.environ.get("S3_BUCKET")
        if not self.bucket:
            raise RuntimeError("S3StorageBackend: bucket not supplied and S3_BUCKET is empty")
        self.prefix   = (prefix if prefix is not None else os.environ.get("S3_KEY_PREFIX", "")).rstrip("/")
        endpoint      = endpoint_url or os.environ.get("S3_ENDPOINT_URL")
        access_key    = access_key_id or os.environ.get("AWS_ACCESS_KEY_ID")
        secret_key    = secret_access_key or os.environ.get("AWS_SECRET_ACCESS_KEY")
        region        = region or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

        # If caller gave us explicit creds we build our own session instead
        # of relying on the process-global boto3 search chain. Otherwise we
        # fall back to whatever boto3 finds (env / profile / metadata).
        session_kwargs = {}
        if access_key and secret_key:
            session_kwargs["aws_access_key_id"]     = access_key
            session_kwargs["aws_secret_access_key"] = secret_key
        session = boto3.session.Session(**session_kwargs)
        self._s3 = session.client(
            "s3",
            endpoint_url=endpoint,
            region_name=region,
        )
        logger.info(
            "S3StorageBackend: bucket=%s prefix=%r endpoint=%s key_src=%s",
            self.bucket, self.prefix, endpoint,
            "explicit" if access_key else "env/profile",
        )

    def _key(self, remote_key: str) -> str:
        return f"{self.prefix}/{remote_key}" if self.prefix else remote_key

    def upload(self, local_path: str | Path, remote_key: str) -> None:
        self._s3.upload_file(str(local_path), self.bucket, self._key(remote_key))
        logger.info(f"S3 upload: {local_path} → {self.path(remote_key)}")

    def download(self, remote_key: str, local_path: str | Path) -> None:
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        self._s3.download_file(self.bucket, self._key(remote_key), str(local_path))

    def upload_bytes(self, data: bytes, remote_key: str) -> None:
        self._s3.put_object(Bucket=self.bucket, Key=self._key(remote_key), Body=data)

    def download_bytes(self, remote_key: str) -> bytes:
        resp = self._s3.get_object(Bucket=self.bucket, Key=self._key(remote_key))
        return resp["Body"].read()

    def exists(self, remote_key: str) -> bool:
        try:
            self._s3.head_object(Bucket=self.bucket, Key=self._key(remote_key))
            return True
        except Exception as e:
            # Audit R2-26 (2026-05-15): proper ClientError-code matching
            # instead of brittle `"404" in str(e)`. The string form is
            # not stable across boto3 versions and S3-compatible
            # backends (Beget vs Minio vs AWS); the .response['Error']
            # ['Code'] field is the contract.
            try:
                from botocore.exceptions import ClientError
                if isinstance(e, ClientError):
                    code = e.response.get("Error", {}).get("Code", "")
                    if code in ("404", "NoSuchKey", "NoSuchBucket"):
                        return False
            except ImportError:
                pass  # botocore not available — fall through to raise
            raise

    def list_keys(self, prefix: str) -> list[str]:
        full_prefix = self._key(prefix)
        paginator = self._s3.get_paginator("list_objects_v2")
        keys = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=full_prefix):
            for obj in page.get("Contents", []):
                # Strip our internal prefix to return clean relative keys
                k = obj["Key"]
                if self.prefix:
                    k = k[len(self.prefix) + 1:]
                keys.append(k)
        return keys

    def delete(self, remote_key: str) -> None:
        self._s3.delete_object(Bucket=self.bucket, Key=self._key(remote_key))

    def path(self, remote_key: str) -> str:
        return f"s3://{self.bucket}/{self._key(remote_key)}"


# ── Factory ───────────────────────────────────────────────────────────────────

def get_storage(backend: str | None = None) -> StorageBackend:
    """
    Return a storage backend instance.
    backend: "local" | "s3" | None (auto-detect from STORAGE_BACKEND env var)
    """
    backend = backend or os.environ.get("STORAGE_BACKEND", "local")
    if backend == "s3":
        return S3StorageBackend()
    base_dir = os.environ.get("ARTIFACTS_DIR", "artifacts")
    return LocalStorageBackend(base_dir=base_dir)


# ── Client-scoped storage paths ───────────────────────────────────────────────

class ClientStorage:
    """
    Scoped storage for a specific client.
    All keys are namespaced under client_id/.

    Directory layout (mirrors S3 structure from ТЗ §17.7):
        {client_id}/raw/
        {client_id}/features/
        {client_id}/models/
        {client_id}/predictions/
        {client_id}/metrics/
    """

    RAW_DATA_KEY   = "{client_id}/raw/data.parquet"
    FEATURES_KEY   = "{client_id}/features/features.parquet"
    MODEL_KEY      = "{client_id}/models/model.pkl"
    FALLBACK_KEY   = "{client_id}/models/fallback.pkl"
    PREDS_KEY      = "{client_id}/predictions/{date}.parquet"
    METRICS_KEY    = "{client_id}/metrics/per_sku.csv"
    DRIFT_KEY      = "{client_id}/metrics/drift.parquet"

    def __init__(self, client_id: str, backend: StorageBackend | None = None):
        self.client_id = client_id
        self.backend = backend or get_storage()

    def _k(self, template: str, **kwargs) -> str:
        return template.format(client_id=self.client_id, **kwargs)

    # ── Model ─────────────────────────────────────────────────────────────────

    def save_model(self, model_obj: object) -> str:
        key = self._k(self.MODEL_KEY)
        self.backend.save_pickle(model_obj, key)
        return self.backend.path(key)

    def load_model(self) -> object:
        key = self._k(self.MODEL_KEY)
        return self.backend.load_pickle(key)

    def model_exists(self) -> bool:
        return self.backend.exists(self._k(self.MODEL_KEY))

    def save_fallback_model(self, model_obj: object) -> None:
        self.backend.save_pickle(model_obj, self._k(self.FALLBACK_KEY))

    def load_fallback_model(self) -> object:
        return self.backend.load_pickle(self._k(self.FALLBACK_KEY))

    def fallback_exists(self) -> bool:
        return self.backend.exists(self._k(self.FALLBACK_KEY))

    # ── Data ──────────────────────────────────────────────────────────────────

    def save_raw_data(self, df: pd.DataFrame) -> None:
        self.backend.save_dataframe(df, self._k(self.RAW_DATA_KEY))

    def load_raw_data(self) -> pd.DataFrame:
        return self.backend.load_dataframe(self._k(self.RAW_DATA_KEY))

    def save_features(self, df: pd.DataFrame) -> None:
        self.backend.save_dataframe(df, self._k(self.FEATURES_KEY))

    def load_features(self) -> pd.DataFrame:
        return self.backend.load_dataframe(self._k(self.FEATURES_KEY))

    # ── Predictions ───────────────────────────────────────────────────────────

    def save_predictions(self, df: pd.DataFrame, date: str) -> str:
        key = self._k(self.PREDS_KEY, date=date)
        self.backend.save_dataframe(df, key)
        return self.backend.path(key)

    def load_predictions(self, date: str) -> pd.DataFrame:
        return self.backend.load_dataframe(self._k(self.PREDS_KEY, date=date))

    # ── Metrics ───────────────────────────────────────────────────────────────

    def save_per_sku_metrics(self, df: pd.DataFrame) -> None:
        import io
        buf = io.BytesIO()
        df.to_csv(buf, index=False)
        self.backend.upload_bytes(buf.getvalue(), self._k(self.METRICS_KEY))

    def load_per_sku_metrics(self) -> pd.DataFrame:
        import io
        data = self.backend.download_bytes(self._k(self.METRICS_KEY))
        return pd.read_csv(io.BytesIO(data))

    def save_drift(self, df: pd.DataFrame) -> None:
        self.backend.save_dataframe(df, self._k(self.DRIFT_KEY))

    def load_drift(self) -> pd.DataFrame | None:
        key = self._k(self.DRIFT_KEY)
        if not self.backend.exists(key):
            return None
        return self.backend.load_dataframe(key)

    def path(self, subkey: str) -> str:
        return self.backend.path(f"{self.client_id}/{subkey}")
