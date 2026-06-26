"""Unit tests for the SFU (settings-field-usage) linter — C1.4 / #174.

Each test exercises one predicate of the guard that replaces the blind
spot which let ~95 dead settings fields accumulate before C1.1 (#171):
flake8/vulture can't see unread pydantic fields, and a direct os.environ
read can silently diverge from a settings field default.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from tools.lint.settings_field_usage import (  # noqa: E402
    check,
    extract_settings_fields,
    load_allowlist,
    main,
)

_SETTINGS_HEADER = "from pydantic_settings import BaseSettings\n\n\nclass Settings(BaseSettings):\n"


def _write(tmp_path: Path, settings_body: str, *consumers: str) -> tuple[Path, Path]:
    """Materialise a settings file + consumer files; return (src_dir, settings_file)."""
    src = tmp_path / "src"
    src.mkdir(parents=True)
    settings_file = src / "settings.py"
    settings_file.write_text(_SETTINGS_HEADER + settings_body + "\n\nsettings = Settings()\n")
    for i, c in enumerate(consumers):
        (src / f"consumer_{i}.py").write_text(c)
    return src, settings_file


def _codes(violations) -> list[str]:
    return sorted(v.code for v in violations)


# ── GUARD A — dead field ────────────────────────────────────────────────────

def test_field_with_reader_is_clean(tmp_path):
    src, sf = _write(
        tmp_path,
        "    used: int = 1\n",
        "from src.settings import settings\nx = settings.used\n",
    )
    violations, stale = check([src], sf, {})
    assert violations == [] and stale == set()


def test_dead_field_is_flagged(tmp_path):
    src, sf = _write(
        tmp_path,
        "    used: int = 1\n    dead: int = 2\n",
        "from src.settings import settings\nx = settings.used\n",
    )
    violations, _ = check([src], sf, {})
    assert _codes(violations) == ["SFU001"]
    assert violations[0].field == "dead"


def test_reader_via_import_alias_counts(tmp_path):
    src, sf = _write(
        tmp_path,
        "    used: int = 1\n",
        "from src.settings import settings as _s\nx = _s.used\n",
    )
    violations, _ = check([src], sf, {})
    assert violations == []


def test_model_config_is_not_a_field(tmp_path):
    # model_config is an un-annotated Assign — must not be treated as a field.
    body = "    model_config = {}\n    used: int = 1\n"
    fields = extract_settings_fields(_SETTINGS_HEADER + body)
    assert fields == {"used"}


def test_allowlist_suppresses_dead_field(tmp_path):
    src, sf = _write(
        tmp_path,
        "    used: int = 1\n    via_getattr: int = 2\n",
        "from src.settings import settings\nx = settings.used\n",
    )
    violations, stale = check([src], sf, {"via_getattr": "read via getattr in ops dump"})
    assert violations == [] and stale == set()


def test_stale_allowlist_entry_is_reported(tmp_path):
    # `used` IS read, so allowlisting it is stale → must be surfaced.
    src, sf = _write(
        tmp_path,
        "    used: int = 1\n",
        "from src.settings import settings\nx = settings.used\n",
    )
    violations, stale = check([src], sf, {"used": "no longer needed"})
    assert stale == {"used"}


# ── GUARD B — env shadow ────────────────────────────────────────────────────

def test_env_shadow_get_is_flagged(tmp_path):
    src, sf = _write(
        tmp_path,
        "    used: int = 1\n",
        "import os\nfrom src.settings import settings\n"
        'x = settings.used\nv = os.environ.get("USED")\n',
    )
    violations, _ = check([src], sf, {})
    assert _codes(violations) == ["SFU002"]
    assert violations[0].field == "used"


def test_env_shadow_subscript_is_flagged(tmp_path):
    src, sf = _write(
        tmp_path,
        "    used: int = 1\n",
        "import os\nfrom src.settings import settings\n"
        'x = settings.used\nv = os.environ["USED"]\n',
    )
    violations, _ = check([src], sf, {})
    assert _codes(violations) == ["SFU002"]


def test_env_shadow_in_docstring_is_not_flagged(tmp_path):
    # The settings.py migration docstring legitimately shows the "before"
    # os.environ.get example — a string constant, no Call node → no match.
    src, sf = _write(
        tmp_path,
        "    used: int = 1\n",
        "from src.settings import settings\n"
        'x = settings.used\n'
        '"""migrate: int(os.environ.get(\\"USED\\", \\"1\\"))"""\n',
    )
    violations, _ = check([src], sf, {})
    assert violations == []


# ── allowlist hygiene ───────────────────────────────────────────────────────

def test_allowlist_entry_without_reason_is_rejected(tmp_path):
    allow = tmp_path / ".allow"
    allow.write_text("lonely_field_without_reason\n")
    with pytest.raises(SystemExit) as exc:
        load_allowlist(allow)
    assert exc.value.code == 2


def test_main_exit_codes(tmp_path):
    src, sf = _write(
        tmp_path,
        "    used: int = 1\n    dead: int = 2\n",
        "from src.settings import settings\nx = settings.used\n",
    )
    assert main([str(src), "--settings-file", str(sf)]) == 1   # dead field
    src2, sf2 = _write(
        tmp_path / "clean",
        "    used: int = 1\n",
        "from src.settings import settings\nx = settings.used\n",
    )
    assert main([str(src2), "--settings-file", str(sf2)]) == 0
