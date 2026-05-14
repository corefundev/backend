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
        EMAIL_FROM_NAME=SKU Forecasting       (display name, optional)
        EMAIL_REPLY_TO=support@yourdomain.example   (optional)
        EMAIL_UNSUBSCRIBE=unsubscribe@yourdomain.example  (optional; defaults to EMAIL_FROM)

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
from email.utils import formataddr
from typing import Protocol

logger = logging.getLogger(__name__)


# ── Public protocol ─────────────────────────────────────────────────────

class EmailSender(Protocol):
    def send(self, *, to: str, subject: str, body: str,
             html: str | None = None) -> None: ...


class EmailDeliveryError(RuntimeError):
    """Sender failed to dispatch the message. Caller decides whether to retry."""


# ── Console (dev) ───────────────────────────────────────────────────────

class ConsoleEmailSender:
    """
    Logs the email to stdout/loggers. Useful for tests and local dev
    where you want to see OTPs in `docker compose logs api` without
    leaving the laptop.

    Defense in depth: refuses to instantiate when APP_ENV=production.
    `src/api/startup_safety.py` already hard-fails on
    `EMAIL_PROVIDER=console` at lifespan startup, but that check can
    be bypassed by direct instantiation (e.g., a test fixture mock
    that forgets to also set APP_ENV). This class-level guard catches
    that path too. Audit R2-2 (2026-05-15).
    """

    def __init__(self) -> None:
        if (os.environ.get("APP_ENV") or "").strip().lower() == "production":
            raise RuntimeError(
                "ConsoleEmailSender refuses to start in production: "
                "OTP codes would be printed to logs. "
                "Set EMAIL_PROVIDER=smtp or =resend in Lockbox."
            )

    def send(self, *, to: str, subject: str, body: str,
             html: str | None = None) -> None:
        logger.warning(
            "\n"
            "═══════════════════ EMAIL (console) ═══════════════════\n"
            "  To:      %s\n"
            "  Subject: %s\n"
            "  HTML?:   %s\n"
            "  ---\n"
            "  %s\n"
            "════════════════════════════════════════════════════════",
            to, subject, "yes" if html else "no", body.replace("\n", "\n  "),
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

    def send(self, *, to: str, subject: str, body: str,
             html: str | None = None) -> None:
        # Deliverability headers — see SmtpEmailSender.send() for the
        # full rationale. mail.ru in particular grades non-human mail
        # without List-Unsubscribe as spam regardless of SPF/DKIM/DMARC.
        unsub_addr = os.environ.get("EMAIL_UNSUBSCRIBE", self.from_addr)
        reply_to   = os.environ.get("EMAIL_REPLY_TO")
        headers: dict[str, str] = {
            "List-Unsubscribe":      f"<mailto:{unsub_addr}?subject=unsubscribe>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        }
        if reply_to:
            headers["Reply-To"] = reply_to

        payload: dict[str, object] = {
            "from":    formataddr((self.from_name, self.from_addr)) if self.from_name else self.from_addr,
            "to":      [to],
            "subject": subject,
            "text":    body,
            "headers": headers,
        }
        if html:
            payload["html"] = html
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
        self.host       = os.environ.get("SMTP_HOST")
        self.port       = int(os.environ.get("SMTP_PORT", "587"))
        self.user       = os.environ.get("SMTP_USER")
        self.password   = os.environ.get("SMTP_PASSWORD")
        self.use_tls    = os.environ.get("SMTP_USE_TLS", "1") == "1"
        self.from_addr  = os.environ.get("EMAIL_FROM")
        self.from_name  = os.environ.get("EMAIL_FROM_NAME", "")
        self.reply_to   = os.environ.get("EMAIL_REPLY_TO")
        # mail.ru/yandex 2024+ require List-Unsubscribe on automated mail
        # even when it's transactional (OTP, password reset). The mailbox
        # doesn't have to exist for the heuristic to pass — but having a
        # real one (`unsubscribe@<domain>`) is cleaner.
        self.unsub_addr = os.environ.get("EMAIL_UNSUBSCRIBE", self.from_addr)
        if not (self.host and self.from_addr):
            raise RuntimeError("SMTP_HOST and EMAIL_FROM must be set")

    def send(self, *, to: str, subject: str, body: str,
             html: str | None = None) -> None:
        msg = EmailMessage()
        msg["From"]    = formataddr((self.from_name, self.from_addr)) if self.from_name else self.from_addr
        msg["To"]      = to
        msg["Subject"] = subject
        if self.reply_to:
            msg["Reply-To"] = self.reply_to
        # Deliverability headers — mail.ru's `X-Mailru-Noreply: yes`
        # heuristic auto-spams `noreply@` senders unless these are set.
        msg["List-Unsubscribe"]      = f"<mailto:{self.unsub_addr}?subject=unsubscribe>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
        # Plain text is the canonical part; HTML is the alternative.
        # Order matters: clients pick the LAST acceptable alternative,
        # so HTML must come second to win in HTML-capable clients.
        msg.set_content(body)
        if html:
            msg.add_alternative(html, subtype="html")

        # Port 465 is SMTP-over-SSL (TLS handshake at connection time);
        # port 587 is STARTTLS (upgrade after plaintext greeting). Beget
        # in particular only listens on 25 and 465, not 587 — so port-
        # based detection is the most portable signal.
        ssl_at_connect = (self.port == 465)

        try:
            # Type annotation widens the union — first-branch inference
            # locked it to SMTP_SSL and rejected the SMTP fallback.
            smtp_cls: type[smtplib.SMTP]
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

def render_otp_email(*, code: str, purpose: str, ttl_minutes: int) -> tuple[str, str, str]:
    """
    Return (subject, text_body, html_body).

    Both alternatives are returned so the sender can attach them as
    multipart/alternative. HTML-capable clients render the styled
    version; plain-text clients (and spam filters) get the text body.
    The two MUST convey the same information — Gmail/mail.ru penalise
    HTML/text mismatch as a phishing signal.
    """
    if purpose == "signup":
        subject = "Подтверждение регистрации в SKU Forecasting"
        intro   = "Введите этот временный код подтверждения, чтобы продолжить:"
        note    = (
            "Проигнорируйте это письмо, если вы не пытались "
            "зарегистрироваться в SKU Forecasting."
        )
    else:  # login
        subject = "Код для входа в SKU Forecasting"
        intro   = "Введите этот временный код подтверждения, чтобы продолжить:"
        note    = (
            "Если вы не запрашивали вход, проигнорируйте письмо и "
            "смените пароль почтового ящика — возможно, ваш адрес знает кто-то ещё."
        )

    text_body = (
        f"{intro}\n\n"
        f"{code}\n\n"
        f"Код действителен {ttl_minutes} минут.\n\n"
        f"{note}\n\n"
        f"С уважением,\n"
        f"Команда SKU Forecasting"
    )
    html_body = _render_otp_html(code=code, intro=intro, note=note, ttl_minutes=ttl_minutes)
    return subject, text_body, html_body


def _render_otp_html(*, code: str, intro: str, note: str, ttl_minutes: int) -> str:
    """
    Minimal, OpenAI-style transactional email.

    Inline styles only — most webmail clients (Gmail, mail.ru) strip
    <style> blocks and `class=`. Layout is a single centred column on
    a white card with a tiny brand mark, the code in a highlighted
    box, a one-line note, and a hairline footer.
    """
    return f"""<!doctype html>
<html lang="ru">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>SKU Forecasting</title>
  </head>
  <body style="margin:0;padding:0;background:#F8FAFC;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Helvetica Neue',Arial,sans-serif;color:#020817;-webkit-font-smoothing:antialiased;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F8FAFC;">
      <tr>
        <td align="center" style="padding:40px 16px;">
          <table role="presentation" width="480" cellpadding="0" cellspacing="0" style="max-width:480px;background:#FFFFFF;border:1px solid #E2E8F0;border-radius:8px;">
            <tr>
              <td style="padding:32px 32px 28px 32px;">
                <div style="font-size:22px;font-weight:700;letter-spacing:-0.01em;color:#020817;margin-bottom:28px;">
                  SKU Forecasting
                </div>
                <div style="font-size:14px;line-height:20px;color:#64748B;margin-bottom:12px;">
                  {intro}
                </div>
                <div style="background:#F1F5F9;border-radius:6px;padding:18px 20px;font-size:22px;font-weight:600;letter-spacing:0.18em;color:#020817;font-family:'SFMono-Regular',Menlo,Consolas,monospace;margin-bottom:20px;">
                  {code}
                </div>
                <div style="font-size:13px;line-height:18px;color:#94A3B8;margin-bottom:24px;">
                  Код действителен {ttl_minutes} минут.<br>
                  {note}
                </div>
                <div style="font-size:13px;line-height:18px;color:#64748B;">
                  С уважением,<br>
                  Команда SKU Forecasting
                </div>
                <div style="height:1px;background:#E2E8F0;margin:28px 0 20px 0;"></div>
                <div style="font-size:13px;font-weight:600;color:#020817;margin-bottom:8px;">
                  SKU Forecasting
                </div>
                <div style="font-size:12px;line-height:18px;">
                  <a href="https://skusystem.ru/" style="color:#64748B;text-decoration:underline;">Веб-кабинет</a>
                  &nbsp;·&nbsp;
                  <a href="https://skusystem.ru/plans" style="color:#64748B;text-decoration:underline;">Тарифы</a>
                  &nbsp;·&nbsp;
                  <a href="mailto:dochub.org@gmail.com" style="color:#64748B;text-decoration:underline;">Поддержка</a>
                </div>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>"""
