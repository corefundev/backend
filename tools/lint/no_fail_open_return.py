#!/usr/bin/env python3
"""NFOR — no-fail-open-return AST linter.

Detects the "exception-swallow leading to fail-open" pattern that was the
root cause of R8-12 (MLflow run_id stayed NULL because `mlflow.log_artifact`
exceptions were caught, logged as WARNING, and the function returned a
non-error value).

A violation is reported when ALL of the following hold:

  1. Inside a function or method (not module level)
  2. A `try` block has an `except` handler that catches `Exception`,
     `BaseException`, or is a bare `except:` (broad catch)
  3. The handler body is "log-only" — it neither re-raises, nor returns
     an error value, nor exits the process, nor records the error to
     persistent state
  4. The enclosing function has at least one `return` of a non-None value
     (i.e. callers may treat the post-except path as success)

Approved fail-open sites can be added to a baseline file via `--baseline`.
Each baseline entry MUST carry a short reason — the linter rejects
entries without one to keep the allowlist auditable.

Exit code 0 on clean run, 1 on violations not covered by the baseline,
2 on usage error.

Usage:
    python tools/lint/no_fail_open_return.py src/
    python tools/lint/no_fail_open_return.py src/ --baseline tools/lint/.no_fail_open_return.baseline
    python tools/lint/no_fail_open_return.py src/foo.py --json
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

CODE = "NFOR001"
BROAD_CATCH_NAMES = {"Exception", "BaseException"}
LOG_CALL_PREFIXES = ("logger.", "logging.", "log.", "self.logger.", "self.log.")
EXIT_FUNCS = {"sys.exit", "os._exit", "os.abort"}


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    func_name: str
    message: str

    def location_key(self) -> str:
        return f"{self.path}:{self.line}"

    def format_text(self) -> str:
        return f"{self.path}:{self.line}: {CODE} {self.message}"

    def to_json(self) -> dict:
        return {
            "code": CODE,
            "path": self.path,
            "line": self.line,
            "function": self.func_name,
            "message": self.message,
        }


def _attr_dotted_name(node: ast.AST) -> str | None:
    """Return dotted name `a.b.c` for Attribute/Name chains, else None."""
    parts: list[str] = []
    cur: ast.AST | None = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return None


def _is_log_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    name = _attr_dotted_name(node.func)
    if name is None:
        return False
    if name == "print":
        return True
    return any(name.startswith(p) for p in LOG_CALL_PREFIXES)


def _is_exit_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    name = _attr_dotted_name(node.func)
    return name in EXIT_FUNCS


def _walk_within_func(node: ast.AST) -> Iterator[ast.AST]:
    """Walk descendants of `node` but stop at nested function / lambda
    boundaries — so a violation inside an inner function is attributed
    to that inner function, not double-counted at the outer one."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        yield child
        yield from _walk_within_func(child)


def _has_raise(body: Iterable[ast.stmt]) -> bool:
    for stmt in body:
        if isinstance(stmt, ast.Raise):
            return True
        for sub in _walk_within_func(stmt):
            if isinstance(sub, ast.Raise):
                return True
    return False


def _has_nontrivial_return(body: Iterable[ast.stmt]) -> bool:
    """A return statement that does NOT return None / empty."""
    def _is_nontrivial_return(n: ast.AST) -> bool:
        if not isinstance(n, ast.Return):
            return False
        if n.value is None:
            return False
        if isinstance(n.value, ast.Constant) and n.value.value is None:
            return False
        return True

    for stmt in body:
        if _is_nontrivial_return(stmt):
            return True
        for sub in _walk_within_func(stmt):
            if _is_nontrivial_return(sub):
                return True
    return False


def _has_exit_call(body: Iterable[ast.stmt]) -> bool:
    for stmt in body:
        if _is_exit_call(stmt):
            return True
        for sub in _walk_within_func(stmt):
            if _is_exit_call(sub):
                return True
    return False


def _is_broad_except(handler: ast.ExceptHandler) -> bool:
    """Bare `except:` or `except Exception:` / `except BaseException:`."""
    exc_type = handler.type
    if exc_type is None:
        return True
    if isinstance(exc_type, ast.Name) and exc_type.id in BROAD_CATCH_NAMES:
        return True
    if isinstance(exc_type, ast.Tuple):
        for elt in exc_type.elts:
            if isinstance(elt, ast.Name) and elt.id in BROAD_CATCH_NAMES:
                return True
    return False


def _handler_is_log_only(handler: ast.ExceptHandler) -> bool:
    """The handler body neither raises, returns an error, exits, nor calls anything
    other than logging / pass / docstring / assignment. Logging-only ⇒ fail-open."""
    if _has_raise(handler.body):
        return False
    if _has_nontrivial_return(handler.body):
        return False
    if _has_exit_call(handler.body):
        return False
    # If every statement is benign (log/pass/expr-doc/assign/comment), it's log-only.
    for stmt in handler.body:
        if isinstance(stmt, ast.Pass):
            continue
        if isinstance(stmt, ast.Assign):
            continue
        if isinstance(stmt, ast.AnnAssign):
            continue
        if isinstance(stmt, ast.AugAssign):
            continue
        if isinstance(stmt, ast.Expr):
            value = stmt.value
            if isinstance(value, ast.Constant):
                continue  # docstring or bare string expression
            if _is_log_call(value):
                continue
            return False
        if isinstance(stmt, (ast.If, ast.For, ast.While, ast.With, ast.Try)):
            # If any branch raises / returns / exits, treat as not log-only.
            if (
                _has_raise([stmt])
                or _has_nontrivial_return([stmt])
                or _has_exit_call([stmt])
            ):
                return False
            return False  # Non-trivial control flow inside handler — too complex to allow.
        return False
    return True


