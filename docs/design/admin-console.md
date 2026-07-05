# Admin Console — design of record

Status: APPROVED design session 2026-07-05 (user-requested full design pass).
Owner epic: ADM. Related: NC epic #245 (notification center).

## 0. Current surface (verified in-repo 2026-07-05)

| Piece | State |
|---|---|
| Auth | `/login/admin` → ADMIN_API_KEY (Lockbox, hmac.compare_digest R5-9) → `/auth/token` → JWT `role=admin`, TTL 60 min (`JWT_EXPIRE_MINUTES`), Redis revocation available, per-subnet rate-limit on token issuance (R2-4) |
| FE guard | `AdminGuard` (UX only; server enforces), adminOnly nav items |
| Pages | `/app/admin/clients` (list, plans, edit modal), `/app/admin/legal` (doc editor) |
| Backend | 7 `require_role("admin")` endpoints: clients×3, config×1, legal×1, ops×2 (audit-verify, internal-state) |
| Audit | R3-25 admin-action logging on client updates; R7-2 daily tamper-evidence verify + Telegram |

## 1. Principles

1. **Server-enforced, FE-guarded** — every admin endpoint carries
   `require_role("admin")`; the FE guard is convenience. Pinned by a static
   test (H1 below), same class as the route-inventory guard.
2. **Read-first** — new sections land read-only; every WRITE is a separate,
   explicitly-designed action with audit + confirm.
3. **Blast-radius limits** — mass actions (broadcast, future bulk ops) get a
   typed confirmation step; no client deletion from UI v1.
4. **Everything audited** — every admin WRITE → audit_log (tamper-evident
   chain, R7-2); PII-viewing READS (client-360) also logged (152-ФЗ posture).
5. **Don't rebuild Grafana** — system health = link-outs + a few numbers,
   never a dashboard clone.

## 2. Information architecture

**Correction 2026-07-06 (ADM-0 #276, user feedback):** the console lives in a
DEDICATED shell at `/admin/*` — its own AdminLayout (sidebar per the tree
below, header with environment + session identity + 30-min JWT countdown +
logout), visually distinct from the client cabinet. The original placement
inside `/app` (inherited from the pre-design precedent) embedded admin
functions into client chrome — functions right, shell wrong. `/app/admin/*`
becomes redirects; the client nav carries no admin items. The subdomain +
edge IP-allowlist step (H8) builds ON TOP of this shell.

```
/admin
├── Обзор            ADM-4  dashboard: KPIs + "требует внимания"
├── Клиенты          exists → client-360 detail (ADM-3 #256)
├── Обучение         ADM-2 #255  runs feed, gate verdicts, stale models
├── Уведомления      ADM-1 #254  compose / target / broadcast confirm / history
├── Загрузки         ADM-6  cross-client uploads, failures
├── Аудит            ADM-5  audit_log viewer + tamper-verify status
├── Юр. документы    exists
└── Система          ADM-4 (part): Grafana/MLflow/alerts links, versions
future:
├── Биллинг          with #145
├── PII-запросы      with #156 (152-ФЗ export/delete queue)
└── Конфиг-оверрайды ADM-9 (B1 schema-validated editor; R13-1 risk class)
```

## 3. Function inventory

**Exists:** client list/edit/plans; legal editor; audit-verify endpoint.

**In flight (filed):** ADM-1 #254 announcements page · ADM-2 #255 training
oversight · ADM-3 #256 client-360.

**This design adds:**
- **ADM-4 dashboard** — clients by plan/status; trainings 7d (finished /
  failed / gate-blocked); stale>45d count; upload failures 7d; unread
  admin-relevant alerts. One "требует внимания" list ranked by severity —
  the operator's landing page.
- **ADM-5 audit viewer** — filter by client/event/date; verify-chain status
  badge (green = last R7-2 run OK). Read-only; the data already exists.
- **ADM-6 uploads & usage** — cross-client uploads (status, rows, errors);
  per-client API usage / rate-limit hits (#155 synergy).
- **ADM-7 security hardening wave** (see §4).
- **ADM-8 named admin accounts + TOTP** — future, triggered by 2nd operator.
- **ADM-9 config-override editor** — B1-schema-validated (whitelist, value
  ranges), diff preview, audit of every change. NOT before B1 (#150) ships.

**Future (blocked, land into ready IA):** billing section (#145), PII
requests queue (#156).

## 4. Security model

**Threats considered:** (T1) ADMIN_API_KEY leak → full takeover; (T2) admin
JWT theft (XSS/storage); (T3) rogue/compromised admin — silent data access;
(T4) privilege-escalation bug in a new endpoint (missing require_role);
(T5) admin-originated content injection (announcement body → client XSS).

**Hardening ladder:**

| # | Measure | Threat | When |
|---|---|---|---|
| H1 | Static test: EVERY route under `/admin/*` + every router function name prefixed `admin_` carries `require_role("admin")` — CI fails on a new unguarded admin endpoint | T4 | ADM-7 (now) |
| H2 | Separate `ADMIN_JWT_EXPIRE_MINUTES` (default 30) — admin sessions shorter than client 60 min; revocation already exists | T2 | ADM-7 |
| H3 | Admin-login signal: every admin token issuance → ops Telegram + audit row (cheap intrusion tripwire; we are the only legit admin) | T1,T3 | ADM-7 |
| H4 | Announcement bodies rendered as PLAIN TEXT in FE (no HTML path), length caps server-side | T5 | with ADM-1 |
| H5 | PII-read audit: client-360 view → audit row (who looked at whom) | T3 | with ADM-3 |
| H6 | ADMIN_API_KEY rotation recipe in OPERATIONS.md (Lockbox add-version + atomicity rule) + becomes bootstrap-only once H7 lands | T1 | ADM-7 (doc) |
| H7 | Named admin accounts (individual credentials, per-admin audit identity) + TOTP MFA (reuse OTP machinery) | T1,T3 | ADM-8 (2nd operator) |
| H8 | Optional nginx IP-allowlist recipe for `/login/admin` + admin token issuance (documented, opt-in — operator roams) | T1 | ADM-8 (doc) |

**Explicit non-goals:** client impersonation (rejected — read-only
client-360 covers support needs without identity ambiguity in audit);
admin-triggered retraining v1 (separate authz/product decision);
in-panel secrets management (Lockbox CLI stays the only path).

## 5. Delivery order

~~ADM-1 → ADM-7~~ (shipped) → **ADM-0 (shell, #276)** → ADM-2 → ADM-4 →
ADM-3(+H5) → ADM-5 → ADM-6 → ADM-9 (after B1) → ADM-8 (trigger: 2nd
operator). Billing/PII when unblocked.
Each item follows the standard closure workflow; every new admin endpoint
must extend the H1 static test in the same PR.
