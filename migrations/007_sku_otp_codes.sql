-- Email OTP codes for signup / login / verify flows. Hashed code +
-- attempt counter + TTL-driven expiry. PostgresOtpStore reads/writes
-- this table; periodic purge_old() trims expired rows.

CREATE TABLE IF NOT EXISTS sku_otp_codes (
    id          BIGSERIAL PRIMARY KEY,
    email       TEXT NOT NULL,
    purpose     TEXT NOT NULL,
    code_hash   TEXT NOT NULL,
    attempts    INT  NOT NULL DEFAULT 0,
    expires_at  TIMESTAMPTZ NOT NULL,
    used_at     TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_otp_email_purpose
    ON sku_otp_codes (LOWER(email), purpose, created_at DESC);
