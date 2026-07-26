"""
src/auth/twofa.py — 2FA-1 (#587): двухэтапная аутентификация клиента.

Способы: TOTP-приложение (pyotp, RFC 6238), одноразовый код на почту
(существующая OTP-инфра — включается флагом здесь, письмо шлёт слой
логина), резервные коды (10 одноразовых).

Контракты безопасности (эпик #587):
  • TOTP включается ТОЛЬКО после верификации кода — pending-секрет
    (totp_enabled_at IS NULL) на вход не влияет;
  • секрет TOTP шифруется Fernet'ом, ключ TWOFA_ENC_KEY из Lockbox;
    ключа нет → fail-closed (TwoFAUnavailable, наружу 503);
  • резервные коды: plaintext отдаётся ОДИН раз, хранится HMAC-SHA256
    с ключом-перцем (высокая энтропия кода делает brute по хешу
    бессмысленным; bcrypt на 10 кодов стоил бы ~2с на каждый вход);
  • регенерация кодов отменяет все прежние (DELETE + INSERT в одной
    транзакции).
"""
from __future__ import annotations

import base64
import hashlib
import hmac as hmac_mod
import logging
import os
import secrets
from dataclasses import dataclass

logger = logging.getLogger(__name__)

BACKUP_CODES_COUNT = 10
TOTP_ISSUER = "Sprosly"


class TwoFAUnavailable(Exception):
    """Крипто-ключ 2FA не сконфигурирован — операции запрещены (503)."""


# ── крипто ────────────────────────────────────────────────────────────────

def _enc_key() -> bytes:
    raw = os.environ.get("TWOFA_ENC_KEY", "")
    if not raw:
        raise TwoFAUnavailable("TWOFA_ENC_KEY is not configured")
    # Lockbox хранит произвольную hex/ascii-строку; Fernet требует
    # 32 urlsafe-base64 байта — детерминированно выводим из строки.
    return base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest())


def encrypt_secret(secret_b32: str) -> str:
    from cryptography.fernet import Fernet
    return Fernet(_enc_key()).encrypt(secret_b32.encode()).decode()


def decrypt_secret(token: str) -> str:
    from cryptography.fernet import Fernet
    return Fernet(_enc_key()).decrypt(token.encode()).decode()


def _code_hmac(code: str) -> str:
    raw = os.environ.get("TWOFA_ENC_KEY", "")
    if not raw:
        raise TwoFAUnavailable("TWOFA_ENC_KEY is not configured")
    norm = code.strip().upper().replace("-", "")
    return hmac_mod.new(raw.encode(), norm.encode(), hashlib.sha256).hexdigest()


# ── TOTP ──────────────────────────────────────────────────────────────────

def new_totp_secret() -> str:
    import pyotp
    return pyotp.random_base32()


def provisioning_uri(secret_b32: str, account_label: str) -> str:
    import pyotp
    return pyotp.TOTP(secret_b32).provisioning_uri(
        name=account_label, issuer_name=TOTP_ISSUER)


def verify_totp(secret_b32: str, code: str) -> bool:
    import pyotp
    code = code.strip().replace(" ", "")
    if not (code.isdigit() and len(code) == 6):
        return False
    # valid_window=1: ±30с дрейфа часов телефона
    return bool(pyotp.TOTP(secret_b32).verify(code, valid_window=1))


# ── резервные коды ────────────────────────────────────────────────────────

def generate_backup_codes(n: int = BACKUP_CODES_COUNT) -> list[str]:
    """XXXX-XXXX из алфавита без похожих символов (0/O, 1/I/L)."""
    alphabet = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
    out = []
    for _ in range(n):
        chars = "".join(secrets.choice(alphabet) for _ in range(8))
        out.append(f"{chars[:4]}-{chars[4:]}")
    return out


# ── состояние/реестр ─────────────────────────────────────────────────────

@dataclass
class TwoFAStatus:
    totp_enabled: bool
    totp_pending: bool
    email_enabled: bool
    backup_left: int

    @property
    def protected(self) -> bool:
        return self.totp_enabled or self.email_enabled


