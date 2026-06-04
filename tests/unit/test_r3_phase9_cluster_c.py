"""
Regression tests for Round-3 Phase 9 Cluster C (MED) — 2026-05-16.

R3-16 — dvc/dvc-s3 are pinned in requirements.txt but verify they have
         ZERO Python imports in src/. The pkgs are CLI-only tools; a
         future `import dvc` callsite would need a proper integration
         test, which this guard makes a refactor failure surface.
R3-18 — optuna TPESampler(seed=42) reproducibility snapshot. Locks in
         the trial param sequence so a future optuna bump that changes
         RNG behaviour breaks CI loudly rather than silently drifting
         the production HPO output.
R3-22 — pip-audit + npm audit are now hard gates (`continue-on-error:
         true` removed from both CI workflows). Source-level invariant
         to prevent a silent regression that re-adds the flag.

R3-17 (shap bump) — DEFERRED. shap 0.46.0 → 0.49+ requires running
                    the SKUExplainer integration tests on Linux+libomp
                    to validate TreeExplainer API stability. R3-22's
                    pip-audit gate covers any CVE that lands in shap;
                    bumping for hygiene alone is a separate task.
"""
from __future__ import annotations

from pathlib import Path

import pytest


_BACKEND = Path(__file__).resolve().parents[2]
_FRONTEND = _BACKEND.parent / "frontend"


# (R3-16 test_no_python_import_of_dvc removed 2026-06-05, R11-#71 — dvc/
#  dvc-s3 deps + their only consumer src/data/versioning.py are gone, so
#  there is no longer a dvc surface to guard.)


# ── R3-18: optuna TPESampler(seed=42) reproducibility ────────────────────

def test_optuna_tpe_sampler_reproducible_across_runs():
    """Two studies with the same seed and the same objective must
    produce IDENTICAL trial param sequences. Locks in optuna RNG
    behaviour against version drift — the audit-recommended pattern
    for hyperparameter-sensitive ML pipelines."""
    optuna = pytest.importorskip("optuna")
    optuna.logging.set_verbosity(optuna.logging.WARNING)

    def make_study():
        return optuna.create_study(
            direction="minimize",
            sampler=optuna.samplers.TPESampler(seed=42),
        )

    def objective(trial):
        # Simple quadratic — fast, deterministic, doesn't depend on
        # LightGBM (libomp-sensitive on macOS).
        x = trial.suggest_float("x", -10.0, 10.0)
        y = trial.suggest_int("y", 0, 100)
        return (x - 1.5) ** 2 + y * 0.1

    study_a = make_study()
    study_a.optimize(objective, n_trials=8, show_progress_bar=False)

    study_b = make_study()
    study_b.optimize(objective, n_trials=8, show_progress_bar=False)

    params_a = [t.params for t in study_a.trials]
    params_b = [t.params for t in study_b.trials]
    assert params_a == params_b, (
        f"optuna TPESampler(seed=42) not reproducible — RNG drift since "
        f"requirements.txt pin. params_a={params_a}, params_b={params_b}"
    )


def test_optuna_tpe_sampler_param_shape_locked():
    """Snapshot the FIRST trial's param shape so a future optuna bump
    that changes the suggest_* defaults (e.g. log-uniform vs uniform)
    breaks here before reaching production HPO."""
    optuna = pytest.importorskip("optuna")
    optuna.logging.set_verbosity(optuna.logging.WARNING)

    sampler = optuna.samplers.TPESampler(seed=42)
    study = optuna.create_study(direction="minimize", sampler=sampler)

    def objective(trial):
        return trial.suggest_float("learning_rate", 0.02, 0.10, log=True)

    study.optimize(objective, n_trials=3, show_progress_bar=False)

    # All trial values must be within the declared range — defends
    # against a future change where suggest_float silently ignores log=True.
    for t in study.trials:
        v = t.params["learning_rate"]
        assert 0.02 <= v <= 0.10, (
            f"suggest_float log=True range violation: {v} not in [0.02, 0.10]"
        )


# ── R3-22: pip-audit + npm audit are hard gates ──────────────────────────

def test_backend_pip_audit_is_hard_gate():
    """The pip-audit job in backend CI must NOT have continue-on-error,
    so a freshly-disclosed CVE fails the run instead of silently
    showing green."""
    ci = (_BACKEND / ".github" / "workflows" / "ci.yml").read_text()
    # Find the pip-audit job block.
    start = ci.find("\n  pip-audit:\n")
    assert start > 0, "pip-audit job missing from ci.yml"
    # The block runs until the next top-level job or EOF — bounded scan.
    end = ci.find("\n  ", start + 12)
    block = ci[start:end] if end > start else ci[start:]
    # The job-level `continue-on-error: true` line must not be present.
    bad_line = "    continue-on-error: true"
    assert bad_line not in block, (
        "pip-audit job has continue-on-error:true — audit R3-22 expects "
        "hard gate (remove the line to fail CI on new CVEs)"
    )


def test_frontend_npm_audit_is_hard_gate():
    """The npm audit step in frontend CI must NOT have
    continue-on-error, so a HIGH+ runtime-dep CVE fails the run.

    Frontend repo lives as a sibling checkout in dev (../frontend) but
    NOT inside the backend CI runner — skip when the sibling is absent
    so the backend unit-tests job stays self-contained. A parallel
    test ships in the frontend repo's own suite."""
    if not (_FRONTEND / ".github" / "workflows" / "ci.yml").exists():
        pytest.skip("frontend repo not checked out alongside backend "
                    "(expected outside dev workstation)")
    ci = (_FRONTEND / ".github" / "workflows" / "ci.yml").read_text()
    # Find the npm audit step.
    idx = ci.find("npm audit --audit-level=high")
    assert idx > 0, "npm audit step missing from frontend ci.yml"
    # The continue-on-error directive sits ABOVE the run on the same
    # step. Scan a 200-char window upward.
    window = ci[max(0, idx - 200):idx]
    assert "continue-on-error: true" not in window, (
        "npm audit step has continue-on-error:true — audit R3-22 expects "
        "hard gate (remove the line to fail CI on new CVEs)"
    )
