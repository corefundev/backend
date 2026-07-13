-- AUTH-2 (#446): remember-me refresh tokens (httpOnly cookie).
--
-- Opaque token "<row_id>.<secret>"; only sha256(secret) is stored.
-- Rotation: every /auth/refresh spends the presented row (used_at) and
-- inserts a fresh one in the SAME family_id. Presenting an already-spent
-- token = theft signal -> the whole family is revoked (reuse detection).
-- revoked_at is set family-wide on logout and client-wide on any
-- password write (change/reset) alongside the JWT revocation watermark.

CREATE TABLE IF NOT EXISTS sku_refresh_tokens (
    id          BIGSERIAL PRIMARY KEY,
    client_id   TEXT        NOT NULL,
    family_id   TEXT        NOT NULL,
    token_hash  TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at  TIMESTAMPTZ NOT NULL,
    used_at     TIMESTAMPTZ,
    revoked_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_sku_refresh_tokens_family ON sku_refresh_tokens (family_id);
CREATE INDEX IF NOT EXISTS idx_sku_refresh_tokens_client ON sku_refresh_tokens (client_id);
