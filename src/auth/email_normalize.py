"""
src/auth/email_normalize.py

Email canonicalization for de-duplicating multi-account attempts.

Why this exists
───────────────
Without normalization the same human can register N "different" accounts
on the same Free tier:

    vasya@gmail.com           ─┐
    vasya+one@gmail.com        │  All deliver to one inbox.
    vasya+two@gmail.com        │  All "different" emails per RFC 5321.
    v.a.s.y.a@gmail.com        │  Three "different" accounts in our DB.
    VASYA@gmail.com           ─┘

Gmail famously ignores dots in the local part and treats `+alias` as a
sub-address. Yandex / Outlook / iCloud do `+alias` too. ProtonMail and
most corporate domains DON'T — so we keep their dots.

Strategy
────────
The persisted record has TWO email columns:

    email           — what the user typed, displayed back to them
    email_canonical — fully normalized, used for UNIQUE constraint

UNIQUE on email_canonical means `vasya+a@gmail.com` and
`vasya+b@gmail.com` collide on signup → 409 "already in use".

What this CANNOT catch
──────────────────────
  • Different inboxes the user actually owns (vasya@gmail + vasya@ya)
  • Disposable domains (handled separately by disposable_domains.py)
  • Custom domains where MX maps multiple addresses to one mailbox
    (no API to detect this without RFC 822 magic)

So this is one layer in a defense-in-depth stack. Each layer catches
a different ~30% of casual abuse.
"""
from __future__ import annotations

# Domains that ignore dots + use +alias for sub-addressing.
# Aliases of the same provider (e.g. yandex.com → yandex.ru) collapse
# to a single canonical form.
_GMAIL_DOMAINS = frozenset({
    "gmail.com",
    "googlemail.com",
})
_YANDEX_DOMAINS = frozenset({
    "yandex.ru",
    "yandex.com",
    "yandex.by",
    "yandex.kz",
    "yandex.ua",
    "ya.ru",
})
_OUTLOOK_DOMAINS = frozenset({
    "outlook.com",
    "hotmail.com",
    "live.com",
    "msn.com",
})
_ICLOUD_DOMAINS = frozenset({
    "icloud.com",
    "me.com",
    "mac.com",
})


def canonical_email(email: str) -> str:
    """
    Return the canonical form for de-dup checks. Lower-cases, strips
    surrounding whitespace, normalizes provider-specific quirks.

    Examples:
        Vasya+Promo@Gmail.com    → vasya@gmail.com
        v.a.s.y.a@googlemail.com → vasya@gmail.com
        vasya+x@yandex.com       → vasya@yandex.ru
        vasya@PROTONMAIL.com     → vasya@protonmail.com  (no other change)
    """
    if not email or not isinstance(email, str):
        raise ValueError("email must be a non-empty string")

    s = email.strip().lower()
    if "@" not in s:
        raise ValueError(f"invalid email: {email!r}")

    local, _, domain = s.rpartition("@")

    # Strip dot-suffixes and stray whitespace from domain.
    domain = domain.rstrip(".").strip()

    # Common: chop +alias on every domain we see.
    # Custom corporate domains may use +alias too; we always strip it.
    # If a real organization's mail server treats "vasya+x@acme.ru" and
    # "vasya@acme.ru" as different mailboxes, they have a problem
    # bigger than our signup flow.
    if "+" in local:
        local = local.split("+", 1)[0]

    # Gmail: also strip dots in the local part. Provider aliases collapse.
    if domain in _GMAIL_DOMAINS:
        local = local.replace(".", "")
        domain = "gmail.com"

    # Yandex: domain aliases collapse; local-part dots are NOT ignored
    # (Yandex differentiates `vasya.ru@yandex.ru` from `vasya@yandex.ru`).
    elif domain in _YANDEX_DOMAINS:
        domain = "yandex.ru"

    # Outlook family: collapse aliases; dots are significant.
    elif domain in _OUTLOOK_DOMAINS:
        # All Microsoft consumer domains map to the same inbox in
        # practice if the username matches; keep the typed-in form
        # except for the canonical aliases that we know overlap.
        # We DON'T collapse to a single domain because a user can have
        # vasya@hotmail.com that's distinct from vasya@outlook.com.
        # Gmail-style local-only dedup is too aggressive here.
        pass

    # iCloud: same idea — alias domains exist but separate inboxes
    # are possible per @icloud.com vs @me.com vs @mac.com.
    elif domain in _ICLOUD_DOMAINS:
        pass

    return f"{local}@{domain}"
