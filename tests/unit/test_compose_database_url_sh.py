"""
tests/unit/test_compose_database_url_sh.py

R10 Phase 0-B PR-C (2026-05-31). Behavioural tests for the POSIX-sh
helper `scripts/compose_database_url.sh` that mirrors the Python
composer at `src/auth/vault_agent.py::_compose_database_urls_from_components`
in the backup-container script chain (cron_wrapper.sh, restore.sh).

The helper must:
  * be a no-op if DATABASE_URL is already set (idempotency)
  * compose DATABASE_URL from POSTGRES_PASSWORD + components if DATABASE_URL is empty
  * percent-encode user/password per RFC 3986 unreserved set
  * fail-loud (exit code 2 + stderr message) if BOTH DATABASE_URL AND
    POSTGRES_PASSWORD are missing — no silent return

Why behavioural-test the sh helper directly (not just lint): the awk-
based percent-encoder is non-trivial; a future "clean-up" that breaks
encoding (e.g. swap to `echo $pwd` without quoting, drop the LC_ALL=C)
could go un-noticed for months on `[A-Za-z0-9_-]{32}` passwords, then
silently break on the first rotation that introduces a reserved char.
The Python composer has the same encoding behaviour pinned by
test_compose_url_encodes_special_chars_in_pwd — this is the sh mirror.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2]
_HELPER = _BACKEND / "scripts" / "compose_database_url.sh"


def _run_helper(env: dict[str, str]) -> subprocess.CompletedProcess:
    """Source the helper in a fresh sh and print the resulting
    DATABASE_URL. Return the completed process for stdout/stderr/code
    inspection."""
    assert _HELPER.is_file(), f"helper not found: {_HELPER}"
    # `set -eu` echoes the helper-side fail-loud; we expect exit 2 in
    # the missing-everything case, so we don't pre-set -e.
    script = f". {_HELPER}; compose_database_url; ec=$?; echo \"DATABASE_URL=${{DATABASE_URL:-<unset>}}\"; exit $ec"
    return subprocess.run(
        ["sh", "-c", script],
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **env},
        capture_output=True,
        text=True,
    )


# ── idempotency ─────────────────────────────────────────────────────


def test_helper_preserves_existing_database_url():
    """If DATABASE_URL is already set, the helper must not overwrite it."""
    res = _run_helper({"DATABASE_URL": "postgresql://pre:set@h:1/d"})
    assert res.returncode == 0, res.stderr
    assert "DATABASE_URL=postgresql://pre:set@h:1/d" in res.stdout


# ── composition from components ─────────────────────────────────────


def test_helper_composes_from_components_with_defaults():
    """POSTGRES_PASSWORD + no component overrides → defaults
    sku/postgres/5432/sku_forecasting (matches Python composer)."""
    res = _run_helper({"POSTGRES_PASSWORD": "secret-pwd"})
    assert res.returncode == 0, res.stderr
    assert "DATABASE_URL=postgresql://sku:secret-pwd@postgres:5432/sku_forecasting" in res.stdout


def test_helper_composes_with_all_components():
    """Full component override matches Python composer behaviour."""
    res = _run_helper({
        "POSTGRES_PASSWORD": "p",
        "DB_USER": "appuser",
        "DB_HOST": "db.example.com",
        "DB_PORT": "5433",
        "DB_NAME": "appdb",
    })
    assert res.returncode == 0, res.stderr
    assert "DATABASE_URL=postgresql://appuser:p@db.example.com:5433/appdb" in res.stdout


# ── percent-encoding ────────────────────────────────────────────────


def test_helper_url_encodes_reserved_chars_in_pwd():
    """Defensive — mirrors Python composer's
    test_compose_url_encodes_special_chars_in_pwd. Reserved URL chars
    must be percent-encoded so the DSN parses correctly."""
    res = _run_helper({"POSTGRES_PASSWORD": "p@ss:wd/foo"})
    assert res.returncode == 0, res.stderr
    # `@`, `:`, `/` in the userinfo section must become %40, %3A, %2F.
    assert "DATABASE_URL=postgresql://sku:p%40ss%3Awd%2Ffoo@postgres:5432/sku_forecasting" in res.stdout


def test_helper_does_not_double_encode_unreserved_chars():
    """Current rotation policy pwd shape `[A-Za-z0-9_-]{32}` must pass
    through unchanged (no false positives in the encoder)."""
    pwd = "Az09_-AzAz09__--Az_09Az_09Az_-Az"  # 32 chars from the allowed set
    res = _run_helper({"POSTGRES_PASSWORD": pwd})
    assert res.returncode == 0, res.stderr
    assert f"DATABASE_URL=postgresql://sku:{pwd}@postgres:5432/sku_forecasting" in res.stdout


# ── fail-loud ───────────────────────────────────────────────────────


def test_helper_fails_loud_when_everything_unset():
    """Both DATABASE_URL AND POSTGRES_PASSWORD missing → exit 2 + a
    diagnostic message naming POSTGRES_PASSWORD, Lockbox, and the
    allowlist gate so the operator can fix without source-diving."""
    res = _run_helper({})
    assert res.returncode == 2, (
        f"expected exit 2 (fail-loud), got {res.returncode}\n"
        f"stdout: {res.stdout}\nstderr: {res.stderr}"
    )
    err = res.stderr
    assert "POSTGRES_PASSWORD" in err, "stderr must name the missing var"
    assert "Lockbox" in err, "stderr must point at Lockbox configuration"
    assert "LOCKBOX_ALLOWED_KEYS" in err, "stderr must mention the allowlist gate"


# ── parity with Python composer ─────────────────────────────────────


def test_helper_compose_matches_python_composer_baseline(monkeypatch):
    """End-to-end parity: same components → same DSN as the Python
    composer. Catches the class of bug where someone changes one side
    but not the other (defaults drift, encoding rules drift, …)."""
    pytest.importorskip("src.auth.vault_agent")
    from src.auth import vault_agent

    for key in (
        "POSTGRES_PASSWORD", "POSTGRES_PASSWORD_REPLICA",
        "DB_USER", "DB_HOST", "DB_PORT", "DB_NAME",
        "DB_HOST_REPLICA",
        "DATABASE_URL", "DATABASE_URL_REPLICA",
    ):
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("POSTGRES_PASSWORD", "test-pwd-123")
    monkeypatch.setenv("DB_HOST", "h.example.com")
    monkeypatch.setenv("DB_PORT", "5433")
    monkeypatch.setenv("DB_NAME", "appdb")
    monkeypatch.setenv("DB_USER", "appuser")

    vault_agent._compose_database_urls_from_components()
    python_dsn = os.environ["DATABASE_URL"]

    res = _run_helper({
        "POSTGRES_PASSWORD": "test-pwd-123",
        "DB_HOST": "h.example.com",
        "DB_PORT": "5433",
        "DB_NAME": "appdb",
        "DB_USER": "appuser",
    })
    assert res.returncode == 0, res.stderr
    sh_dsn = next(
        (line.split("=", 1)[1] for line in res.stdout.splitlines() if line.startswith("DATABASE_URL=")),
        None,
    )
    assert sh_dsn == python_dsn, (
        f"sh/Python composer drift:\n  sh:     {sh_dsn!r}\n  python: {python_dsn!r}"
    )
