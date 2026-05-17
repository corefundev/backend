"""
src/auth/signup_rate_limit.py

Subnet-level rate limiting for signup attempts. Two windows:

  • per-hour SOFT cap on /auth/signup attempts
        — protects against captcha-bypassing scripts and signup floods
        — exceeding it returns 429
  • per-day HARD cap on SUCCESSFUL signups
        — limits how many fresh accounts come from one /24 in a day
        — exceeding it returns 429 even if everything else is valid

We bucket on /24 for IPv4 and /48 for IPv6. /32 (single IP) is too narrow
— shared NAT in offices and ISPs lights up false positives. /16 is too
wide — would block whole regions in countries with consolidated mobile
operators. /24 is the customary tradeoff.

Storage
───────
Redis is the right tool: native atomic INCR + EXPIRE, sub-millisecond
latency, already in our stack. Keys use distinct prefixes so they can be
inspected/SCAN'd by ops without touching anything else.

    rl:signup:attempt:{subnet}:{hour}    INT, TTL 1h
    rl:signup:success:{subnet}:{date}    INT, TTL 24h

If Redis is unreachable, we FAIL OPEN — better to let a few signups
through than to block legitimate users entirely. Redis health is
monitored separately by /readyz.

IP extraction
─────────────
Behind nginx, the client IP arrives in `X-Forwarded-For`. We trust the
LAST hop only (peer-address) when no proxy is configured, otherwise we
take the LEFT-most entry that's NOT in our trusted-proxy CIDR. This
avoids the classic "user-supplied X-Forwarded-For poisoning" — a curl
client can put anything in that header.
"""
from __future__ import annotations

import ipaddress
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


# ── Limits (R5-M4 — sourced from central Settings) ─────────────────────
#
# All limit / TRUSTED_PROXIES values now come from `src.settings.settings`.
# This is what the audit flagged: rate-limit tunables read from
# os.environ.get on EVERY request was wasteful + risked drift between
# env-var name and the documented default. Settings is constructed
# once at startup; subsequent reads are cached attribute access.
#
# To re-read at runtime (e.g. for tests overriding env after import),
# construct a fresh `Settings()` inside the test. The module-level
# `settings` import binds at module load time.

from src.settings import settings


def _attempt_limit() -> int:
    return settings.signup_attempt_per_hour_per_subnet


def _success_limit() -> int:
    return settings.signup_success_per_day_per_subnet


def _trusted_proxies() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """
    CIDRs we trust as our own reverse proxy (nginx in our compose).

    Audit R3-14 — default narrowed to loopback only. The previous
    default (`127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16`)
    treated EVERY container on the Docker bridge network as a trusted
    proxy, so a compromised worker could spoof `X-Forwarded-For` and
    bypass the per-subnet rate limit.

    Production deployments override via the `TRUSTED_PROXIES` env var
    (Lockbox-injected) with the actual nginx bridge CIDR.
    """
    out: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for c in settings.trusted_proxies.split(","):
        c = c.strip()
        if c:
            try:
                out.append(ipaddress.ip_network(c, strict=False))
            except ValueError:
                logger.warning("TRUSTED_PROXIES: bad CIDR %r — skipped", c)
    return out


# ── IP extraction from FastAPI Request ──────────────────────────────────

def client_ip(request) -> Optional[str]:
    """
    Resolve the actual client IP from a FastAPI / Starlette Request.

    Header trust model:
      • If the immediate peer is in TRUSTED_PROXIES, walk the
        X-Forwarded-For list right-to-left and take the first IP that
        is NOT in TRUSTED_PROXIES — that's the closest untrusted hop.
      • If the peer is NOT trusted (e.g. local dev with no proxy), use
        the peer address directly. Don't trust client-supplied headers.
    """
    peer = (request.client.host if request.client else None) or "0.0.0.0"
    try:
        peer_ip = ipaddress.ip_address(peer)
    except ValueError:
        return peer

    trusted = _trusted_proxies()
    is_trusted = any(peer_ip in net for net in trusted)
    if not is_trusted:
        return peer

    xff = request.headers.get("x-forwarded-for", "")
    candidates = [c.strip() for c in xff.split(",") if c.strip()]
    # Walk right-to-left; the rightmost is the closest hop (which is
    # likely our own nginx). The first one NOT in trusted CIDRs is the
    # real client.
    for c in reversed(candidates):
        try:
            ip = ipaddress.ip_address(c)
        except ValueError:
            continue
        if not any(ip in net for net in trusted):
            return c
    # All candidates were trusted (multiple internal hops?); fall back
    # to the peer.
    return peer


