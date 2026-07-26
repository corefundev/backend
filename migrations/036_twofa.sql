-- 2FA-1 #587: двухэтапная аутентификация клиента.
-- Секрет TOTP шифруется приложением (Fernet, ключ TWOFA_ENC_KEY из
-- Lockbox) — БД никогда не видит plaintext. Резервные коды — только
-- HMAC-SHA256 (высокоэнтропийные, brute по хешу бессмысленен; bcrypt
-- на 10 кодов делал бы каждый вход ~2с).
CREATE TABLE IF NOT EXISTS sku_twofa (
    client_id        TEXT PRIMARY KEY,
    totp_secret_enc  TEXT,                       -- Fernet(base32-секрет); NULL = не подключено
    totp_enabled_at  TIMESTAMPTZ,                -- NULL при pending-enrollment
    email_enabled_at TIMESTAMPTZ,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sku_twofa_backup_codes (
    id         BIGSERIAL PRIMARY KEY,
    client_id  TEXT NOT NULL,
    code_hmac  TEXT NOT NULL,
    used_at    TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS twofa_backup_client_idx
    ON sku_twofa_backup_codes (client_id) WHERE used_at IS NULL;
