"""
src/auth/email_sender.py

Email delivery abstraction for OTP messages.

Three implementations, picked by EMAIL_PROVIDER env:

    EMAIL_PROVIDER=console   (default in dev) — prints OTP to logs
    EMAIL_PROVIDER=resend                     — REST API at resend.com
    EMAIL_PROVIDER=smtp                       — generic SMTP

The intent here is to keep the email body simple and provider-neutral.
HTML templates are deliberately not used: spam filters score plain-text
much more leniently, the OTP is the only actionable content, and we
avoid the entire HTML-rendering threat surface (display: none tricks
that hide the real recipient, etc.).

Spam-filter friendliness
────────────────────────
DKIM/SPF/DMARC are configured at the DNS/sender level (see DEPLOY.md).
This module assumes the provider already passes those checks.

Required env per provider
─────────────────────────
    Resend:
        EMAIL_PROVIDER=resend
        RESEND_API_KEY=re_...
        EMAIL_FROM=noreply@yourdomain.example       (must be verified in Resend)
        EMAIL_FROM_NAME=SKU Forecasting             (optional)

    SMTP:
        EMAIL_PROVIDER=smtp
        SMTP_HOST=smtp.example.com
        SMTP_PORT=587
        SMTP_USER=...
        SMTP_PASSWORD=...
        SMTP_USE_TLS=1
        EMAIL_FROM=noreply@yourdomain.example

    Console (dev only):
        EMAIL_PROVIDER=console
        EMAIL_FROM=dev@local
"""
from __future__ import annotations

import json
import logging
import os
import smtplib
import urllib.error
import urllib.request
from email.message import EmailMessage
from typing import Protocol

logger = logging.getLogger(__name__)


# ── Public protocol ─────────────────────────────────────────────────────

class EmailSender(Protocol):
    def send(self, *, to: str, subject: str, body: str) -> None: ...


class EmailDeliveryError(RuntimeError):
    """Sender failed to dispatch the message. Caller decides whether to retry."""


# ── Console (dev) ───────────────────────────────────────────────────────

class ConsoleEmailSender:
    """
    Logs the email to stdout/loggers. Useful for tests and local dev
    where you want to see OTPs in `docker compose logs api` without
    leaving the laptop.
    """
    def send(self, *, to: str, subject: str, body: str) -> None:
        logger.warning(
            "\n"
            "═══════════════════ EMAIL (console) ═══════════════════\n"
            "  To:      %s\n"
            "  Subject: %s\n"
            "  ---\n"
            "  %s\n"
            "════════════════════════════════════════════════════════",
            to, subject, body.replace("\n", "\n  "),
        )


# ── Resend ──────────────────────────────────────────────────────────────

