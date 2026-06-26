#!/usr/bin/env python3
"""SFU — settings-field-usage linter (C1.4 / #174).

Guards `src/settings.py` against the two failure modes that let ~95 dead
fields accumulate unnoticed until C1.1 (#171) removed them:

  GUARD A — dead field: every field declared on the `Settings` class must
    have at least one reader — an attribute access `<alias>.<field>`
    somewhere under the scanned tree, via ANY import alias of the
    singleton (`from src.settings import settings [as _settings]`).
    flake8 / vulture cannot catch this: pydantic's metaclass "uses" every
    field through introspection, so an unread field looks live to them.

  GUARD B — env shadow: if a field `foo` exists, its env var `FOO`
    (`foo.upper()`) MUST be read through `settings.foo`, never directly
    via `os.environ.get("FOO")` / `os.getenv("FOO")` / `os.environ["FOO"]`.
    A direct env read alongside a settings field is the divergent-default
    bug class (login_lockout_threshold was settings=5 vs os.environ
    default=10 — found during C1.1).

Both checks are AST-based, NOT text/substring: a docstring example like
`int(os.environ.get("LOGIN_ATTEMPT_PER_HOUR_PER_SUBNET", "20"))` lives in a
string constant with no Call node, so it does not false-match (the lesson
behind feedback_config_test_parse_not_substring).

A field with a legitimate non-attribute reader (accessed only via
`getattr(settings, computed_name)` or enumerated by `model_dump()`) can be
allowlisted via --allow; each entry must carry a reason, mirroring the NFOR
baseline so the allowlist stays auditable.

Exit code 0 clean, 1 on violations, 2 on usage error.

Usage:
    python tools/lint/settings_field_usage.py src/
    python tools/lint/settings_field_usage.py src/ \
        --settings-file src/settings.py \
        --allow tools/lint/.settings_field_usage.allow
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

CODE_DEAD = "SFU001"   # declared field with no reader
CODE_SHADOW = "SFU002"  # field's env var read directly via os.environ

SETTINGS_CLASS = "Settings"
_ENV_GET_FUNCS = {"os.environ.get", "os.getenv"}


@dataclass(frozen=True)
class Violation:
    code: str
    path: str
    line: int
    field: str
    message: str

    def format_text(self) -> str:
        return f"{self.path}:{self.line}: {self.code} {self.message}"

    def to_json(self) -> dict:
        return {
            "code": self.code,
            "path": self.path,
            "line": self.line,
            "field": self.field,
            "message": self.message,
        }


def _attr_dotted_name(node: ast.AST) -> str | None:
    """Return dotted name `a.b.c` for an Attribute/Name chain, else None."""
    parts: list[str] = []
    cur: ast.AST | None = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return None


def _const_str(node: ast.AST | None) -> str | None:
    """Return the string value if `node` is a string Constant, else None.
    Since Python 3.9 a subscript slice is the expression directly (no
    `ast.Index` wrapper), so a plain Constant check suffices."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


# ── settings.py field extraction (authoritative field set) ─────────────────

def extract_settings_fields(source: str) -> set[str]:
    """Field names declared on the `Settings` class — every annotated
    assignment `name: type [= default]` whose target is a bare Name.
    `model_config = SettingsConfigDict(...)` is a plain (un-annotated)
    Assign, so it is naturally excluded."""
    tree = ast.parse(source)
    fields: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == SETTINGS_CLASS:
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    fields.add(stmt.target.id)
    return fields


# ── per-file scans ─────────────────────────────────────────────────────────

def settings_aliases(tree: ast.AST) -> set[str]:
    """Local names the settings singleton is bound to in this module —
    `from …settings import settings` → {"settings"};
    `from …settings import settings as _settings` → {"_settings"}."""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.endswith("settings"):
            for a in node.names:
                if a.name == "settings":
                    aliases.add(a.asname or a.name)
    return aliases


def find_field_reads(tree: ast.AST, aliases: set[str]) -> set[str]:
    """Attribute names read off any settings alias: `<alias>.<attr>`."""
    reads: set[str] = set()
    if not aliases:
        return reads
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in aliases
        ):
            reads.add(node.attr)
    return reads


