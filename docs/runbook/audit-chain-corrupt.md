# `audit_log` HMAC chain reports corruption

## Symptoms

- Telegram alert from R7-2 cron: `🔥 AUDIT CHAIN CORRUPT`.
- `gh run list --workflow=audit-verify-cron.yml` shows red runs.
- Manual `curl /internal/audit/verify` returns `{"ok": false, ...}`.

## What this means

`audit_log` is the HMAC-chained table where every row's
`row_hash = HMAC(audit_hmac_key, prev_hash || row_data)`. If any
row was modified after insertion — by a buggy migration, a manual
SQL edit, or (worst case) an attacker — the chain breaks at that
row. **From that row forward, every subsequent verification will
fail.**

This is **tamper-evidence**, not tamper-prevention. The point is
that you find out, not that you stop it.

## First action: DO NOT TOUCH the audit_log table

The single most important thing: **do not run any UPDATE / DELETE
/ TRUNCATE on audit_log**. The corruption is now a forensic
artifact. Any rewrite destroys evidence + makes the chain
diverge further.

If a teammate is mid-incident also looking at the table, tell them
to stop.

## Triage (5 min)

```bash
# 1. Re-run the verify to get the failing row id.
gh workflow run audit-verify-cron.yml
# Or directly:
ssh -i ~/.ssh/claude/deploy-key deploy@api.testcore.ru \
    'docker exec docker-api-1 python3 -c "
from src.auth.vault_agent import bootstrap_secrets
bootstrap_secrets()
from src.audit import verify_chain
r = verify_chain(limit=100000)
print(\"ok:\", r.ok)
print(\"rows_checked:\", r.rows_checked)
print(\"first_bad_id:\", r.first_bad_id)
print(\"reason:\", r.reason)
print(\"detail:\", r.detail)
"'

# 2. Inspect the bad row + neighbours.
ssh ... 'docker exec docker-postgres-1 psql -U sku -d sku_forecasting -c \
    "SELECT id, ts, event_type, event_subtype, actor_email, \
            target_type, target_id, success \
     FROM audit_log \
     WHERE id BETWEEN <first_bad_id - 5> AND <first_bad_id + 5> \
     ORDER BY id;"'

# 3. Check for recent migrations that might have touched audit_log.
ssh ... 'docker logs docker-migrate-1 2>&1 | grep -i audit | tail -20'
```

## Distinguishing cause

### Cause A — buggy migration

Migrations sometimes need to backfill / re-shape audit columns.
If a migration UPDATE'd rows without recomputing `row_hash`, the
chain breaks at the FIRST migrated row.

Indicator: `reason` says `hmac_mismatch`, `first_bad_id` is at a
boundary corresponding to a recent migration timestamp.

**Resolution:** the migration was wrong. Don't "fix" by rewriting
audit_log — that defeats the purpose. Document the gap, freeze
backward verification at the boundary, and treat post-boundary
chain as the new baseline.

### Cause B — DB credential rotation window

If credentials rotated mid-write, a partially-committed transaction
could leave a row without its `row_hash` computed. R6-3 atomic-write
discipline prevents this on the OTP path, but audit_log inserts
go through `INSERT` on Postgres — should be atomic by default.

Indicator: rare, but possible if `pg_terminate_backend` was used
during a known-busy window.

**Resolution:** preserve the bad row, mark in memory, accept the
gap.

### Cause C — real tampering

Someone with SQL access modified a row to hide an action. This is
the scenario that justifies the entire chain.

Indicator: `reason` says `hmac_mismatch`, `first_bad_id` corresponds
to no migration / no rotation window, OR row data clearly looks
"clean" in a way that doesn't match the original event shape (e.g.,
`actor_email` = NULL where it was always populated).

**Resolution:**
1. Snapshot the entire audit_log table immediately:
   ```bash
   ssh ... 'docker exec docker-postgres-1 pg_dump -U sku \
       -t audit_log sku_forecasting > /tmp/audit_forensic_YYYYMMDD.sql'
   ```
2. Copy to off-VPS storage (S3 bucket with restricted access).
3. Pull DB credential list (`pg_authid`), check `pg_stat_activity`
   for the rough timeframe, cross-reference with VPS auth logs
   (`journalctl -u ssh`).
4. Escalate.

## Verify (after preservation, before clearing)

Once the cause is identified AND the bad rows are preserved
in a forensic dump:

- **DO NOT rewrite the chain.** A broken chain is a permanent
  artifact — any "fix" looks like further tampering.
- Add an audit_log entry of `event_type='audit_chain_break'` with
  `target_id=<first_bad_id>` so the gap has a timestamp.
- Update R7-2 cron to skip rows up to that id, OR accept the
  daily Telegram noise and treat as known-bad.

## Post-mortem

This is the most important post-mortem of the year. Document:

* Date / time of detection (alert timestamp).
* `first_bad_id` and the inferred cause (A / B / C).
* Forensic dump location.
* Who had SQL access in the window (`pg_authid` + SSH logs).
* Whether the chain break corresponds to a known operation
  (migration, rotation) or NOT.

Add a Round 8 audit memory entry referencing the forensic
artifact location.

## DO NOT

* **DO NOT** `UPDATE audit_log SET row_hash = ...` to "fix" the
  chain. That's exactly what an attacker would do; from the
  outside it looks identical.
* **DO NOT** `DELETE` the bad row. Same problem.
* **DO NOT** TRUNCATE the table to "start fresh". You lose every
  prior security event AND signal to any future auditor that
  evidence was destroyed.
* **DO NOT** discuss the corruption in Slack / Telegram before
  the forensic dump is off-VPS. If cause C is real, you don't
  want to tip off the attacker that you've noticed.
