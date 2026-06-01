# R11 Remediation Roadmap (2026-06-01)

Source: `docs/audit/R11.md` (0 CRIT / 9 HIGH / 16 MED / 22 LOW). Baseline tag `audit-baseline-R11`.
Every item follows the per-item closure workflow: fix → commit (feature branch) → CI green → deploy (staging→prod) → prod-verify → memory → close. Severity `[V]` = lead-verified, `[A]` = agent-reported (re-read before its PR lands).

Sequencing principle: **stop active leaks first (cheap, low-risk) → protect data/billing integrity → fix the core product (forecast quality, with measurement) → harden the perimeter (careful, staging-gated) → clean house.** Cleanup (Phase 4) is low-risk and can run in parallel with any phase.

---

## Phase 0 — Quick wins / stop the bleeding  (~0.5–1 day, 4 small independent PRs)

Low-risk, fast, each independently shippable. Removes two live info-leaks + two latent crashers.

| # | Item | Why first | Risk |
|---|---|---|---|
| #64 H7 [V] | `/readyz` invalid JSON + dep-class leak | Trivial (`JSONResponse`); observed live on prod; was already roadmap T2 | none |
| #62 H5 [A] | `/jobs/{id}` traceback leak → trace_id | Small; tenant-facing info disclosure | low |
| #63 H6 [A] | `aggregate_metrics` IndexError on all-NaN | Small guard; turns an opaque training crash into a clean result | low |
| #68(part) M7 [V] | `train.py` MLflow temp-pkl leak → try/finally | Small; stops `/tmp` disk-fill on every failed-MLflow run | low |

**Exit:** 4 PRs merged + prod-verified. No behaviour change to forecasts.

---

## Phase 1 — Data & billing integrity  (~1–2 days, 3–4 PRs)

Protects what customers are billed for and what `/predict` serves. Touches training/quota/DB — moderate test surface.

| # | Item | Why | Risk |
|---|---|---|---|
| #61 H4 [V] | Concurrent-training clobber → `status != 'training'` row-locked gate | **Data integrity** — two runs corrupt the served model/forecasts today | med (concurrency tests needed) |
| #60 H3 [V] | SKU plan-cap bypass (manifest-fail→0→skip; data_path route) | **Billing integrity** — clients train beyond `max_skus` | med |
| #67(part) M5 [A] | anomalies/forecasts `ON CONFLICT DO UPDATE` + upstream dedup | Dup `(sku,date)` aborts the replace → silently stale anomalies after retrain | med |
| #67(part) M2 [V] | signup-verify OTP → atomic `claim_active` | Closes the one non-atomic OTP leg (mirror login) | low |

**Exit:** training pipeline can't clobber, quota is enforced on every path, replace is idempotent. Add concurrency regression tests.

---

## Phase 2 — Forecast quality: the core product  (~2–3 days, gated on measurement)

Highest customer value, highest care. These change `/predict` output, so they must be proven improvements, not blind edits.

**2a — Build the measurement scaffold FIRST** (prerequisite):
- A repeatable backtest harness: fixed dataset → train → walk-forward → WMAPE/MASE, before vs after. Without it we cannot prove H1/H2 fixes help (and the training-quality baseline in memory says synthetic data has weak signal — pick a dataset with real per-SKU structure).

**2b — The two fixes, each measured:**

| # | Item | Fix shape | Risk |
|---|---|---|---|
| #58 H1 [V] | `sku_encoded` train/serve skew | Persist train-time sku→code map + apply at serve, OR drop the feature entirely (it's nominal-as-ordinal — likely net-positive to drop). Decide by importance/backtest. | med — changes the feature vector; needs retrain of all client models |
| #59 H2 [V] | MIMO/ensemble served recursively | Branch on `is_mimo`/`is_ensemble` → direct `(H,)` predict; keep `recursive_forecast` for single-step `lgbm` only | med — changes serve path for the default config |

**Decision needed:** both fixes likely require retraining existing client models (feature-vector / serve-semantics change). Plan a controlled re-train + a before/after accuracy report per client, not a silent swap.

**Exit:** backtest shows ≥ no-regression (ideally improvement); models retrained; per-client accuracy delta recorded.

