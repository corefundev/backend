#!/usr/bin/env python3
"""COMPOSE_PWD_INTERP — lint forbidding ${POSTGRES_PASSWORD} interpolation
inside docker-compose `command:` and `entrypoint:` blocks.

Why this exists:
  Docker Compose interpolates ${VAR} at config-load time from .env, which
  per the R3-13 design holds the seed placeholder `sku` — NOT the real
  postgres password (the real value lives in Yandex Lockbox and is
  injected per-container at runtime via lockbox_bootstrap.sh / the
  Python `vault_agent.bootstrap_secrets()` chain).

  When the interpolation lands inside a service's `command:` or
  `entrypoint:`, the seed is baked into the process's CLI args BEFORE
  any runtime Lockbox bootstrap can overlay the live value. The
  service then authenticates as `sku/sku` and SCRAM-fails against
  the rotated postgres user.

  This is exactly the R8-12 v1 SASL trap (2026-05-25): mlflow's
  compose command included `--backend-store-uri postgresql://sku:
  ${POSTGRES_PASSWORD}@…` which compose-interpolated to
  `postgresql://sku:sku@…`. After force-recreate, the seed-as-pwd
  hit the rotated postgres and /readyz dropped to 503 for the
  user-visible recovery window.

  Patch was PR #24 (mlflow_entrypoint.sh builds DSN from Lockbox-
  injected POSTGRES_PASSWORD); PR #28 did the same for
  postgres-exporter. This linter prevents the trap from re-appearing
  on any future service.

The correct pattern when a service needs a postgres DSN:
  1. Pull POSTGRES_PASSWORD (and DB_HOST/PORT/NAME/USER) into the
     container env via `LOCKBOX_ALLOWED_KEYS` in
     docker-compose.lockbox.yml (compose-side `environment:` blocks
     are fine — they're container env, not CLI args; Python apps
     re-overlay via vault_agent at runtime).
  2. In the service's entrypoint script, after lockbox_bootstrap.sh,
     compose the DSN from the live env vars and append to the CLI
     (or set an env-var the binary reads).
  See scripts/mlflow_entrypoint.sh + scripts/postgres_exporter_entrypoint.sh
  for the reference implementation.

Exit codes:
  0  no violations (or only baselined ones)
  1  new violations found
  2  CLI usage error

Baseline format mirrors tools/lint/.no_fail_open_return.baseline:
    <file>:<lineno>  <REASON-WITH-TICKET-OR-AUDIT-REF>
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Iterable, List, Set, Tuple


# Patterns that mean "compose-time interpolation of the postgres password".
# Both `${POSTGRES_PASSWORD}` (the modern compose form, including the
# strict-gate `:?…` variant) and `$POSTGRES_PASSWORD` (legacy bash-style,
# also supported by compose) are equally trap-prone — both reach the
# value through `.env` at config-load.
_FORBIDDEN_RE = re.compile(
    r"\$\{POSTGRES_PASSWORD(?:_REPLICA)?(?:[:?\-+}].*?)?\}"
    r"|\$POSTGRES_PASSWORD(?:_REPLICA)?\b"
)

# YAML keys where the interpolation is forbidden. `command:` is the
# obvious one; `entrypoint:` is the override path that compose lets you
# set as well.
FORBIDDEN_BLOCKS = frozenset({"command", "entrypoint"})

# Match a YAML "key:" pattern at any indent. The `[a-zA-Z_]…` constraint
# keeps it from matching numeric list indices or URL fragments.
_KEY_RE = re.compile(r"^(\s*)([a-zA-Z_][a-zA-Z_0-9-]*)\s*:\s*(.*)$")


def find_violations(text: str, file_path: str) -> List[Tuple[str, int, str, str]]:
    """Walk `text` line-by-line, tracking the YAML indent stack of keys.

    A line is a violation when:
      - It contains any forbidden pattern OUTSIDE a `#` comment, AND
      - The most recent key on the indent stack is one of FORBIDDEN_BLOCKS.

    Returns tuples (file_path, lineno, blocking_key, source_line).
    """
    violations: List[Tuple[str, int, str, str]] = []
    # Stack of (indent, key). The top is the most recently-entered block.
    stack: List[Tuple[int, str]] = []

    for lineno, raw_line in enumerate(text.split("\n"), 1):
        stripped = raw_line.strip()
        if not stripped:
            continue

        # If the line is purely a comment, skip both key-extraction and
        # forbidden-pattern check (a `# ${POSTGRES_PASSWORD}` in a comment
        # is harmless).
        if stripped.startswith("#"):
            continue

        m = _KEY_RE.match(raw_line)
        if m:
            indent = len(m.group(1))
            key = m.group(2)
            # Pop the stack while the top has indent >= ours — we've
            # left those nested keys.
            while stack and stack[-1][0] >= indent:
                stack.pop()
            stack.append((indent, key))

        # Check this line for the pattern, but only on the code portion
        # (before any inline `#` comment).
        code_only = re.sub(r"#.*$", "", raw_line)
        if not _FORBIDDEN_RE.search(code_only):
            continue

        # Look up the stack for the enclosing forbidden block.
        for _ind, k in reversed(stack):
            if k in FORBIDDEN_BLOCKS:
                violations.append((file_path, lineno, k, raw_line.rstrip()))
                break

    return violations


def parse_baseline(baseline_path: str) -> Set[Tuple[str, int]]:
    """Parse `<file>:<lineno><whitespace><reason>` lines.

    Empty lines and `#`-prefixed lines are ignored. Every entry MUST
    carry a reason — entries without one are rejected (the linter
    refuses to start) so the allowlist stays auditable.

    Returns a set of (file, lineno) tuples.
    """
    if not os.path.exists(baseline_path):
        return set()
    allowed: Set[Tuple[str, int]] = set()
    with open(baseline_path) as f:
        for ln, raw in enumerate(f, 1):
            line = raw.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            parts = re.split(r"\s{2,}|\t", line, maxsplit=1)
            if len(parts) != 2 or not parts[1].strip():
                raise SystemExit(
                    f"{baseline_path}:{ln}: baseline entry missing reason: "
                    f"{raw!r}"
                )
            loc, _reason = parts
            if ":" not in loc:
                raise SystemExit(
                    f"{baseline_path}:{ln}: baseline entry not in "
                    f"'file:line' format: {loc!r}"
                )
            file_part, line_part = loc.rsplit(":", 1)
            try:
                allowed.add((file_part, int(line_part)))
            except ValueError:
                raise SystemExit(
                    f"{baseline_path}:{ln}: bad lineno in {loc!r}"
                )
    return allowed


def collect_files(paths: Iterable[str]) -> List[Path]:
    """Expand a list of args into a sorted, deduplicated list of compose files.

    Each `path` may be a single YAML file or a directory; directories
    are expanded to all `docker-compose*.yml` direct children.
    """
    files: List[Path] = []
    for arg in paths:
        p = Path(arg)
        if p.is_dir():
            files.extend(sorted(p.glob("docker-compose*.yml")))
        elif p.is_file():
            files.append(p)
        else:
            print(f"compose_pwd_interpolation: path not found: {arg}",
                  file=sys.stderr)
            raise SystemExit(2)
    # Dedupe while preserving order
    seen: Set[Path] = set()
    out: List[Path] = []
    for f in files:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="compose_pwd_interpolation",
        description=(
            "Lint: forbid ${POSTGRES_PASSWORD} interpolation inside "
            "docker-compose `command:` and `entrypoint:` blocks. "
            "See module docstring for the design rationale."
        ),
    )
    parser.add_argument(
        "paths", nargs="+",
        help="compose YAML files or directories containing them",
    )
    parser.add_argument(
        "--baseline", default=None,
        help="path to baseline file with `<file>:<line>  <reason>` entries",
    )
    args = parser.parse_args(argv)

    files = collect_files(args.paths)
    if not files:
        print("compose_pwd_interpolation: no compose files to lint",
              file=sys.stderr)
        return 2

    allowed = parse_baseline(args.baseline) if args.baseline else set()

    all_violations: List[Tuple[str, int, str, str]] = []
    for f in files:
        text = f.read_text(encoding="utf-8")
        all_violations.extend(find_violations(text, str(f)))

    unbaselined = [
        v for v in all_violations if (v[0], v[1]) not in allowed
    ]
    found_locs = {(v[0], v[1]) for v in all_violations}
    stale = sorted(allowed - found_locs)

    for path, lineno, key, source in unbaselined:
        sys.stderr.write(
            f"{path}:{lineno}: COMPOSE_PWD_INTERP forbidden interpolation "
            f"of ${{POSTGRES_PASSWORD}} inside '{key}:' block — the value "
            f"will be `.env`'s seed `sku`, not the live Lockbox value. "
            f"Construct the DSN at runtime in the service entrypoint "
            f"instead (see scripts/mlflow_entrypoint.sh).\n"
            f"    {source}\n"
        )

    if stale:
        sys.stderr.write(
            "WARNING: baseline contains entries that no longer match any "
            "violation — remove them: "
            f"{[f'{p}:{ln}' for p, ln in stale]}\n"
        )

    return 1 if unbaselined else 0


if __name__ == "__main__":
    sys.exit(main())