class TwoFARegistry:
    """Postgres-реестр 2FA. SQL по образцу остальных реестров (psycopg2)."""

    def __init__(self, database_url: str):
        import psycopg2
        import psycopg2.extras
        self._psycopg2 = psycopg2
        self._extras = psycopg2.extras
        self._dsn = database_url

    def _conn(self):
        return self._psycopg2.connect(self._dsn)

    # ── чтение ──
    def status(self, client_id: str) -> TwoFAStatus:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT totp_secret_enc, totp_enabled_at, email_enabled_at "
                "FROM sku_twofa WHERE client_id = %s", (client_id,))
            row = cur.fetchone()
            cur.execute(
                "SELECT count(*) FROM sku_twofa_backup_codes "
                "WHERE client_id = %s AND used_at IS NULL", (client_id,))
            left = int(cur.fetchone()[0])
        if row is None:
            return TwoFAStatus(False, False, False, left)
        secret_enc, totp_at, email_at = row
        return TwoFAStatus(
            totp_enabled=bool(secret_enc and totp_at),
            totp_pending=bool(secret_enc and not totp_at),
            email_enabled=bool(email_at),
            backup_left=left,
        )

    def totp_secret(self, client_id: str) -> str | None:
        """Расшифрованный секрет ВКЛЮЧЁННОГО totp (pending не отдаём)."""
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT totp_secret_enc FROM sku_twofa "
                "WHERE client_id = %s AND totp_enabled_at IS NOT NULL",
                (client_id,))
            row = cur.fetchone()
        return decrypt_secret(row[0]) if row and row[0] else None

    def pending_secret(self, client_id: str) -> str | None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT totp_secret_enc FROM sku_twofa "
                "WHERE client_id = %s AND totp_enabled_at IS NULL",
                (client_id,))
            row = cur.fetchone()
        return decrypt_secret(row[0]) if row and row[0] else None

    # ── enrollment ──
    def start_totp_enrollment(self, client_id: str, secret_b32: str) -> None:
        """Pending-секрет; повторный вызов перезаписывает (новый QR)."""
        enc = encrypt_secret(secret_b32)
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sku_twofa (client_id, totp_secret_enc, totp_enabled_at)
                VALUES (%s, %s, NULL)
                ON CONFLICT (client_id) DO UPDATE
                    SET totp_secret_enc = EXCLUDED.totp_secret_enc,
                        totp_enabled_at = NULL,
                        updated_at = now()
                """, (client_id, enc))
            conn.commit()

    def confirm_totp(self, client_id: str) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE sku_twofa SET totp_enabled_at = now(), updated_at = now() "
                "WHERE client_id = %s AND totp_secret_enc IS NOT NULL",
                (client_id,))
            conn.commit()

    def disable_totp(self, client_id: str) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE sku_twofa SET totp_secret_enc = NULL, "
                "totp_enabled_at = NULL, updated_at = now() "
                "WHERE client_id = %s", (client_id,))
            conn.commit()

    def set_email(self, client_id: str, enabled: bool) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            if enabled:
                cur.execute(
                    """
                    INSERT INTO sku_twofa (client_id, email_enabled_at)
                    VALUES (%s, now())
                    ON CONFLICT (client_id) DO UPDATE
                        SET email_enabled_at = now(), updated_at = now()
                    """, (client_id,))
            else:
                cur.execute(
                    "UPDATE sku_twofa SET email_enabled_at = NULL, "
                    "updated_at = now() WHERE client_id = %s", (client_id,))
            conn.commit()

    # ── резервные коды ──
    def regenerate_backup_codes(self, client_id: str) -> list[str]:
        """Новые 10 кодов; прежние отменяются в той же транзакции."""
        codes = generate_backup_codes()
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM sku_twofa_backup_codes WHERE client_id = %s",
                (client_id,))
            for c in codes:
                cur.execute(
                    "INSERT INTO sku_twofa_backup_codes (client_id, code_hmac) "
                    "VALUES (%s, %s)", (client_id, _code_hmac(c)))
            conn.commit()
        return codes

    def consume_backup_code(self, client_id: str, code: str) -> bool:
        """Атомарно жжёт код; True = код был жив."""
        h = _code_hmac(code)
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE sku_twofa_backup_codes SET used_at = now() "
                "WHERE client_id = %s AND code_hmac = %s AND used_at IS NULL",
                (client_id, h))
            hit = cur.rowcount == 1
            conn.commit()
        return hit


_registry: TwoFARegistry | None = None


def get_twofa_registry() -> TwoFARegistry:
    global _registry
    if _registry is None:
        dsn = os.environ.get("DATABASE_URL")
        if not dsn:
            raise TwoFAUnavailable("DATABASE_URL is not configured")
        _registry = TwoFARegistry(dsn)
    return _registry


def reset_for_tests() -> None:
    global _registry
    _registry = None
