"""
src/auth/lockbox_agent.py

Yandex Lockbox secret fetcher.

Cloud-native alternative to HashiCorp Vault on the Yandex Cloud stack.
Rationale:
  • Vault has no native seal backend for YC KMS — forcing anything
    Vault-y on YC means running a second Vault for Transit seal, or
    relying on manual unseal after every reboot.
  • YC Lockbox is the local equivalent of AWS Secrets Manager: you
    read a versioned, access-controlled JSON blob over REST, auth'd
    by an IAM token. No seal semantics, no operator work after reboot.

Authentication chain
────────────────────
1. Read the service-account (SA) authorised key from disk — a JSON
   file generated via `yc iam key create --service-account-name ...`
   that contains `id`, `service_account_id`, and a PEM `private_key`.
2. Build a JWT (PS256) signed with that private key.
3. POST the JWT to https://iam.api.cloud.yandex.net/iam/v1/tokens
   → receive an IAM token (TTL ~12 h).
4. GET https://payload.lockbox.api.cloud.yandex.net/lockbox/v1/secrets/<id>/payload
   with `Authorization: Bearer <IAM token>`.

Injection into the app
──────────────────────
The Lockbox payload is a list of `{key, text_value | binary_value}`.
We uppercase `key` and set it in `os.environ` — same contract that
`vault_agent.bootstrap_secrets()` uses, so `src/api/main.py` doesn't
need to distinguish sources.

Env knobs
─────────
    YC_SA_KEY_FILE   — path to the SA authorised-key JSON (read-only mount).
    YC_LOCKBOX_SECRET_ID  — the secret's id (looks like e6qxxx…).
    YC_LOCKBOX_ENDPOINT   — default "payload.lockbox.api.cloud.yandex.net".
    YC_IAM_ENDPOINT       — default "iam.api.cloud.yandex.net".

The SA must have IAM role `lockbox.payloadViewer` on the folder that
owns the secret. Nothing else — least privilege.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────

IAM_ENDPOINT_DEFAULT     = "iam.api.cloud.yandex.net"
LOCKBOX_ENDPOINT_DEFAULT = "payload.lockbox.api.cloud.yandex.net"
JWT_AUDIENCE             = "https://iam.api.cloud.yandex.net/iam/v1/tokens"
JWT_LIFETIME_SEC         = 3600     # max accepted by YC IAM


# ── Exceptions ───────────────────────────────────────────────────────────

class LockboxError(RuntimeError):
    """Raised when Lockbox cannot be reached or the SA cannot auth."""


# ── JWT helpers (PS256, no external dep) ─────────────────────────────────
#
# We avoid `PyJWT` to keep the bootstrap surface minimal — PyJWT's own
# crypto backend pulls in cryptography, which is already in our deps for
# Fernet, but making this module usable from the Dockerfile.worker too
# means we want a tight import graph.

def _b64url(data: bytes) -> bytes:
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def _build_jwt(sa_key: dict, now: int) -> str:
    """
    Sign a JWT using PS256 with the SA's RSA private key.

    `sa_key` is the YC-issued JSON with keys: `id`, `service_account_id`,
    `private_key` (PEM string).
    """
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError as e:
        raise LockboxError(
            "cryptography package required for Lockbox bootstrap"
        ) from e

    header  = {"typ": "JWT", "alg": "PS256", "kid": sa_key["id"]}
    payload = {
        "iss": sa_key["service_account_id"],
        "aud": JWT_AUDIENCE,
        "iat": now,
        "exp": now + JWT_LIFETIME_SEC,
    }

    header_b64  = _b64url(json.dumps(header,  separators=(",", ":")).encode())
    payload_b64 = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    signing_in  = header_b64 + b"." + payload_b64

    # YC service-account keys are RSA-2048 by spec; narrow the union
    # the loader returns so .sign() resolves to the RSA overload that
    # accepts (data, padding, algorithm). Other key types (Ed25519,
    # DH, etc.) have a different .sign() signature and are rejected
    # at runtime by Lockbox anyway.
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
    priv = serialization.load_pem_private_key(
        sa_key["private_key"].encode(), password=None
    )
    if not isinstance(priv, RSAPrivateKey):
        raise LockboxError(
            f"YC SA key must be RSA, got {type(priv).__name__}"
        )
    sig = priv.sign(
        signing_in,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
        hashes.SHA256(),
    )
    return (signing_in + b"." + _b64url(sig)).decode("ascii")


# ── HTTP helpers ─────────────────────────────────────────────────────────

def _post_json(url: str, body: dict, headers: dict | None = None, timeout: int = 10) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")
        raise LockboxError(f"POST {url} → {e.code}: {body_text}") from e
    except urllib.error.URLError as e:
        raise LockboxError(f"POST {url} failed: {e.reason}") from e


def _get_json(url: str, headers: dict | None = None, timeout: int = 10) -> dict:
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")
        raise LockboxError(f"GET {url} → {e.code}: {body_text}") from e
    except urllib.error.URLError as e:
        raise LockboxError(f"GET {url} failed: {e.reason}") from e


# ── Public API ───────────────────────────────────────────────────────────

def exchange_jwt_for_iam_token(sa_key: dict) -> str:
    """Return an IAM token (bearer). The SA key itself never leaves this process."""
    iam_host = os.environ.get("YC_IAM_ENDPOINT", IAM_ENDPOINT_DEFAULT)
    jwt      = _build_jwt(sa_key, now=int(time.time()))
    resp     = _post_json(f"https://{iam_host}/iam/v1/tokens", {"jwt": jwt})
    token    = resp.get("iamToken")
    if not token:
        raise LockboxError(f"IAM response missing iamToken: {resp}")
    return token


def fetch_payload(secret_id: str, iam_token: str) -> dict:
    """
    Return the raw Lockbox payload:
        {"entries":[{"key":"foo","textValue":"bar"},{"key":"baz","binaryValue":"…"}]}
    """
    host = os.environ.get("YC_LOCKBOX_ENDPOINT", LOCKBOX_ENDPOINT_DEFAULT)
    return _get_json(
        f"https://{host}/lockbox/v1/secrets/{secret_id}/payload",
        headers={"Authorization": f"Bearer {iam_token}"},
    )


def _inject_entries(payload: dict) -> int:
    """
    Walk Lockbox entries and push each into os.environ (uppercase name).
    Skips binary-only entries (they have no sensible env representation).
    Returns the number of variables injected.
    """
    n = 0
    for entry in payload.get("entries", []):
        key = entry.get("key")
        val = entry.get("textValue")
        if not key or val is None:
            continue
        env_name = str(key).upper()
        # don't clobber explicit env overrides
        if os.environ.get(env_name):
            continue
        os.environ[env_name] = str(val)
        n += 1
    return n


def load_from_lockbox() -> bool:
    """
    Bootstrap entry point. Returns True if secrets were loaded, False if
    this source isn't configured (caller then falls through to the next
    bootstrap path — Vault, then plain env).

    Never raises on "not configured"; only raises when config IS present
    but network/auth/parsing fails — the app can't silently proceed with
    partial secrets.
    """
    sa_key_file = os.environ.get("YC_SA_KEY_FILE")
    secret_id   = os.environ.get("YC_LOCKBOX_SECRET_ID")

    if not sa_key_file or not secret_id:
        return False

    try:
        with open(sa_key_file) as f:
            sa_key = json.load(f)
    except FileNotFoundError:
        raise LockboxError(f"YC_SA_KEY_FILE not found: {sa_key_file}")
    except json.JSONDecodeError as e:
        raise LockboxError(f"YC_SA_KEY_FILE is not valid JSON: {e}")

    required = {"id", "service_account_id", "private_key"}
    missing  = required - set(sa_key)
    if missing:
        raise LockboxError(
            f"SA key file missing fields: {sorted(missing)}"
        )

    iam_token = exchange_jwt_for_iam_token(sa_key)
    payload   = fetch_payload(secret_id, iam_token)
    count     = _inject_entries(payload)
    logger.info(
        "Lockbox bootstrap: injected %d variables from secret %s",
        count, secret_id,
    )
    return True
