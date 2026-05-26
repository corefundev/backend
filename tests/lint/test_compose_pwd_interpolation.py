"""Unit tests for tools/lint/compose_pwd_interpolation.py.

The linter forbids ${POSTGRES_PASSWORD} interpolation inside
docker-compose `command:` and `entrypoint:` blocks (R10 Phase 0-C
PR-1). Tests cover positive matches (string, list, multi-line),
negative cases (environment: blocks, comments), the baseline
mechanism, and the stale-baseline warning.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the linter importable as a module
_LINT_DIR = Path(__file__).resolve().parents[2] / "tools" / "lint"
sys.path.insert(0, str(_LINT_DIR))

import compose_pwd_interpolation as lint  # noqa: E402


# ── positive cases — must report ─────────────────────────────────────

def test_inline_string_command_violation():
    text = """\
services:
  mlflow:
    image: foo
    command: postgresql://sku:${POSTGRES_PASSWORD}@postgres:5432/db
"""
    v = lint.find_violations(text, "test.yml")
    assert len(v) == 1
    assert v[0][1] == 4
    assert v[0][2] == "command"


def test_multiline_folded_command_violation():
    text = """\
services:
  mlflow:
    command: >
      mlflow server
      --backend-store-uri postgresql://sku:${POSTGRES_PASSWORD}@postgres:5432/db
      --workers 1
"""
    v = lint.find_violations(text, "test.yml")
    assert len(v) == 1
    assert v[0][1] == 5  # the line with the pattern
    assert v[0][2] == "command"


def test_multiline_literal_command_violation():
    text = """\
services:
  worker:
    command: |
      set -e
      psql -c "ALTER USER sku WITH PASSWORD '${POSTGRES_PASSWORD}';"
"""
    v = lint.find_violations(text, "test.yml")
    assert len(v) == 1
    assert v[0][2] == "command"


def test_list_command_violation():
    text = """\
services:
  app:
    command: ["sh", "-c", "echo $POSTGRES_PASSWORD"]
"""
    v = lint.find_violations(text, "test.yml")
    assert len(v) == 1
    assert v[0][2] == "command"


def test_entrypoint_block_also_caught():
    text = """\
services:
  app:
    entrypoint: ["/bin/sh", "-c", "psql 'postgresql://sku:${POSTGRES_PASSWORD}@h/d'"]
"""
    v = lint.find_violations(text, "test.yml")
    assert len(v) == 1
    assert v[0][2] == "entrypoint"


def test_strict_gate_variant_caught():
    """${VAR:?...} (strict-gate) form must also be rejected — it carries
    the same trap."""
    text = """\
services:
  app:
    command: psql 'postgresql://sku:${POSTGRES_PASSWORD:?required}@h/d'
"""
    v = lint.find_violations(text, "test.yml")
    assert len(v) == 1


def test_replica_variant_caught():
    """The _REPLICA variant must also fire — same class of bug."""
    text = """\
services:
  app:
    command: psql 'postgresql://sku:${POSTGRES_PASSWORD_REPLICA}@r/d'
"""
    v = lint.find_violations(text, "test.yml")
    assert len(v) == 1


def test_bash_style_dollar_caught():
    """`$POSTGRES_PASSWORD` (no braces) is also a valid compose interpolation
    and must be flagged the same way."""
    text = """\
services:
  app:
    command: sh -c 'echo $POSTGRES_PASSWORD | psql'
"""
    v = lint.find_violations(text, "test.yml")
    assert len(v) == 1


# ── negative cases — must NOT report ────────────────────────────────

def test_environment_block_not_caught():
    """environment: vars are fine — they're container env, not CLI args,
    and Python apps (vault_agent) re-overlay at runtime."""
    text = """\
services:
  api:
    environment:
      DATABASE_URL: postgresql://sku:${POSTGRES_PASSWORD}@postgres:5432/db
"""
    v = lint.find_violations(text, "test.yml")
    assert v == []


def test_yaml_anchor_at_top_level_not_caught():
    """The top-level x-common-env anchor isn't a command/entrypoint context."""
    text = """\
x-common-env: &common-env
  DATABASE_URL: postgresql://sku:${POSTGRES_PASSWORD}@postgres:5432/db
services:
  app:
    image: foo
    environment:
      <<: *common-env
"""
    v = lint.find_violations(text, "test.yml")
    assert v == []


