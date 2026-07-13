-- AUTH-1 (#445): classic email/password authentication.
--
-- password_hash    — bcrypt (cost 12, house standard: api_keys/OTP).
--                    NULL = no password yet: legacy email-OTP accounts
--                    (forced to set one at next login, AUTH-4 #448) and
--                    OAuth accounts (password optional by owner decision).
-- password_set_at  — when the password was last set/changed. Doubles as
--                    the session-revocation watermark: JWTs issued before
--                    this instant are rejected (iat < password_set_at).
--
-- One-time confirm/reset LINKS reuse sku_otp_codes (purposes 'confirm' /
-- 'reset', token = "<id>.<secret>", bcrypt hash in code_hash) — no new
-- table needed; TTL is per-row via expires_at.

ALTER TABLE sku_clients ADD COLUMN IF NOT EXISTS password_hash   TEXT;
ALTER TABLE sku_clients ADD COLUMN IF NOT EXISTS password_set_at TIMESTAMPTZ;
