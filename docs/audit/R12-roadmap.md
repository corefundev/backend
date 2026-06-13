# R12 Remediation Roadmap — 2026-06-11

Source: `docs/audit/R12.md` (0 CRIT / 4 HIGH / 9 MED / 6 LOW, baseline
`audit-baseline-R12` = `1b34860`). Every item follows the closure workflow:
fix → commit → CI green → deploy → prod-verify → memory update → close.
No quick fixes; serve-path changes gate through the same-model A/B harness
(`scripts/backtest_ab_serve.py`).

## Phase 0 — Quick-win HIGHs (today) — task #85 wave 1

| Item | Repo | Change | Verify |
|---|---|---|---|
| 0.1 PyJWT 2.12.0 → 2.13.0 | backend | `requirements.txt:65` one-line bump (drop-in; closes CVE-2026-48526 HIGH + 4) | unit auth tests + CI + prod /auth smoke |
| 0.2 axios → 1.16.0, react-router → 6.30.4 | frontend | in-range `npm update` + lock commit (closes 6×HIGH + open redirect) | tsc + build + prod login smoke |

Exit: both PRs merged, CD green staging+prod, auth flows smoke-tested live.

## Phase 1 — Remaining HIGHs

1. **#87 telegram.py triple** — atomic `GETDEL` token consume; 3-attempt
   send retry (backup.sh pattern); partial-update of `notifications.telegram`
   subtree instead of whole-config RMW. Unit tests for all three.
2. **#86 + #65 container hardening (merged scope)** — Grafana/MLflow:
   root-phase Lockbox fetch → drop to service UID before exec; mlflow pip
   install off root. Then the broader #65 cap_drop/no-new-privileges sweep for
   app/data tier. Per [[no-revert rule]]: if an image blocks hardening,
   replace the piece, never revert.
3. **#88 cosign verify in cd_deploy.sh** — fail-closed signature check
   (repo identity + GitHub OIDC issuer) before pull/up on both hosts; cosign
   binary provisioning on VPS is part of the item.
4. **#66 process-worker off raw docker.sock** (R11 leftover, same tier) —
   docker-socket-proxy pattern already proven by autoheal.

Exit: 0 open HIGH across R11+R12.

## Phase 2 — MED batch — tasks #89, #85 wave 2

- #89a pin `cryptography` explicitly (security-critical transitive).
- #89b port G4d manifest gate into `scripts/restore.sh` (manual DR parity).
- #89c split `requirements-dev.txt` (pytest/flake8/mypy/types/httpx out of
  prod images).
- #89d backup-container cron logrotate.
- #85 wave 2: fastapi 0.136.x + starlette 1.0.1 paired bump (full regression
  run), scikit-learn 1.5+ opportunistic.
- #82 MLflow server image → v3.13.0 (R11 leftover, pairs with the dep work).

## Phase 3 — Forecast quality (the product lever)

Order fixed by expected impact; each measured via same-model A/B before/after:

1. **#58 sku_encoded train/serve skew** — persist the train-time factorize
   mapping in the model artifact (or drop the feature after A/B). The single
   largest open serve-quality lever.
2. **#91 holiday recompute in recursive path** — exclude holiday cols from
   carry_cols, recompute per forecast date.
