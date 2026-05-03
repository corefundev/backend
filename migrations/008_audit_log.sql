-- Audit log: append-only record of security-relevant events.
--
-- What lives here:
--   • auth events  — login_success, login_failure, logout, otp_send,
--                    otp_verify, oauth_callback, signup
--   • account      — plan_upgrade, plan_downgrade, password_change,
--                    email_change
--   • data ops     — model_delete, model_train_enqueued, upload_delete
--   • system       — secret_rotation, admin_action
--
-- What does NOT live here: routine reads (GET /forecasts, GET /clients)
-- — they belong in nginx access logs, not audit. Audit is for "who did
-- a thing that changed state or accessed credentials".
--
-- Append-only: we never UPDATE or DELETE rows from this table from
-- application code. If a retention policy lands later, that's a
-- scheduled job, not an in-request mutation.
--
-- client_id is nullable: failed login attempts before authentication
-- have no client yet (we know the email they tried, not the client_id
-- that resolves to). actor_email captures the attempted identity.

CREATE TABLE IF NOT EXISTS audit_log (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    client_id       TEXT,
    actor_email     TEXT,
    event_type      TEXT NOT NULL,
    event_subtype   TEXT,
    target_type     TEXT,
    target_id       TEXT,
    ip              INET,
    user_agent      TEXT,
    success         BOOLEAN NOT NULL DEFAULT TRUE,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- Per-client timeline (the most common query: "show me my account's
-- recent events" on the security page).
CREATE INDEX IF NOT EXISTS idx_audit_log_client_ts
    ON audit_log (client_id, ts DESC) WHERE client_id IS NOT NULL;

-- Cross-client by event type (incident response: "all OAuth failures
-- in the last hour", "all plan_upgrades today").
CREATE INDEX IF NOT EXISTS idx_audit_log_event_ts
    ON audit_log (event_type, ts DESC);

-- Failed-login surface for brute-force detection: index only the
-- failure rows so the partial index stays small.
CREATE INDEX IF NOT EXISTS idx_audit_log_failures
    ON audit_log (actor_email, ts DESC)
    WHERE success = FALSE AND actor_email IS NOT NULL;