---

## Phase 3 — Perimeter hardening  (~2–3 days, staging-gated, security depth)

Container isolation. Higher deploy-risk (a wrong cap/mount breaks a service — the autoheal session is the cautionary tale). Validate EACH service boots healthy on staging before prod.

| # | Item | Why | Risk |
|---|---|---|---|
| #66 H9 [A] | process-worker off the raw RW docker.sock | **Biggest blast radius** — untrusted-upload parser has host-root. Needs design: scoped socket-proxy (allow only `containers/create+start` + image/flag allowlist) vs rootless/sysbox | high (design + validation) |
| #65 H8 [A] | app/data-tier `cap_drop:[ALL]` + `no-new-privileges` | Extends existing hardening to api/workers/postgres/mlflow | med (per-service boot validation) |
| #69(part) M12 [A] | cAdvisor: drop `privileged` → `cap_add:[SYS_PTRACE]` | Host-wide blast radius on a cAdvisor CVE | low-med |
| #69(part) M13 [A] | Digest-pin Dockerfile `FROM` bases | Closes the supply-chain gap the R4-20 effort missed | low |
| #69(part) M14 [A] | `migrate` resource limits | Hygiene; escaped the R5-M9 sweep | none |

**Decision needed (H9):** which isolation approach? Socket-proxy (consistent with autoheal, ~1 day) is the pragmatic pick; rootless/sysbox is stronger but a bigger infra change.

**Exit:** every network/untrusted-input container drops caps + blocks priv-esc; H9 sandbox spawner can no longer reach a full daemon.

---

## Phase 4 — Cleanup  (~1–2 days, low risk, high maintainability ROI — parallelizable)

Can run alongside any phase. Mostly deletion.

| # | Item | Notes |
|---|---|---|
| #70 M15 [V] | Remove dead deps `catboost`/`mapie`/`scipy`-direct/`dvc-s3` | ~100MB dead wheels. **pyarrow stays at 17.0.0** (T11 rejected). |
| #71 M16 [V] | Delete ~3,358 LOC dead modules (15) + `k8s/` + `k8s-training-job.yaml` | Per-module verify zero live callers + remove the test files that import them. **Decision: Airflow chain (auto_retrain→advanced→drift→registry_gates, ~1,036 LOC) keep or kill?** |
| #67(part) M6 [A] | `/forecasts` LIMIT + pagination | Unbounded read on the event loop |
| #68(part) M8/M10/M11 [A] | merged-parquet leak; `with_fallback` returns-zeros; SeasonalNaive NaN season | ML robustness; M9 cold-start z-score is latent (defer until cold-start is wired) |

**Exit:** dead surface removed; deps lean; remaining MED ML-robustness closed.

---

## Phase 5 — LOW batch  (opportunistic, #72)

Pick off alongside related work. Highest-value LOWs:
- **L1** OAuth `token`+`api_key` in redirect query string (cross-repo: backend → fragment/POST, frontend `history.replaceState` scrub) — do with any auth-path work.
- **L7** distinct `MODEL_SIGNING_KEY` in prod Lockbox · **L11** `lockbox_bootstrap.sh` eval key-name validation · **L13** retire legacy beget compose · **L14** dedup `_parse_origins` · **M3** JWT cross-worker revocation (shorter TTL / DB fallback).
Rest: hygiene, as-encountered.

---

## Decisions blocking the roadmap (need user input)

1. **Phase 2 — forecast-quality fixes require retraining existing client models.** OK to plan a controlled retrain + per-client before/after accuracy report? (vs leave H1/H2 and just document.)
2. **Phase 3 / H9 — docker.sock isolation approach:** scoped socket-proxy (pragmatic, ~1d) vs rootless/sysbox (stronger, bigger change)?
3. **Phase 4 / #71 — Airflow auto-retrain chain (~1,036 LOC behind a never-enabled profile):** delete or keep as documented opt-in?

## Suggested start
Phase 0 today (4 small low-risk PRs, two of them close live info-leaks), in parallel begin the Phase 2a backtest scaffold (it gates the highest-value work and takes the longest to set up). Phase 4 deletion can be slotted in whenever there's a low-focus window.
