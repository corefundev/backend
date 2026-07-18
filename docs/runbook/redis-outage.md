# Redis outage

## Symptoms

- `RedisDown` alert firing.
- `/readyz` returns 503 with `"redis": {"ok": false}`.
- `/auth/signup` may still work (R5-M7 fail-open on rate-limit).
- `/auth/logout` may report success but token isn't actually revoked
  until Redis is back (R3-5 bounded in-mem fallback).
- RQ workers idle / `RqQueueBackup` alert (training jobs queued
  but nothing picks them up).

## Triage (60s)

```bash
ssh -i ~/.ssh/claude/deploy-key deploy@62.217.181.157 \
    'docker ps -f name=docker-redis-1 --format "{{.Names}}\t{{.Status}}"'
# Expected on healthy: "Up Nh (healthy)"
# Expected on broken: "Exited" or missing
```

If container is up but Redis itself unreachable:

```bash
ssh ... 'docker exec docker-redis-1 redis-cli ping'
# Expected: PONG
```

## What still works (intentional fail-open)

| Path | Behaviour without Redis |
|------|--------------------------|
| `/predict` (cached client) | Works — model in-memory cache |
| `/predict` (cold client) | Works — Postgres-only lookup |
| `/auth/signup` rate-limit | Fails open (R5-M7) — counts allowed through |
| `/auth/login` rate-limit | Fails open |
| `/auth/logout` JWT revoke | Falls back to in-memory bounded map (R3-5, R5-M7), single-process only |
| Training enqueue | **BROKEN** — RQ requires Redis |
| OTP store (file backend) | Works (dev path only — prod uses Postgres) |

## Action

### Option 1 — Container is `Exited`, no data loss expected

```bash
ssh -i ~/.ssh/claude/deploy-key deploy@62.217.181.157 \
    'cd /srv/backend && docker compose ... up -d redis'
# Verify:
ssh ... 'docker exec docker-redis-1 redis-cli ping'
# Expected: PONG
```

### Option 2 — Container healthy but Redis hanging / OOM

```bash
# Check memory pressure first:
ssh ... 'docker stats --no-stream docker-redis-1'
# If mem usage near limit (R5-M9: 256m), prod may need a temporary
# bump. Edit docker-compose.yml mem_limit, then restart:
ssh ... 'cd /srv/backend && docker compose ... restart redis'
```

### Option 3 — Data corrupt / replication broken

Redis has no persistent state we care about — every value is
rate-limit counter (24h-bucketed) or RQ job (re-enqueueable from
Postgres training_runs). Safe to wipe:

```bash
ssh ... 'docker exec docker-redis-1 redis-cli FLUSHALL'
# Then re-enqueue any pending training runs from Postgres:
ssh ... 'docker exec docker-api-1 python3 -c "
from src.storage.training_runs import get_training_runs_registry
reg = get_training_runs_registry()
# Mark stale running rows so the next training trigger picks up
reg.reap_stale_runs(stale_after_minutes=0)
print(\"reaped\")
"'
```

## Verify

```bash
curl -fsS https://api.sprosly.com/readyz | jq .
# Expected: {"ready": true, "checks": {"redis": {"ok": true}, ...}}
```

Smoke a rate-limit:

```bash
# After Redis is back, /auth/token rate-limit should re-engage.
# Repeated 401s from a single IP should hit 429 after 20/hour.
```

## Post-mortem

Brief — Redis outages are usually OOM or DC blip, not interesting.
If it was OOM, log the mem_limit before/after change in
`project_audit_remediation.md` so the next operator knows the new
ceiling.

## DO NOT

* **DO NOT** delete Redis data files (`/data/dump.rdb`) without
  first confirming RQ queue is empty. Job loss = re-trigger
  training manually, but at least intentional.
* **DO NOT** raise R5-M9 mem_limit silently. The 256m cap is a
  signal that something is leaking — bump + log + plan to fix.
