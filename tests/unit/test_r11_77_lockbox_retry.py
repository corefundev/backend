"""
tests/unit/test_r11_77_lockbox_retry.py

R11-#77 — bounded retry+backoff in the Lockbox bootstrap HTTP path.

The #83 prod-migrate failure was a single transient "Network is unreachable"
to Yandex Lockbox that fail-closed the whole bootstrap. The fix retries
transient faults (connection/timeout, HTTP 5xx) with exponential backoff but
still fails fast on deterministic 4xx and still raises once the budget is
spent (fail-closed preserved). These tests pin that contract.
"""
from __future__ import annotations

import io
import urllib.error

import pytest

import src.auth.lockbox_agent as lba
from src.auth.lockbox_agent import LockboxError


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Record backoff calls instead of actually sleeping."""
    calls: list[float] = []
    monkeypatch.setattr(lba.time, "sleep", lambda s: calls.append(s))
    return calls


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://lockbox/x", code=code, msg="boom",
        hdrs=None, fp=io.BytesIO(b'{"message":"boom"}'),
    )


def _url_error(reason: str = "Network is unreachable") -> urllib.error.URLError:
    return urllib.error.URLError(OSError(101, reason))


def _seq_urlopen(monkeypatch, outcomes):
    """Patch urlopen to walk `outcomes`: an Exception is raised, anything
    else is returned as a context-manager whose .read() yields its JSON."""
    seq = iter(outcomes)
    calls = {"n": 0}

    class _Resp:
        def __init__(self, payload): self._p = payload
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return self._p

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        nxt = next(seq)
        if isinstance(nxt, Exception):
            raise nxt
        return _Resp(nxt)

    monkeypatch.setattr(lba.urllib.request, "urlopen", fake_urlopen)
    return calls


def test_transient_urlerror_then_success(monkeypatch, _no_real_sleep):
    """Two network blips, then success → one returned dict, two backoffs."""
    calls = _seq_urlopen(monkeypatch, [
        _url_error(), _url_error(), b'{"ok": true}',
    ])
    req = lba.urllib.request.Request("https://lockbox/x")
    out = lba._urlopen_json(req, timeout=5, what="GET test")
    assert out == {"ok": True}
    assert calls["n"] == 3
    assert len(_no_real_sleep) == 2          # backoff before each retry, none after success


def test_5xx_is_retried(monkeypatch, _no_real_sleep):
    calls = _seq_urlopen(monkeypatch, [_http_error(503), b'{"ok": 1}'])
    req = lba.urllib.request.Request("https://lockbox/x")
    assert lba._urlopen_json(req, timeout=5, what="GET test") == {"ok": 1}
    assert calls["n"] == 2


def test_4xx_fails_fast_no_retry(monkeypatch, _no_real_sleep):
    """A 403 is deterministic (bad SA key / missing role) — must NOT retry."""
    calls = _seq_urlopen(monkeypatch, [_http_error(403)])
    req = lba.urllib.request.Request("https://lockbox/x")
    with pytest.raises(LockboxError, match="403"):
        lba._urlopen_json(req, timeout=5, what="GET test")
    assert calls["n"] == 1                    # single attempt, no retry
    assert _no_real_sleep == []               # no backoff for fail-fast


def test_budget_exhausted_raises_failclosed(monkeypatch, _no_real_sleep):
    """Persistent transient fault → LockboxError after exactly N attempts."""
    monkeypatch.setattr(lba, "_RETRY_ATTEMPTS", 4)
    calls = _seq_urlopen(monkeypatch, [_url_error()] * 4)
    req = lba.urllib.request.Request("https://lockbox/x")
    with pytest.raises(LockboxError, match="unreachable"):
        lba._urlopen_json(req, timeout=5, what="GET test")
    assert calls["n"] == 4                    # exactly the budget, no more
    assert len(_no_real_sleep) == 3           # backoff between, none after the last


def test_backoff_is_exponential(monkeypatch, _no_real_sleep):
    monkeypatch.setattr(lba, "_RETRY_ATTEMPTS", 5)
    monkeypatch.setattr(lba, "_RETRY_BACKOFF_BASE", 0.5)
    _seq_urlopen(monkeypatch, [_url_error()] * 4 + [b'{"ok": 1}'])
    req = lba.urllib.request.Request("https://lockbox/x")
    lba._urlopen_json(req, timeout=5, what="GET test")
    # 0.5, 1.0, 2.0, 4.0 — doubling, under the 8.0 cap
    assert _no_real_sleep == [0.5, 1.0, 2.0, 4.0]


def test_backoff_capped(monkeypatch, _no_real_sleep):
    monkeypatch.setattr(lba, "_RETRY_ATTEMPTS", 8)
    monkeypatch.setattr(lba, "_RETRY_BACKOFF_BASE", 0.5)
    _seq_urlopen(monkeypatch, [_url_error()] * 7 + [b'{"ok": 1}'])
    req = lba.urllib.request.Request("https://lockbox/x")
    lba._urlopen_json(req, timeout=5, what="GET test")
    # 0.5,1,2,4,8,8,8 — never exceeds the 8.0 cap
    assert _no_real_sleep == [0.5, 1.0, 2.0, 4.0, 8.0, 8.0, 8.0]
    assert max(_no_real_sleep) == lba._RETRY_BACKOFF_CAP
