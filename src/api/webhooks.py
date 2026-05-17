"""
src/api/webhooks.py

Webhook notifications for async events:
  training_complete, forecast_ready, drift_alert, model_error

Features:
  - HMAC-SHA256 signature on payload
  - Exponential backoff retry (3 attempts)
  - Async delivery via background task
  - Stored endpoint per client in registry

Usage:
    webhook = WebhookSender(client_id, endpoint_url, secret)
    await webhook.send("training_complete", {"metrics": {...}})
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import socket
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

WEBHOOK_EVENTS = {
    "training_complete",
    "forecast_ready",
    "drift_alert",
    "model_error",
}


def _sign_payload(payload: bytes, secret: str) -> str:
    """HMAC-SHA256 signature for webhook verification."""
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


class WebhookUrlRejected(ValueError):
    """Raised when a webhook destination fails SSRF validation."""


def validate_webhook_url(url: str) -> str:
    """
    Audit R3-7 + R5-10 — refuse SSRF-prone destinations BEFORE issuing
    the request, AND return the validated public IP so the caller can
    pin the actual TCP connection to it (closing the DNS-rebinding
    TOCTOU window).

    Rejects:
      • non-http(s) schemes (file://, gopher://, etc.)
      • localhost / loopback / link-local / private RFC1918
      • cloud-metadata IP 169.254.169.254 (covered by link-local)
      • hostnames that resolve to any of the above

    Returns the FIRST IP that passed all checks — caller uses it as
    the literal connection target (see `send_webhook_sync` → DNS-
    pinned opener). Returning the IP also lets internal-only callers
    log which destination the SSRF gate cleared.

    R5-10 — the prior implementation returned None, so callers
    re-resolved DNS at `urlopen()` time; an attacker-controlled
    domain could return a public IP for validation, then flip to
    127.0.0.1 / 169.254.169.254 between the gate and the actual
    request. With the returned IP pinned at connect time, the second
    resolution cannot happen.

    Caller-supplied URLs (client_id's configured endpoint) must pass
    this gate; internal-only callers (admin alert_webhook_url from
    env) may pre-validate at config-load time.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise WebhookUrlRejected(f"scheme {parsed.scheme!r} not allowed")
    host = parsed.hostname or ""
    if not host:
        raise WebhookUrlRejected("missing host")

    candidates: list[str] = []
    try:
        ipaddress.ip_address(host)
        candidates.append(host)
    except ValueError:
        try:
            # getaddrinfo's sockaddr is (host, port) for AF_INET and
            # (host, port, flowinfo, scopeid) for AF_INET6; in both
            # variants index [0] is the address string. str() coerces
            # mypy's union narrowing without changing runtime behaviour.
            for info in socket.getaddrinfo(host, None):
                candidates.append(str(info[4][0]))
        except OSError as e:
            raise WebhookUrlRejected(f"cannot resolve {host}: {e}") from e

    # Validate every resolved IP; reject if ANY is private. Return the
    # FIRST verified-public IP for pinning.
    safe_ip: str | None = None
    for ip_str in candidates:
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if (ip.is_loopback or ip.is_private or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise WebhookUrlRejected(
                f"destination {host} → {ip_str} blocked (loopback/private/metadata)"
            )
        if safe_ip is None:
            safe_ip = ip_str

    if safe_ip is None:
        raise WebhookUrlRejected(f"no valid IP resolved for {host}")
    return safe_ip


def _build_pinned_opener(pinned_ip: str):
    """
    R5-10 — DNS-pinned HTTP(S) opener.

    Returns a `urllib.request.OpenerDirector` that connects to
    `pinned_ip` instead of re-resolving the request URL's hostname.
    For HTTPS the original hostname is still presented for SNI +
    certificate validation, so a public-cert MITM via IP-only target
    is not possible.

    The host header from the original URL is preserved by urllib —
    we only override the TCP target. This closes the DNS-rebinding
    TOCTOU window between `validate_webhook_url` and `urlopen`.
    """
    import http.client
    import ssl
    import socket as _socket
    import urllib.request

    # mypy --follow-imports=silent (our CI config) can't see the
    # stdlib HTTP(S)Connection attributes we inherit, so each cross-
    # boundary access carries `# type: ignore[attr-defined]`. The
    # attributes exist at runtime — they're standard parts of
    # http.client.HTTP{S,}Connection.
    class PinnedHTTPSConnection(http.client.HTTPSConnection):
        def connect(self) -> None:
            self.sock = _socket.create_connection(
                (pinned_ip, self.port), self.timeout,
                self.source_address,                          # type: ignore[attr-defined]
            )
            if self._tunnel_host:                              # type: ignore[attr-defined]
                self._tunnel()                                 # type: ignore[attr-defined]
            # `self.host` is the original hostname — used for SNI +
            # cert validation. `pinned_ip` is what we actually connect
            # to. This is exactly the shape that defeats DNS-rebinding.
            self.sock = self._context.wrap_socket(             # type: ignore[attr-defined]
                self.sock, server_hostname=self.host,
            )

    class PinnedHTTPConnection(http.client.HTTPConnection):
        def connect(self) -> None:
            self.sock = _socket.create_connection(
                (pinned_ip, self.port), self.timeout,
                self.source_address,                          # type: ignore[attr-defined]
            )

    class PinnedHTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(
                PinnedHTTPSConnection, req,
                context=ssl.create_default_context(),
            )

    class PinnedHTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(PinnedHTTPConnection, req)

    return urllib.request.build_opener(
        PinnedHTTPSHandler(), PinnedHTTPHandler(),
    )


def send_webhook_sync(
    url: str,
    event: str,
    data: dict,
    client_id: str,
    secret: str = "webhook-secret",
    max_retries: int = 3,
) -> bool:
    """
    Synchronous webhook delivery with retry.
    Returns True on success, False on any failure (network error, HTTP
    error, or SSRF policy rejection — audits R3-7 + R5-10).

    R5-10 — opens the connection through a DNS-pinned opener so the
    IP `validate_webhook_url` cleared is the same IP we actually
    connect to. Plain `urllib.request.urlopen(url)` re-resolves DNS
    at send time → an attacker-controlled domain can flip from a
    public IP (passes validation) to 127.0.0.1 / 169.254.169.254
    (real request) between the gate and the connect.
    """
    import urllib.request

    try:
        pinned_ip = validate_webhook_url(url)
    except WebhookUrlRejected as e:
        logger.error(
            "Webhook URL rejected (SSRF policy): client=%s event=%s reason=%s",
            client_id, event, e,
        )
        return False

    payload = json.dumps({
        "event":      event,
        "client_id":  client_id,
        "timestamp":  datetime.now(timezone.utc).isoformat(),
        "data":       data,
    }).encode()

    signature = _sign_payload(payload, secret)
    headers = {
        "Content-Type":        "application/json",
        "X-SKU-Signature":     f"sha256={signature}",
        "X-SKU-Event":         event,
        "User-Agent":          "SKU-Forecasting/2.0",
    }

    # R5-10 — opener pinned to the validated IP. SNI / cert validation
    # still use the original hostname (see _build_pinned_opener).
    opener = _build_pinned_opener(pinned_ip)

    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            with opener.open(req, timeout=10) as resp:
                if resp.status < 300:
                    logger.info(
                        "Webhook delivered: event=%s client=%s url=%s pinned_ip=%s",
                        event, client_id, url, pinned_ip,
                    )
                    return True
        except Exception as e:
            wait = 2 ** attempt
            logger.warning(f"Webhook attempt {attempt+1}/{max_retries} failed: {e}. Wait {wait}s")
            if attempt < max_retries - 1:
                time.sleep(wait)

    logger.error(f"Webhook failed after {max_retries} attempts: event={event} url={url}")
    return False


class WebhookRegistry:
    """Simple in-memory registry of client webhook endpoints."""

    def __init__(self):
        self._endpoints: dict[str, dict] = {}

    def register(self, client_id: str, url: str, secret: str = "") -> None:
        self._endpoints[client_id] = {"url": url, "secret": secret}
        logger.info(f"Webhook registered: client={client_id} url={url}")

    def get(self, client_id: str) -> dict | None:
        return self._endpoints.get(client_id)

    def notify(self, client_id: str, event: str, data: dict) -> bool:
        ep = self.get(client_id)
        if not ep:
            return False
        return send_webhook_sync(
            url=ep["url"],
            event=event,
            data=data,
            client_id=client_id,
            secret=ep.get("secret", ""),
        )


# Global singleton
webhook_registry = WebhookRegistry()