def subnet(ip_str: str) -> str:
    """Return /24 for IPv4, /48 for IPv6 — used as the bucket key."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return ip_str
    if isinstance(ip, ipaddress.IPv4Address):
        return str(ipaddress.ip_network(f"{ip}/24", strict=False).network_address) + "/24"
    return str(ipaddress.ip_network(f"{ip}/48", strict=False).network_address) + "/48"


# ── Redis interaction ──────────────────────────────────────────────────

def _redis():
    """Return a Redis connection or None if unreachable. Caller fails open.

    R5-M3 (2026-05-17) — routes through `src.redis_pool` (the canonical
    process-wide Redis abstraction added by R3-4) instead of reaching
    into `src.pipeline.task_queue` for `get_redis_connection`. The
    pipeline module is a worker-side concern; auth is HTTP-side; the
    cross-layer import made auth tests transitively load the rq
    package + bootstrap_secrets, slowing them and creating a fragile
    coupling whose only justification was that the connection-pool
    helper happened to live there.
    """
    try:
        from src.redis_pool import get_redis_or_none
        return get_redis_or_none()
    except Exception as e:
        logger.warning("signup rate-limit: Redis unavailable (%s) — failing open", e)
        return None


class RateLimited(Exception):
    """Caller has exceeded the limit; HTTP layer maps this to 429."""

    def __init__(self, message: str, retry_after_sec: int):
        super().__init__(message)
        self.retry_after_sec = retry_after_sec


# Atomic INCR + EXPIRE via single Lua eval. Prevents a race where the
# client crashes between INCR and EXPIRE — leaving an immortal counter
# that DoS's the subnet forever. EVAL is atomic in Redis.
_INCR_TOUCH_LUA = """
local n = redis.call('INCR', KEYS[1])
if n == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return n
"""


def _incr_with_ttl(r, key: str, ttl_sec: int) -> int:
    """Atomic INCR-and-set-TTL-on-first-hit. Returns the new counter value."""
    return int(r.eval(_INCR_TOUCH_LUA, 1, key, ttl_sec))


# R5-M2 (2026-05-17) — six `check_*_attempt` functions previously each
# repeated the same six-step shape (redis-or-fail-open, build bucket
# key, _incr_with_ttl, fetch limit, raise RateLimited with ttl). The
# audit flagged this as duplication; collapsed here into one private
# helper. Each public `check_*_attempt` wrapper now owns only its
# rate-limit policy (prefix, subject, limit source, user message).
#
# Behaviour preserved: same keys, same TTL, same fail-open semantics,
# same `RateLimited(retry_after_sec=...)` exception shape so any
# 429-mapping HTTP handler keeps working untouched.

def _hour_bucket_check(
    prefix: str,
    subject: str,
    limit: int,
    user_message: str,
) -> None:
    """
    Common rate-limit primitive — atomic INCR on an hour-bucketed
    Redis key, raise RateLimited if count exceeds `limit`.

    Args:
      prefix:        Redis key namespace (e.g. "signup:attempt").
      subject:       per-caller bucket suffix (subnet or client_id).
      limit:         max calls per hour. <=0 → unlimited (early-return).
      user_message:  template for the 429 body. Must contain the
                     literal `{ttl_min}` placeholder for "Try again
                     in N minutes" formatting.
    """
    if limit <= 0:
        return                  # unlimited
    r = _redis()
    if r is None:
        return                  # fail open — auth path must not fail-closed on Redis outage

    hour = datetime.now(timezone.utc).strftime("%Y%m%d%H")
    key = f"rl:{prefix}:{subject}:{hour}"

    count = _incr_with_ttl(r, key, 3600)
    if count > limit:
        ttl = int(r.ttl(key) or 3600)
        raise RateLimited(
            user_message.format(ttl_min=ttl // 60 + 1),
            retry_after_sec=max(1, ttl),
        )


def check_signup_attempt(ip: str) -> None:
    """Per-/24 rate-limit for /auth/signup (R2-4).

    Counts every attempt regardless of captcha outcome — bot scripts
    that fail captcha should still get throttled. Default limit from
    `_attempt_limit()` (env SIGNUP_ATTEMPT_PER_HOUR_PER_SUBNET).
    """
    _hour_bucket_check(
        prefix="signup:attempt",
        subject=subnet(ip),
        limit=_attempt_limit(),
        user_message="Too many signup attempts from your network. "
                     "Try again in {ttl_min} minutes.",
    )


def check_predict_attempt(client_id: str, limit_per_hour: int | None) -> None:
    """Per-client rate-limit for /predict + /predict/batch (R2-10).

    Authenticated endpoint — subject is `client_id`, not IP. Limit
    comes from the plan spec (`predict_requests_per_hour`); None or 0
    = unlimited (Business tier).
    """
    _hour_bucket_check(
        prefix="predict",
        subject=client_id,
        limit=int(limit_per_hour or 0),
        user_message=f"Predict request rate exceeded for this plan "
                     f"({limit_per_hour}/hour). Try again in "
                     f"{{ttl_min}} minutes or upgrade for higher throughput.",
    )


def check_token_attempt(ip: str) -> None:
    """Per-/24 rate-limit for /auth/token (R2-4).

    bcrypt cost=12 throttles single-host to ~4 req/sec, but distributed
    attacks aren't bounded by that. Limit from settings.
    """
    _hour_bucket_check(
        prefix="token:attempt",
        subject=subnet(ip),
        limit=settings.token_attempt_per_hour_per_subnet,
        user_message="Too many token-exchange attempts from your network. "
                     "Try again in {ttl_min} minutes.",
    )


def check_login_attempt(ip: str) -> None:
    """Per-/24 rate-limit for /auth/login (R4-3).

    Captcha-solver farms flood /auth/login with valid captchas,
    triggering OTP creates + email sends. Limit from settings.
    """
    _hour_bucket_check(
        prefix="login:attempt",
        subject=subnet(ip),
        limit=settings.login_attempt_per_hour_per_subnet,
        user_message="Too many login attempts from your network. "
                     "Try again in {ttl_min} minutes.",
    )


def check_otp_verify_attempt(ip: str) -> None:
    """Per-/24 rate-limit for /auth/login/verify + /auth/signup/verify (R4-4).

    Closes the cross-email OTP-spray gap. Limit from settings.
    """
    _hour_bucket_check(
        prefix="otp:verify",
        subject=subnet(ip),
        limit=settings.otp_verify_per_hour_per_subnet,
        user_message="Too many verification attempts from your network. "
                     "Try again in {ttl_min} minutes.",
    )


def check_rotate_attempt(client_id: str) -> None:
    """Per-client rate-limit for /clients/{id}/api-key/rotate (R3-24).

    Each rotate burns bcrypt cost=12 + writes audit_log. Limit from
    settings.
    """
    rotate_limit = settings.rotate_attempt_per_hour_per_client
    _hour_bucket_check(
        prefix="apikey:rotate",
        subject=client_id,
        limit=rotate_limit,
        user_message=f"API key rotation rate exceeded ({rotate_limit}/hour). "
                     "Try again in {ttl_min} minutes.",
    )


def record_signup_success(ip: str) -> None:
    """
    Bump the per-day success counter. Call AFTER /auth/signup/verify
    creates the client record.

    If the new value exceeds the daily cap, raise RateLimited — the
    caller should then DELETE the just-created client and tell the user
    to come back tomorrow. (We could also pre-check before creating, but
    the post-check race window is harmless: at most one extra account
    slips through per day, which is below the noise floor.)
    """
    r = _redis()
    if r is None:
        return                  # fail open

    bucket = subnet(ip)
    date = datetime.now(timezone.utc).strftime("%Y%m%d")
    key = f"rl:signup:success:{bucket}:{date}"

    count = _incr_with_ttl(r, key, 24 * 3600)

    limit = _success_limit()
    if count > limit:
        ttl = int(r.ttl(key) or 24 * 3600)
        raise RateLimited(
            f"Too many new accounts created from your network today. Try again tomorrow.",
            retry_after_sec=max(60, ttl),
        )


def assert_signup_allowed(ip: str) -> None:
    """
    Pre-check called from /auth/signup/verify, BEFORE creating the
    client. We check the daily cap here (the attempt cap is already
    enforced on /auth/signup). This way we don't have to delete a
    half-created client if the cap was hit between attempt and verify.
    """
    r = _redis()
    if r is None:
        return

    bucket = subnet(ip)
    date = datetime.now(timezone.utc).strftime("%Y%m%d")
    key = f"rl:signup:success:{bucket}:{date}"

    count = int(r.get(key) or 0)
    if count >= _success_limit():
        ttl = int(r.ttl(key) or 24 * 3600)
        raise RateLimited(
            "Too many new accounts created from your network today. Try again tomorrow.",
            retry_after_sec=max(60, ttl),
        )