def find_env_shadow_reads(
    tree: ast.AST, env_to_field: dict[str, str], path: str
) -> Iterator[Violation]:
    """Direct os.environ reads whose key matches a settings field's env var."""
    for node in ast.walk(tree):
        key: str | None = None
        if isinstance(node, ast.Call):
            name = _attr_dotted_name(node.func)
            if name in _ENV_GET_FUNCS and node.args:
                key = _const_str(node.args[0])
        elif isinstance(node, ast.Subscript):
            if _attr_dotted_name(node.value) == "os.environ":
                key = _const_str(node.slice)
        if key is not None and key in env_to_field:
            field = env_to_field[key]
            yield Violation(
                code=CODE_SHADOW,
                path=path,
                line=getattr(node, "lineno", 0),
                field=field,
                message=(
                    f"os.environ read of `{key}` shadows settings field "
                    f"`{field}` — read it via `settings.{field}` so the value "
                    f"can't diverge from the field default"
                ),
            )


# ── allowlist (GUARD A exemptions) ─────────────────────────────────────────

def load_allowlist(path: Path) -> dict[str, str]:
    """`field_name  reason` per line; blank / `#` ignored. A reason is
    mandatory so the allowlist stays auditable (mirrors NFOR baseline)."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "  " not in line:
            print(f"{path}: allow entry without reason rejected: {raw!r}", file=sys.stderr)
            sys.exit(2)
        field, reason = line.split("  ", 1)
        if not reason.strip():
            print(f"{path}: allow entry without reason rejected: {raw!r}", file=sys.stderr)
            sys.exit(2)
        out[field.strip()] = reason.strip()
    return out


# ── driver ─────────────────────────────────────────────────────────────────

def iter_python_files(root: Path, exclude: set[Path]) -> Iterator[Path]:
    candidates = [root] if root.is_file() else root.rglob("*.py")
    for p in candidates:
        if p.suffix != ".py":
            continue
        if set(p.parts) & {"__pycache__", ".venv", "venv", ".git", "build", "dist", "node_modules"}:
            continue
        if p.resolve() in exclude:
            continue
        yield p


def check(
    paths: list[Path], settings_file: Path, allow: dict[str, str]
) -> tuple[list[Violation], set[str]]:
    fields = extract_settings_fields(settings_file.read_text(encoding="utf-8"))
    env_to_field = {f.upper(): f for f in fields}
    exclude = {settings_file.resolve()}

    read_fields: set[str] = set()
    shadow_violations: list[Violation] = []
    for root in paths:
        for fp in iter_python_files(root, exclude):
            try:
                tree = ast.parse(fp.read_text(encoding="utf-8"))
            except SyntaxError as e:
                print(f"{fp}: SFU000 syntax error: {e}", file=sys.stderr)
                continue
            spath = str(fp)
            read_fields |= find_field_reads(tree, settings_aliases(tree))
            shadow_violations.extend(find_env_shadow_reads(tree, env_to_field, spath))

    violations = list(shadow_violations)
    dead = sorted(fields - read_fields - set(allow))
    for f in dead:
        violations.append(
            Violation(
                code=CODE_DEAD,
                path=str(settings_file),
                line=0,
                field=f,
                message=(
                    f"settings field `{f}` has no reader (`settings.{f}` is read "
                    f"nowhere) — wire it to a consumer in the same PR, remove it, "
                    f"or allowlist it with a reason"
                ),
            )
        )
    stale_allow = set(allow) - (fields - read_fields)
    return violations, stale_allow


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="settings-field-usage", description=__doc__)
    parser.add_argument("paths", nargs="+", help="files or directories to scan")
    parser.add_argument("--settings-file", type=Path, default=Path("src/settings.py"))
    parser.add_argument("--allow", type=Path, help="allowlist of intentionally-unread fields")
    parser.add_argument("--json", action="store_true", help="emit findings as JSON")
    args = parser.parse_args(argv)

    if not args.settings_file.exists():
        print(f"{args.settings_file}: no such file", file=sys.stderr)
        return 2
    roots: list[Path] = []
    for raw in args.paths:
        p = Path(raw)
        if not p.exists():
            print(f"{p}: no such file or directory", file=sys.stderr)
            return 2
        roots.append(p)

    allow = load_allowlist(args.allow) if args.allow else {}
    violations, stale_allow = check(roots, args.settings_file, allow)

    if args.json:
        print(json.dumps([v.to_json() for v in violations], indent=2))
    else:
        for v in violations:
            print(v.format_text())
        if stale_allow:
            print(
                f"\nWARNING: allowlist has entries that are now read (remove them): "
                f"{sorted(stale_allow)}",
                file=sys.stderr,
            )

    if violations or stale_allow:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
