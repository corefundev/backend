"""
tests/lint/test_placeholder_constant_consistency.py

R10 Phase 0-C PR-4 — pure-static guard: the placeholder string
`_LOCKBOX_NOT_INJECTED_DO_NOT_USE_PG_PASSWORD` MUST appear identically
in every file that documents or enforces it. Without this gate, the
constant could drift between `.env.example` (operator-visible),
`docker/docker-compose.yml` (compose strict-gate error message),
`scripts/mlflow_entrypoint.sh` (runtime placeholder-rejection case),
`scripts/cd_deploy.sh` (operator-bootstrap NOTE), and the integration
test that proves postgres rejects it.

Drift in even one of those would: leave the operator with stale
documentation, OR fail the rejection-check in entrypoints that look
for an outdated literal, OR leave the integration gate testing the
wrong string. Static check is fast (~10 ms) and catches the whole
class of regressions.
"""
from __future__ import annotations

import re
from pathlib import Path


_BACKEND = Path(__file__).resolve().parents[2]

# Single source of truth for this test. The integration test in
# tests/integration/test_placeholder_pg_auth_rejected.py defines the
# same constant — this lint asserts both stay in sync with the real
# files.
PLACEHOLDER = "_LOCKBOX_NOT_INJECTED_DO_NOT_USE_PG_PASSWORD"


# Files that MUST contain the placeholder verbatim, and why.
EXPECTED_OCCURRENCES = {
    ".env.example":
        "Operator-visible placeholder in the .env template — the "
        "actual line that gets copied to /srv/backend/.env on VPS "
        "bootstrap.",
    "docker/docker-compose.yml":
        "compose strict-gate error message points the operator at "
        "the exact placeholder line they need to add.",
    "scripts/mlflow_entrypoint.sh":
        "Runtime placeholder-rejection: the `case` statement that "
        "FATAL-exits if the entrypoint ever sees this string in "
        "POSTGRES_PASSWORD (which would mean Lockbox bootstrap "
        "failed silently).",
    "scripts/cd_deploy.sh":
        "Operator-bootstrap NOTE block with the one-shot `echo "
        "POSTGRES_PASSWORD=… >> .env` command.",
    "tests/integration/test_placeholder_pg_auth_rejected.py":
        "Integration gate: asserts postgres SCRAM rejects this exact "
        "string. The test is the authoritative property check.",
}


def test_placeholder_appears_in_every_expected_file():
    missing = []
    for relpath, reason in EXPECTED_OCCURRENCES.items():
        path = _BACKEND / relpath
        assert path.exists(), (
            f"expected file missing: {relpath} (reason: {reason})"
        )
        content = path.read_text(encoding="utf-8")
        if PLACEHOLDER not in content:
            missing.append((relpath, reason))
    assert not missing, (
        "placeholder constant drifted out of these files:\n"
        + "\n".join(f"  {p}: {r}" for p, r in missing)
        + f"\nExpected literal: {PLACEHOLDER!r}"
    )


def test_legacy_seed_sku_is_no_longer_documented_as_active():
    """The pre-Phase-0-C placeholder was the bare string `sku`. After
    PR-3 ships, no operator-facing doc should claim `sku` is the
    current placeholder — that would mislead readers into using a
    short, easily-typed-by-accident, low-entropy value as the seed.

    Comments may still REFERENCE `sku` historically (e.g.
    "legacy `sku` seed was replaced 2026-05-27") — those don't count
    as documenting it as active. The test only fires on assignment
    forms `POSTGRES_PASSWORD=sku` outside of comment context."""
    # Match `POSTGRES_PASSWORD=sku` at start-of-line (potential .env
    # assignment) or in a docker-compose `${POSTGRES_PASSWORD:-sku}`
    # default fallback.
    pattern = re.compile(
        r"(^POSTGRES_PASSWORD=sku\b)"
        r"|"
        r"(\$\{POSTGRES_PASSWORD:-sku\})",
        re.MULTILINE,
    )

    bad = []
    for relpath in (".env.example", "docker/docker-compose.yml",
                    "scripts/cd_deploy.sh", "scripts/mlflow_entrypoint.sh"):
        path = _BACKEND / relpath
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8")
        for m in pattern.finditer(content):
            # Determine if the match is in a comment line.
            line_start = content.rfind("\n", 0, m.start()) + 1
            nl = content.find("\n", m.end())
            line = content[line_start:nl] if nl >= 0 else content[line_start:]
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("//"):
                continue
            bad.append((relpath, m.group(0)))

    assert not bad, (
        "legacy `sku` placeholder still documented as the active "
        "value in non-comment context:\n"
        + "\n".join(f"  {p}: {m!r}" for p, m in bad)
        + "\nReplace with _LOCKBOX_NOT_INJECTED_DO_NOT_USE_PG_PASSWORD."
    )
