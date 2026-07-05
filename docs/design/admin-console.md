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

## 2. Information architecture (v2 — full rethink 2026-07-06, user-approved)

The v1 IA under-scoped the console (user: «полноценная админ-панель, контроль
над системой, а не демо»). v2 structure of record — sections join the /admin
sidebar as they ship:

```
/admin
├── Обзор          ADM-4 #257   KPI + «требует внимания» + события
├── Клиенты        ADM-10 #278  поиск/фильтры/suspend/rotate  [Волна 1]
│   └── карточка   ADM-3 #256   client-360 + действия + H5    [Волна 1]
├── Тарифы         ADM-11 #279  каталог/история смен/распределение [Волна 1]
├── Обучение       ADM-2 #255   лента+gate+stale; v2 действия [Волна 2]
├── Данные         ADM-6 #259   загрузки/карантин/объёмы      [Волна 3]
├── Уведомления    ✅ #251+#254  compose/broadcast/история
├── Аудит          ADM-5 #258   журнал+tamper-verify+CSV      [Волна 3]
├── Безопасность   ADM-12 #280  админ-входы/возраст ключа     [Волна 3]
├── Система        ADM-13 #281  здоровье/версии/бэкапы/links  [Волна 2]
├── Юр. документы  ✅
├── Биллинг        ⏸ #145
└── PII / 152-ФЗ   ⏸ #156
```

Waves: 1 = ядро управления (Клиенты-PRO → Тарифы → client-360);
2 = надзор (Обучение → Обзор → Система); 3 = комплаенс (Аудит →
Безопасность → Данные). Summary table of record lives in epic #263.

**Standing non-goals:** impersonation; in-UI secrets; plan-limit edits from
UI before B1 (#150). Every new admin route extends the H1 static test in
the same PR; every write lands in audit_log; client-360 views audit-logged
(H5).

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
