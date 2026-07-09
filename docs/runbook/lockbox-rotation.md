# Yandex Lockbox SA-key rotation

## Symptoms

- Triggered by: scheduled rotation (every N months), suspected
  key leak, post-incident hardening.
- API container fails to bootstrap secrets → `503 service
  unavailable` from `/readyz` with `postgres / redis: ok=false`.
- `secret_rotation` audit event in audit_log when the new key is
  detected (lifespan hook emits this on api restart — see
  `_emit_secret_rotation_event_if_changed` in main.py).

## Pre-flight

```bash
# 1. Confirm yc CLI is authenticated locally.
yc config list
# Profile should be the master-creds profile (NOT the SA-key
# itself — that's what we're rotating).

# 2. Confirm the SA name from memory.
cat ~/.claude/projects/-Users-ilailin-Desktop-core/memory/project_lockbox_bootstrap.md \
    | grep -i "service.account\|sa.*name"
# Look for the SA used by api / worker / sandbox containers.

# 3. Note current key id in audit_log so you can verify
#    the rotation registered.
ssh -i ~/.ssh/claude/deploy-key deploy@api.testcore.ru \
    'docker exec docker-postgres-1 psql -U sku -d sku_forecasting -c \
     "SELECT target_id, ts FROM audit_log \
      WHERE event_type = '"'"'secret_rotation'"'"' \
      ORDER BY ts DESC LIMIT 1;"'
```

## Action

Use the canonical script — it handles the safety window
(keep 2 newest, delete the rest):

```bash
# From your laptop (NEVER from prod VPS — master-creds must not
# live on the production host):
bash scripts/rotate_lockbox_key.sh
# The script:
#   1. yc iam key create — new JSON
#   2. scp to /srv/backend/secrets/yc/yc-sa-key.json.new on api VPS
#   3. atomic mv .new → yc-sa-key.json
#   4. docker compose restart api worker scan-worker process-worker
#   5. Wait for /readyz=200 (Lockbox bootstrap must succeed)
#   6. yc iam key delete for all keys EXCEPT the 2 newest
#      (safety window)
#
# Idempotent: any stage failure leaves the OLD key in place. Old
# keys are deleted only after restart+readyz succeeds.
```

## Verify

```bash
# 1. /readyz green
curl -fsS https://api.testcore.ru/readyz | jq .

# 2. secret_rotation audit event emitted (new target_id).
ssh ... 'docker exec docker-postgres-1 psql -U sku -d sku_forecasting -c \
    "SELECT target_id, ts FROM audit_log \
     WHERE event_type = '"'"'secret_rotation'"'"' \
     ORDER BY ts DESC LIMIT 2;"'
# Top row should be NEW (different target_id from pre-flight).
# Second row is what was previously latest — preserved by the
# audit chain.

# 3. Telegram alert NOT fired
# (audit-verify-cron R7-2 runs daily at 04:00 UTC; should be ok=true).

# 4. List of remaining SA keys — should be ≤2.
yc iam key list --service-account-id <SA_ID>
```

## Same-day key revocation (emergency, leaked key)

If a key was published / leaked publicly:

```bash
# Don't wait for rotation safety window — pull the bad key NOW.
yc iam key delete <LEAKED_KEY_ID>
# This will break any in-flight requests that resolve a Lockbox
# secret with the leaked key, but cached env-already-in-Python
# stays valid. New container restarts will pick a different key.
# IMMEDIATELY follow with full rotate_lockbox_key.sh to get to
# a clean state.
```

## Post-mortem

If rotation was a scheduled one — log it in
`project_audit_remediation.md` with the new SA key id (the
audit event also captures this, but a doc trail helps).

If emergency rotation (leak) — add a memory entry:

```markdown
## Leak incident — SA key id <X> revoked YYYY-MM-DD
Source of leak: <git push / log spillage / etc>
Containment: full rotate_lockbox_key.sh + new SA key
Forensic copy: see audit_log secret_rotation event id <Y>
```

## DO NOT

* **DO NOT** run rotation FROM api.testcore.ru. Master-creds must
  not live on prod.
* **DO NOT** delete the previous key without confirming the new
  one bootstrapped successfully (`/readyz=200`).
* **DO NOT** edit `secrets/yc/yc-sa-key.json` in-place on the VPS
  without the .new → atomic rename pattern. A partially-written
  file will brick api on the next container restart.
