#!/usr/bin/env python3
"""
resign_models.py — rebuild the HMAC envelope on every model.pkl in the
processed S3 zone when MODEL_SIGNING_KEY rotates.

Why this exists
───────────────
src/storage/backend.py wraps pickle payloads with:

    SKUSIG1 | HMAC-SHA256(key, payload) | pickle_bytes

If MODEL_SIGNING_KEY is changed without re-signing, every `load_pickle()`
call will raise `InvalidSignature`, the `/predict` endpoint returns 500
for every client, and the only fix is either rolling back the key or
re-training every client's model (slow).

This script walks the processed zone, reads each model.pkl (or fallback.pkl),
verifies it against the OLD key, and writes a fresh blob signed with the
NEW key. Runs in read-mostly fashion — the only writes are the rewritten
model.pkl and fallback.pkl.

Usage
─────
    MODEL_SIGNING_KEY_OLD=<hex or text>  \\
    MODEL_SIGNING_KEY_NEW=<hex or text>  \\
    STORAGE_BACKEND=s3                   \\
    S3_PROCESSED_BUCKET=...              \\
    S3_PROCESSED_ACCESS_KEY_ID=...       \\
    S3_PROCESSED_SECRET_ACCESS_KEY=...   \\
    S3_PROCESSED_ENDPOINT_URL=...        \\
    python scripts/resign_models.py [--dry-run]

Flags
─────
    --dry-run   List what would be resigned, no writes. Highly recommended
                first run.
    --client    Only process one client_id (useful for staged rollout).

Safety
──────
    • The script never deletes anything.
    • A rewrite is atomic at the S3 key level (PUT is single-request).
    • If verification against OLD fails for a key, it's reported and SKIPPED
      — the key may already be re-signed from a previous partial run, or
      signed with a third (even older) key.

When to rotate MODEL_SIGNING_KEY
────────────────────────────────
    1. Existing key leaked (scp history, disk image).
    2. Compliance window expired (PCI-DSS 3.6.4 → ≤ 90 days).
    3. Staff offboarding — someone with vault/console access left.

Run order (for full zero-downtime rotation):
    1. Generate NEW key, keep OLD key safely.
    2. Run this script with both → all models re-signed.
    3. Update MODEL_SIGNING_KEY in Lockbox/Vault to NEW value.
    4. Restart api + worker containers; they pick up NEW key.
    5. Verify /predict works for several clients.
    6. After 24h observation, delete OLD key from the password manager.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import io
import logging
import os
import sys
from typing import Iterable


# ─── HMAC envelope (mirrors src/storage/backend.py) ────────────────────────
_SIG_MAGIC = b"SKUSIG1"
_SIG_LEN   = 32


def _coerce_key(s: str) -> bytes:
    """Env value may be hex or literal bytes — mirror backend.py._signing_key."""
    try:
        return bytes.fromhex(s)
    except ValueError:
        return s.encode("utf-8")


def _verify(data: bytes, key: bytes) -> bytes:
    """Return the inner pickle payload if HMAC matches, else raise."""
    if not data.startswith(_SIG_MAGIC):
        raise ValueError("blob is not in SKUSIG1 format — skip")
    sig     = data[len(_SIG_MAGIC): len(_SIG_MAGIC) + _SIG_LEN]
    payload = data[len(_SIG_MAGIC) + _SIG_LEN:]
    expected = hmac.new(key, payload, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        raise ValueError("HMAC mismatch — possibly already resigned with NEW")
    return payload


def _sign(payload: bytes, key: bytes) -> bytes:
    digest = hmac.new(key, payload, hashlib.sha256).digest()
    return _SIG_MAGIC + digest + payload


# ─── S3 walking ───────────────────────────────────────────────────────────

def _client():
    """Construct an S3 client from S3_PROCESSED_* env (not the shared one)."""
    try:
        import boto3
    except ImportError:
        print("ERROR: boto3 required (pip install boto3)", file=sys.stderr)
        sys.exit(2)

    bucket = os.environ.get("S3_PROCESSED_BUCKET") or os.environ.get("S3_BUCKET")
    if not bucket:
        print("ERROR: S3_PROCESSED_BUCKET / S3_BUCKET not set", file=sys.stderr)
        sys.exit(2)

    session = boto3.session.Session(
        aws_access_key_id     = os.environ.get("S3_PROCESSED_ACCESS_KEY_ID")     or os.environ.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key = os.environ.get("S3_PROCESSED_SECRET_ACCESS_KEY") or os.environ.get("AWS_SECRET_ACCESS_KEY"),
    )
    s3 = session.client(
        "s3",
        endpoint_url = os.environ.get("S3_PROCESSED_ENDPOINT_URL") or os.environ.get("S3_ENDPOINT_URL"),
        region_name  = os.environ.get("S3_PROCESSED_REGION")       or os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
    )
    return s3, bucket


def _iter_model_keys(s3, bucket: str, client_filter: str | None) -> Iterable[str]:
    """Yield every S3 key that looks like a model artifact."""
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not (key.endswith("/model.pkl") or key.endswith("/fallback.pkl")):
                continue
            if client_filter and not key.startswith(f"{client_filter}/"):
                continue
            yield key


# ─── Main ─────────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--dry-run", action="store_true", help="list keys, no writes")
    p.add_argument("--client",  default=None,        help="limit to one client_id")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    old_env = os.environ.get("MODEL_SIGNING_KEY_OLD")
    new_env = os.environ.get("MODEL_SIGNING_KEY_NEW")
    if not old_env or not new_env:
        print("ERROR: set MODEL_SIGNING_KEY_OLD and MODEL_SIGNING_KEY_NEW", file=sys.stderr)
        return 2
    old_key = _coerce_key(old_env)
    new_key = _coerce_key(new_env)
    if old_key == new_key:
        print("ERROR: OLD == NEW, nothing to do", file=sys.stderr)
        return 2

    s3, bucket = _client()
    print(f"Bucket: {bucket}{'  (DRY RUN)' if args.dry_run else ''}")
    if args.client:
        print(f"Filter: client={args.client}")

    n_ok = n_skip = n_err = 0
    for key in _iter_model_keys(s3, bucket, args.client):
        try:
            body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        except Exception as e:
            print(f"  ERR  {key}: cannot read — {e}")
            n_err += 1
            continue

        try:
            payload = _verify(body, old_key)
        except ValueError as e:
            # Check whether it's already signed with NEW (partial prior run)
            try:
                _verify(body, new_key)
                print(f"  skip {key}: already signed with NEW")
                n_skip += 1
            except ValueError:
                print(f"  ERR  {key}: {e}")
                n_err += 1
            continue

        resigned = _sign(payload, new_key)

        if args.dry_run:
            print(f"  DRY  {key} ({len(body)} B → {len(resigned)} B)")
            n_ok += 1
            continue

        try:
            s3.put_object(Bucket=bucket, Key=key, Body=resigned)
            print(f"  ok   {key}")
            n_ok += 1
        except Exception as e:
            print(f"  ERR  {key}: write — {e}")
            n_err += 1

    print("\n" + "═" * 60)
    print(f"  done: {n_ok} re-signed, {n_skip} already new, {n_err} errors")
    return 1 if n_err else 0


if __name__ == "__main__":
    sys.exit(main())