def _try_blocks_in_func(func: ast.FunctionDef | ast.AsyncFunctionDef) -> Iterator[ast.Try]:
    """`try` blocks defined directly in this function body — not in nested defs."""
    for sub in _walk_within_func(func):
        if isinstance(sub, ast.Try):
            yield sub


def _func_returns_nonnone(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Function returns a non-None value at the top of its body or in a non-nested
    control-flow branch. Nested function bodies are excluded from this check."""
    for sub in _walk_within_func(func):
        if isinstance(sub, ast.Return):
            value = sub.value
            if value is None:
                continue
            if isinstance(value, ast.Constant) and value.value is None:
                continue
            return True
    return False


def _line_has_noqa(source_lines: list[str], line: int) -> bool:
    """Check for `# noqa: NFOR001` on the given line (1-indexed) or
    on the immediate `try` / `except` / function-def line."""
    if 0 < line <= len(source_lines):
        target = source_lines[line - 1]
        if "# noqa" in target.lower():
            comment = target.lower().split("# noqa", 1)[1]
            if ":" not in comment:
                return True  # bare # noqa
            if CODE.lower() in comment:
                return True
    return False


def _check_function(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    path: str,
    source_lines: list[str],
) -> Iterator[Violation]:
    if not _func_returns_nonnone(func):
        return
    for try_node in _try_blocks_in_func(func):
        # A `finally` that raises / exits cancels the fail-open class for the whole try.
        if (
            _has_raise(try_node.finalbody)
            or _has_exit_call(try_node.finalbody)
            or _has_nontrivial_return(try_node.finalbody)
        ):
            continue
        for handler in try_node.handlers:
            if not _is_broad_except(handler):
                continue
            if not _handler_is_log_only(handler):
                continue
            if _line_has_noqa(source_lines, handler.lineno):
                continue
            yield Violation(
                path=path,
                line=handler.lineno,
                func_name=func.name,
                message=(
                    f"broad except in `{func.name}` swallows exception; function "
                    f"returns a non-None value — re-raise, return an explicit "
                    f"error, or persist error state before falling through"
                ),
            )


def check_source(source: str, path: str = "<string>") -> list[Violation]:
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        # Treat as zero violations — let the syntax checker (flake8 / py_compile)
        # surface the parse error.
        print(f"{path}: NFOR000 syntax error: {e}", file=sys.stderr)
        return []
    source_lines = source.splitlines()
    out: list[Violation] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.extend(_check_function(node, path, source_lines))
    return out


def check_path(path: Path) -> list[Violation]:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        print(f"{path}: cannot read: {e}", file=sys.stderr)
        return []
    return check_source(source, str(path))


def iter_python_files(root: Path) -> Iterator[Path]:
    if root.is_file():
        if root.suffix == ".py":
            yield root
        return
    for p in root.rglob("*.py"):
        # Skip caches and venvs
        parts = set(p.parts)
        if parts & {"__pycache__", ".venv", "venv", ".git", "build", "dist", "node_modules"}:
            continue
        yield p


def load_baseline(path: Path) -> dict[str, str]:
    """Load baseline file. Format per line:

        file/path.py:LINE  reason text (must be non-empty)

    Blank lines and lines starting with `#` are ignored. Returns
    a map { "file/path.py:LINE" → reason }.
    """
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "  " not in line:
            print(
                f"{path}: baseline entry without reason rejected: {raw!r}",
                file=sys.stderr,
            )
            sys.exit(2)
        loc, reason = line.split("  ", 1)
        loc = loc.strip()
        reason = reason.strip()
        if not reason:
            print(
                f"{path}: baseline entry without reason rejected: {raw!r}",
                file=sys.stderr,
            )
            sys.exit(2)
        out[loc] = reason
    return out


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="no-fail-open-return",
        description=__doc__,
    )
    parser.add_argument("paths", nargs="+", help="files or directories to scan")
    parser.add_argument(
        "--baseline",
        type=Path,
        help="path to baseline file of approved fail-open sites",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit findings as JSON array on stdout",
    )
    args = parser.parse_args(argv)

    baseline = load_baseline(args.baseline) if args.baseline else {}

    all_violations: list[Violation] = []
    for raw in args.paths:
        p = Path(raw)
        if not p.exists():
            print(f"{p}: no such file or directory", file=sys.stderr)
            return 2
        for fp in iter_python_files(p):
            all_violations.extend(check_path(fp))

    new_violations: list[Violation] = []
    stale_baseline = set(baseline.keys())
    for v in all_violations:
        key = v.location_key()
        if key in baseline:
            stale_baseline.discard(key)
            continue
        new_violations.append(v)

    if args.json:
        print(json.dumps([v.to_json() for v in new_violations], indent=2))
    else:
        for v in new_violations:
            print(v.format_text())
        if stale_baseline:
            print(
                f"\nWARNING: baseline contains entries that no longer match any "
                f"violation — remove them: {sorted(stale_baseline)}",
                file=sys.stderr,
            )

    if new_violations:
        return 1
    if stale_baseline:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
