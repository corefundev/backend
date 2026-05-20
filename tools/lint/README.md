# tools/lint — custom AST linters

In-house static checks that flake8 / mypy do not cover. Each tool is
self-contained Python — no plugin registration, no extra dependencies.
CI invokes them directly.

## `no_fail_open_return.py` — NFOR001

Detects the **exception-swallow → fail-open** pattern: a broad
`except Exception` (or bare `except:`) that logs the error and lets
control fall through to a non-None return, leaving callers believing
the operation succeeded.

This pattern was the root cause of **R8-12** (MLflow run_id stayed
NULL because `mlflow.log_artifact` exceptions were caught, logged as
WARNING, and the surrounding function returned an "ok" dict). The
deep audit found nine high-risk sites with the same shape; this linter
catches the whole pattern across the codebase.

### Running

```bash
# Strict (used in CI)
python tools/lint/no_fail_open_return.py src/ \
  --baseline tools/lint/.no_fail_open_return.baseline

# Show only NEW violations (no baseline)
python tools/lint/no_fail_open_return.py src/
```

Exit codes: `0` clean, `1` violations or stale-baseline entries, `2` usage.

### Suppressing one site

Two routes — pick the auditable one:

1. **Inline suppression** when the fail-open is genuinely intended
   (e.g. cleanup hook where re-raising would mask the original
   exception). Add on the `except` line:

   ```python
   except Exception:  # noqa: NFOR001
       logger.warning("cleanup failed")
   ```

2. **Baseline entry** for legacy sites scheduled for fix. Add to
   `.no_fail_open_return.baseline` with a non-empty reason linking to
   the audit finding / triage ticket:

   ```
   src/foo.py:42  R10-T-XYZ — bar.baz fallback; triage in R10
   ```

   The linter rejects baseline entries without a reason — the file
   stays auditable.

### Removing a baseline entry

That is the **target state**. When you fix the swallow, delete the
matching line from the baseline file. The linter also fails CI if the
baseline references a `file:line` that no longer matches a real
violation (so resolved sites cannot be quietly left in the allowlist).

### What is **not** a violation

- `except SpecificError:` (narrow catch)
- `except Exception: raise` (re-raise; possibly with `from e`)
- `except Exception: return ErrorResponse(...)` (explicit error
  return — caller observes the failure)
- `except Exception: sys.exit(1)` (process exit; failure cannot be
  masked)
- `except Exception: db.persist_error(e); raise` (error captured
  durably, then re-raised)
- A `try` with `finally: raise` (finalizer escalates the original
  exception out of the function)
- Functions that only return `None` (no fail-open risk — caller has
  nothing to confuse with success)
- Module-level `try / except` outside any function

### Tests

`tests/lint/test_no_fail_open_return.py` pins every detection rule
to a positive + negative case. Run:

```bash
python -m pytest tests/lint/ -v
```