def test_full_line_comment_with_pattern_not_caught():
    text = """\
services:
  mlflow:
    # historical note: command used to have ${POSTGRES_PASSWORD} hardcoded
    command: mlflow server --workers 1
"""
    v = lint.find_violations(text, "test.yml")
    assert v == []


def test_inline_comment_with_pattern_not_caught():
    """If the pattern is ONLY inside an inline `# …` comment on a
    `command:` line, it must NOT count."""
    text = """\
services:
  mlflow:
    command: mlflow server   # later: postgresql://sku:${POSTGRES_PASSWORD}@ — DON'T
"""
    v = lint.find_violations(text, "test.yml")
    assert v == []


def test_inline_comment_after_real_violation_still_caught():
    """If the pattern is in BOTH the code and a trailing comment, the
    code half must still fire."""
    text = """\
services:
  app:
    command: psql 'postgresql://${POSTGRES_PASSWORD}@h/d'  # trap
"""
    v = lint.find_violations(text, "test.yml")
    assert len(v) == 1


def test_other_var_not_caught():
    text = """\
services:
  app:
    command: ['serve', '--addr=:${API_PORT}']
"""
    v = lint.find_violations(text, "test.yml")
    assert v == []


def test_command_followed_by_environment_does_not_leak_context():
    """Once we leave `command:` (sibling key at same/lesser indent), a
    POSTGRES_PASSWORD in the next block must NOT be attributed to
    command. Otherwise the indent-stack would be broken."""
    text = """\
services:
  app:
    command: mlflow server
    environment:
      DATABASE_URL: postgresql://sku:${POSTGRES_PASSWORD}@h/d
"""
    v = lint.find_violations(text, "test.yml")
    assert v == []


# ── baseline ────────────────────────────────────────────────────────

def test_baseline_suppresses_matched_entry(tmp_path):
    yml = tmp_path / "docker-compose.test.yml"
    yml.write_text("""\
services:
  legacy:
    command: psql 'postgresql://sku:${POSTGRES_PASSWORD}@h/d'
""")
    base = tmp_path / "baseline"
    base.write_text(f"{yml}:3  LEG-123  legacy service triaged in T9\n")

    rc = lint.main([str(yml), "--baseline", str(base)])
    assert rc == 0


def test_baseline_without_reason_rejected(tmp_path):
    base = tmp_path / "baseline"
    base.write_text("some/file.yml:42\n")  # no reason

    with pytest.raises(SystemExit):
        lint.parse_baseline(str(base))


def test_baseline_with_comments_and_blanks(tmp_path):
    base = tmp_path / "baseline"
    base.write_text(
        "# header comment\n"
        "\n"
        "  # indented comment\n"
        "foo.yml:7  REASON-X\n"
    )
    allowed = lint.parse_baseline(str(base))
    assert allowed == {("foo.yml", 7)}


def test_stale_baseline_emits_warning(tmp_path, capsys):
    yml = tmp_path / "docker-compose.clean.yml"
    yml.write_text("""\
services:
  api:
    command: gunicorn app:app
""")
    base = tmp_path / "baseline"
    base.write_text(f"{yml}:99  STALE-REASON\n")

    rc = lint.main([str(yml), "--baseline", str(base)])
    assert rc == 0
    err = capsys.readouterr().err
    assert "baseline contains entries that no longer match" in err
    assert "99" in err


# ── repo state — must be clean ──────────────────────────────────────

def test_real_compose_files_are_clean():
    """The repo's actual compose files must have zero violations as of
    the PR shipping this linter (R8-12 v2 + PR #28 already moved the
    last two trap sites — mlflow + postgres-exporter — into their
    entrypoint scripts)."""
    repo = Path(__file__).resolve().parents[2]
    files = sorted((repo / "docker").glob("docker-compose*.yml"))
    assert files, "no compose files found"
    violations = []
    for f in files:
        violations.extend(lint.find_violations(f.read_text(), str(f)))
    assert violations == [], \
        f"current compose tree has {len(violations)} violation(s): {violations}"