class ResendEmailSender:
    """
    Resend.com REST API. Modern free tier (3000 emails/month, 100/day),
    excellent deliverability out of the box for verified domains.

    Sends a POST to https://api.resend.com/emails and expects 200 OK.
    On failure raises EmailDeliveryError — the caller (signup endpoint)
    will return 503 to the user without leaking the underlying error.
    """
    API_URL = "https://api.resend.com/emails"

    def __init__(self, api_key: str | None = None,
                 from_addr: str | None = None,
                 from_name: str | None = None):
        self.api_key   = api_key   or os.environ.get("RESEND_API_KEY")
        self.from_addr = from_addr or os.environ.get("EMAIL_FROM")
        self.from_name = from_name or os.environ.get("EMAIL_FROM_NAME", "")
        if not self.api_key:
            raise RuntimeError("RESEND_API_KEY not set")
        if not self.from_addr:
            raise RuntimeError("EMAIL_FROM not set")

    def send(self, *, to: str, subject: str, body: str) -> None:
        payload = {
            "from":    f"{self.from_name} <{self.from_addr}>" if self.from_name else self.from_addr,
            "to":      [to],
            "subject": subject,
            "text":    body,
        }
        req = urllib.request.Request(
            self.API_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type":  "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status != 200:
                    raise EmailDeliveryError(
                        f"Resend returned {resp.status}: {resp.read()[:200]!r}"
                    )
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")[:200]
            raise EmailDeliveryError(f"Resend HTTP {e.code}: {err}") from e
        except urllib.error.URLError as e:
            raise EmailDeliveryError(f"Resend transport: {e.reason}") from e


# ── SMTP ────────────────────────────────────────────────────────────────

class SmtpEmailSender:
    """
    Plain-vanilla SMTP through `smtplib`. Use when you have a corporate
    relay or mailcow / postfix on a dedicated host. Not recommended for
    SaaS where deliverability matters — Resend / SES / Postmark are
    much better at landing in the inbox.
    """

    def __init__(self):
        self.host     = os.environ.get("SMTP_HOST")
        self.port     = int(os.environ.get("SMTP_PORT", "587"))
        self.user     = os.environ.get("SMTP_USER")
        self.password = os.environ.get("SMTP_PASSWORD")
        self.use_tls  = os.environ.get("SMTP_USE_TLS", "1") == "1"
        self.from_addr = os.environ.get("EMAIL_FROM")
        if not (self.host and self.from_addr):
            raise RuntimeError("SMTP_HOST and EMAIL_FROM must be set")

    def send(self, *, to: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"]    = self.from_addr
        msg["To"]      = to
        msg["Subject"] = subject
        msg.set_content(body)

        # Port 465 is SMTP-over-SSL (TLS handshake at connection time);
        # port 587 is STARTTLS (upgrade after plaintext greeting). Beget
        # in particular only listens on 25 and 465, not 587 — so port-
        # based detection is the most portable signal.
        ssl_at_connect = (self.port == 465)

        try:
            if ssl_at_connect:
                smtp_cls = smtplib.SMTP_SSL
            else:
                smtp_cls = smtplib.SMTP
            with smtp_cls(self.host, self.port, timeout=10) as smtp:
                if not ssl_at_connect and self.use_tls:
                    smtp.starttls()
                if self.user and self.password:
                    smtp.login(self.user, self.password)
                smtp.send_message(msg)
        except (smtplib.SMTPException, OSError) as e:
            raise EmailDeliveryError(f"SMTP send failed: {e}") from e


# ── Factory ─────────────────────────────────────────────────────────────

_singleton: EmailSender | None = None


def get_email_sender() -> EmailSender:
    """
    Pick the configured implementation. Cached, so we instantiate once
    per process and reuse. To switch providers, restart the API.
    """
    global _singleton
    if _singleton is not None:
        return _singleton
    provider = (os.environ.get("EMAIL_PROVIDER") or "console").lower()
    if provider == "resend":
        _singleton = ResendEmailSender()
    elif provider == "smtp":
        _singleton = SmtpEmailSender()
    else:
        _singleton = ConsoleEmailSender()
    logger.info("Email sender = %s", provider)
    return _singleton


def reset_for_tests() -> None:
    global _singleton
    _singleton = None


# ── OTP message templates ───────────────────────────────────────────────

def render_otp_email(*, code: str, purpose: str, ttl_minutes: int) -> tuple[str, str]:
    """
    Return (subject, body). Plain-text only on purpose — better for
    spam-filter scores and avoids the HTML-render security surface.
    """
    if purpose == "signup":
        subject = "Подтверждение регистрации в SKU Forecasting"
        body = (
            f"Здравствуйте,\n\n"
            f"Ваш код для завершения регистрации: {code}\n\n"
            f"Введите его в приложении в течение {ttl_minutes} минут.\n"
            f"Если вы не запрашивали регистрацию, просто проигнорируйте письмо.\n\n"
            f"— SKU Forecasting"
        )
    else:  # login
        subject = "Код для входа в SKU Forecasting"
        body = (
            f"Здравствуйте,\n\n"
            f"Ваш код для входа: {code}\n\n"
            f"Действителен {ttl_minutes} минут. Если вы не запрашивали "
            f"вход, измените пароль почтового ящика — кто-то знает ваш email.\n\n"
            f"— SKU Forecasting"
        )
    return subject, body
