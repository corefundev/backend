"""Unit tests for NFOR linter.

Each test exercises one rule predicate. Together they pin the behaviour
that R9-B item #14 will rely on when removing the baseline.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Tool is not a package on sys.path by default — add the repo root.
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from tools.lint.no_fail_open_return import check_source  # noqa: E402


def _violations(code: str) -> list:
    return check_source(code, "test.py")


# ── positive cases — must flag ────────────────────────────────────────────────

def test_log_warning_only_returning_nonnone_is_flagged():
    code = """
def run() -> int:
    try:
        x = compute()
    except Exception as e:
        logger.warning("oops: %s", e)
    return 0
"""
    vs = _violations(code)
    assert len(vs) == 1
    assert vs[0].func_name == "run"


def test_bare_except_is_flagged():
    code = """
def run():
    try:
        x = compute()
    except:
        pass
    return 1
"""
    assert len(_violations(code)) == 1


def test_except_baseexception_is_flagged():
    code = """
def run():
    try:
        x = compute()
    except BaseException:
        logger.error("bad")
    return "ok"
"""
    assert len(_violations(code)) == 1


def test_except_tuple_with_exception_is_flagged():
    code = """
def run():
    try:
        x = compute()
    except (ValueError, Exception):
        logger.error("bad")
    return "ok"
"""
    assert len(_violations(code)) == 1


def test_print_is_treated_as_log_only():
    code = """
def run():
    try:
        x = compute()
    except Exception as e:
        print("oops:", e)
    return 1
"""
    assert len(_violations(code)) == 1


def test_pass_in_handler_is_flagged():
    code = """
def run():
    try:
        x = compute()
    except Exception:
        pass
    return 42
"""
    assert len(_violations(code)) == 1


def test_assignment_fallback_only_is_flagged():
    code = """
def run():
    try:
        x = compute()
    except Exception:
        x = None
    return x
"""
    assert len(_violations(code)) == 1


def test_async_function_is_flagged():
    code = """
async def run():
    try:
        x = await compute()
    except Exception as e:
        logger.warning(e)
    return 1
"""
    assert len(_violations(code)) == 1


def test_nested_function_independently_checked():
    code = """
def outer():
    def inner():
        try:
            x = compute()
        except Exception:
            logger.warning("bad")
        return 1
    return inner
"""
    vs = _violations(code)
    assert len(vs) == 1
    assert vs[0].func_name == "inner"


def test_multiple_handlers_each_flagged():
    code = """
def run():
    try:
        x = compute()
    except ValueError:
        logger.warning("v")  # narrow — not flagged
    except Exception as e:
        logger.warning("e: %s", e)
    return 1
"""
    vs = _violations(code)
    assert len(vs) == 1


# ── negative cases — must NOT flag ────────────────────────────────────────────


def test_reraise_is_ok():
    code = """
def run():
    try:
        x = compute()
    except Exception as e:
        logger.warning(e)
        raise
    return 1
"""
    assert _violations(code) == []


def test_raise_from_is_ok():
    code = """
def run():
    try:
        x = compute()
    except Exception as e:
        raise RuntimeError("wrapped") from e
    return 1
"""
    assert _violations(code) == []


def test_narrow_except_is_ok():
    code = """
def run():
    try:
        x = compute()
    except ValueError as e:
        logger.warning(e)
    return 1
"""
    assert _violations(code) == []


def test_function_returns_none_only_is_ignored():
    code = """
def run() -> None:
    try:
        x = compute()
    except Exception as e:
        logger.warning(e)
"""
    assert _violations(code) == []


def test_finally_raise_neutralises_try():
    code = """
def run():
    try:
        x = compute()
    except Exception:
        logger.warning("oops")
    finally:
        if some_check():
            raise
    return 1
"""
    assert _violations(code) == []


def test_sys_exit_in_handler_is_ok():
    code = """
def run():
    try:
        x = compute()
    except Exception as e:
        logger.error(e)
        sys.exit(1)
    return 1
"""
    assert _violations(code) == []


def test_return_nontrivial_in_handler_is_ok():
    code = """
def run():
    try:
        x = compute()
    except Exception as e:
        logger.warning(e)
        return {"error": str(e)}
    return {"ok": 1}
"""
    assert _violations(code) == []


def test_noqa_line_suppresses():
    code = """
def run():
    try:
        x = compute()
    except Exception as e:  # noqa: NFOR001
        logger.warning(e)
    return 1
"""
    assert _violations(code) == []


def test_noqa_bare_suppresses():
    code = """
def run():
    try:
        x = compute()
    except Exception as e:  # noqa
        logger.warning(e)
    return 1
"""
    assert _violations(code) == []


def test_module_level_try_is_ignored():
    code = """
try:
    import fancy_thing
except Exception:
    fancy_thing = None
"""
    assert _violations(code) == []


def test_logger_in_class_method_is_flagged():
    code = """
class Worker:
    def run(self):
        try:
            x = self._compute()
        except Exception as e:
            self.logger.warning(e)
        return self._result
"""
    vs = _violations(code)
    assert len(vs) == 1
    assert vs[0].func_name == "run"


def test_handler_with_db_write_is_NOT_flagged_when_followed_by_return_error():
    code = """
def run():
    try:
        x = compute()
    except Exception as e:
        db.write_error(e)
        return ErrorResponse(str(e))
    return SuccessResponse(x)
"""
    assert _violations(code) == []


def test_syntax_error_returns_empty():
    code = "def broken("  # invalid
    assert _violations(code) == []
