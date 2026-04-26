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
import json
import logging
import time
from datetime import datetime, timezone

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
    Returns True on success.
    """
    import urllib.request

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

    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status < 300:
                    logger.info(f"Webhook delivered: event={event} client={client_id} url={url}")
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
