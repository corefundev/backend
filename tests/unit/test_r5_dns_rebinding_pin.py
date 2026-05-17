"""
Regression tests for R5-10 — DNS-rebinding TOCTOU bypassing the R3-7
SSRF guard in src/api/webhooks.py (2026-05-17).

Before fix: `validate_webhook_url(url)` resolved DNS once and
verified all IPs were public; then `urllib.request.urlopen(req)` did
its OWN DNS resolution before connecting. An attacker-controlled
domain could return a public IP for the validation lookup (passes
the gate), then 169.254.169.254 / 127.0.0.1 for the actual request
(classic DNS-rebinding TOCTOU). The R3-7 SSRF guard was effective
in spirit, ineffective in execution.

Fix: `validate_webhook_url` now RETURNS the first verified-public
IP, and `send_webhook_sync` opens the connection through a custom
DNS-pinned `OpenerDirector` (`_build_pinned_opener`) that connects
to the pinned IP rather than re-resolving the URL hostname. For
HTTPS the original hostname is still presented for SNI and cert
validation so a public-cert MITM via IP-only target stays impossible.

Source-level pin + behavioural unit tests.
"""
from __future__ import annotations

from pathlib import Path

import pytest


_BACKEND = Path(__file__).resolve().parents[2]


# ── Source-level invariants ──────────────────────────────────────────────

def test_validate_webhook_url_returns_pinned_ip():
    """The function signature must return `str` (the pinned IP), not
    None — callers need it to pin the actual connection."""
    text = (_BACKEND / "src" / "api" / "webhooks.py").read_text()
    assert "def validate_webhook_url(url: str) -> str:" in text, (
        "validate_webhook_url must return the pinned IP as str (R5-10)"
    )


def test_pinned_opener_helper_exists_and_overrides_connect():
    """`_build_pinned_opener(pinned_ip)` must declare a custom
    HTTP(S)Connection subclass that overrides `connect` to use
    `pinned_ip` instead of the URL hostname."""
    text = (_BACKEND / "src" / "api" / "webhooks.py").read_text()
    assert "def _build_pinned_opener(" in text, (
        "DNS-pinning helper _build_pinned_opener must exist"
    )
    assert "class PinnedHTTPSConnection" in text, (
        "must define a custom HTTPSConnection that pins the target IP"
    )
    assert "class PinnedHTTPConnection" in text, (
        "must define a custom HTTPConnection that pins the target IP"
    )
    assert "server_hostname=self.host" in text, (
        "PinnedHTTPSConnection must keep SNI on the original hostname "
        "so cert validation still works correctly (R5-10)"
    )


def test_send_webhook_sync_uses_pinned_opener():
    """The connection in `send_webhook_sync` must be opened via
    `opener.open(req, ...)` where opener came from
    `_build_pinned_opener(pinned_ip)` — NOT plain
    `urllib.request.urlopen(req)` (the legacy re-resolving path)."""
    text = (_BACKEND / "src" / "api" / "webhooks.py").read_text()
    func_idx = text.find("def send_webhook_sync(")
    assert func_idx > 0
    end_idx = text.find("\nclass ", func_idx)
    block = text[func_idx:end_idx] if end_idx > 0 else text[func_idx:]

    assert "_build_pinned_opener(pinned_ip)" in block, (
        "send_webhook_sync must build a DNS-pinned opener from "
        "validate_webhook_url's return value (R5-10)"
    )
    assert "opener.open(req" in block, (
        "request must be issued via opener.open, not urllib.request.urlopen"
    )
    # The legacy bypass pattern must NOT recur.
    assert "urllib.request.urlopen(req" not in block, (
        "send_webhook_sync must not call urllib.request.urlopen directly "
        "(the path that re-resolves DNS and bypasses the SSRF guard — R5-10)"
    )


# ── Behavioural tests ────────────────────────────────────────────────────

def test_validate_webhook_url_returns_public_ip_for_public_host():
    """A public hostname (8.8.8.8 — literal IP) must return that IP."""
    from src.api.webhooks import validate_webhook_url
    ip = validate_webhook_url("https://8.8.8.8/hook")
    assert ip == "8.8.8.8", f"expected literal IP echoed back, got {ip!r}"


def test_validate_webhook_url_rejects_localhost():
    """`http://localhost` resolves to 127.0.0.1 — must be rejected."""
    from src.api.webhooks import validate_webhook_url, WebhookUrlRejected
    with pytest.raises(WebhookUrlRejected, match="blocked"):
        validate_webhook_url("http://localhost/hook")


def test_validate_webhook_url_rejects_metadata_ip():
    """169.254.169.254 (cloud metadata) is link-local → blocked."""
    from src.api.webhooks import validate_webhook_url, WebhookUrlRejected
    with pytest.raises(WebhookUrlRejected, match="blocked"):
        validate_webhook_url("http://169.254.169.254/")


def test_validate_webhook_url_rejects_private_rfc1918():
    """10.x.x.x is private → blocked."""
    from src.api.webhooks import validate_webhook_url, WebhookUrlRejected
    with pytest.raises(WebhookUrlRejected, match="blocked"):
        validate_webhook_url("https://10.0.0.1/hook")


def test_validate_webhook_url_rejects_non_http_scheme():
    """file:// / gopher:// / etc. all rejected before any DNS lookup."""
    from src.api.webhooks import validate_webhook_url, WebhookUrlRejected
    with pytest.raises(WebhookUrlRejected, match="scheme"):
        validate_webhook_url("file:///etc/passwd")


def test_pinned_opener_actually_uses_pinned_ip_not_hostname(monkeypatch):
    """Behavioural: the opener returned by _build_pinned_opener must
    open a TCP connection to the pinned IP, not the URL's hostname.
    Mock socket.create_connection and verify the address argument."""
    import socket as _socket
    from src.api.webhooks import _build_pinned_opener

    PINNED = "203.0.113.42"  # TEST-NET-3 — guaranteed unroutable
    captured: dict = {}

    real_create = _socket.create_connection

    def spy_create_connection(address, *args, **kwargs):
        captured["address"] = address
        # Don't actually connect — raise so the test doesn't hang.
        raise OSError("test stub — no actual socket open")

    monkeypatch.setattr(_socket, "create_connection", spy_create_connection)

    opener = _build_pinned_opener(PINNED)
    import urllib.request
    req = urllib.request.Request("http://example.com/hook", data=b"x")
    try:
        opener.open(req, timeout=1)
    except Exception:
        pass  # expected — create_connection raised

    assert "address" in captured, "create_connection should have been called"
    addr = captured["address"]
    assert addr[0] == PINNED, (
        f"pinned opener must connect to the pinned IP {PINNED!r}, "
        f"got {addr[0]!r} (DNS-rebinding window NOT closed — R5-10)"
    )