3. **Known-future covariates** — calendar+holidays at TARGET dates for the
   direct/MIMO path (train-side change, the "third path" from the #59 A/B).
4. **#59 decision** — Business-tier same-model A/B (ensemble far heads);
   then tier-gate or revert the direct default.
5. **Ensemble blend weights on clean holdout** (paid tiers, ~33% train cost).
6. **Feature-importance → MLflow** (visibility; kill dead features).
7. **Horizon alignment** — model_h vs plan horizons 30/90, per-tier decision.

Exit: measured WMAPE/MASE improvement vs the #76 baselines
(Free 0.453/2.93, Business 0.340/1.249) on the wired backtest.

## Phase 3.5 — Product / UX + config-system improvements

Added 2026-06-14 after the client-config review. These are NOT forecast-math
items; they are the product surface around training config.

### Product / UX
- **#94 Currency-selection UI (FE)** — `features.external_regressors_ru`
  (CNY/USD/EUR/BYN/KZT, ЦБ РФ FX) is backend-complete and in the Start
  whitelist, but has ZERO UI — settable only via raw API. Add to
  `SettingsPage.tsx`: a "Учёт курсов валют (ЦБ РФ)" toggle + a checkbox
  group, default all-on, presets Asia (CNY+KZT+BYN) / West (USD+EUR) /
  CNY-only. Plan-gate: Start+Business editable, Free locked. Mirrors the
  existing weather block; inline Toggle/Field/Section patterns, brand-500,
  radius scale, inline RU copy. Full implementation map in task #94.
- FX regressors are auto-forced ON for Start+Business today but the user
  can't SEE or tune the currency list — #94 closes that visibility gap.

### Config-system hardening (from the 2026-06-14 review)
- **#95 Audit gap** — PUT/PATCH `/clients/{id}/config` are NOT audited
  (only DELETE is), yet a stale comment claims they are. Config changes are
  billing+output-relevant; add `record_event(EVT_PLAN_CHANGE, config_set/
  config_patch)` with a before/after diff. MED.
- **#96 user-set tracking** — train.py hand-lists 3 keys
  (`user_set_hpo/objective/regressors_ru`) so plan defaults don't clobber
  explicit choices; fragile (new defaulted key silently clobbers). Replace
  with a generic "default only if the dotted key is absent from the override"
  helper. Folds into #92.
- Business config is validated only on ranges + JSON depth (None whitelist) —
  malformed keys/types reach training and fail late instead of a clean 422.
  Low-risk DX; note for a future config-schema pass.

### Proposed Start vs Business feature split (DESIGN — needs sign-off)
Today the only hard line is `config_allowed_keys` (Start = 16-key whitelist,
Business = all) + HPO budget (15 vs 30). The model tier (3×MIMO ensemble) and
feature engine are IDENTICAL. That makes the paid tiers hard to tell apart.
Recommended differentiation (see chat 2026-06-14 for rationale):
- **Keep shared:** ensemble model, all feature toggles, horizon knob,
  objective choice, FX regressors + currency selection (#94).
- **Start-capped / Business-only:** raw ML hyperparams (already Business-only
  via whitelist — keep); HPO depth (15 vs 30 — keep); longer horizon (30 vs
  90 — keep); SKU ceiling (1500 vs ∞ — keep).
- **New Business-only levers to ADD (once the quality workstream lands them):**
  champion/challenger model lifecycle, per-cluster ensemble (Tier C), custom
  holiday calendars / promo uploads, conformal prediction intervals, and
  higher `/predict` throughput (already ∞ vs 5000/h). These give Business a
  *capability* story, not just "more of the same knobs."
- Decision needed: do FX currency presets stay Start+Business, or become a
  Business-only "advanced exogenous" bundle? Recommend: **basic FX on Start,
  full exogenous (Brent/key-rate/Rosstat when built) on Business.**

## Phase 4 — Decisions needed + maintenance

- **#90 dead Airflow chain teardown** — BLOCKED on user sign-off (intersects
  R6-1 evidently/scipy deferral). Scope: auto_retrain_dag + evidently +
  scipy wedge + compose airflow profile services.
- #73 monthly-limit machinery teardown (map in R12 track-8 output, ~60 LOC).
- #92 arch backlog: settings.py adoption (43 env-read sites), auth.py split,
  audit/log.py + storage/backend.py unit tests, ARCHITECTURE.md.
- #92 LOW batch: otp `functools.cache`, FE `<a href>`→`<Link>` ×3 + `as any`,
  unpinned python-multipart line, skip_staging policy note.
- #74 Ansible scaffold; #83 buildx resilience; R12-M9 EOL toolchain
  (Node 22+, vite 6, eslint 9) folded into R7-11 maintenance.

## Sequencing rules

- One PR per item (docs may ride along), watch CD green to staging AND prod
  before the next merge (feedback_verify_cd_per_merge).
- Infra-tier compose changes need manual `--force-recreate` after CD ships
  the file (cd_deploy.sh only recreates api/workers/migrate).
- NFOR linter (`tools/lint/no_fail_open_return.py`) as the last step before
  every commit.
