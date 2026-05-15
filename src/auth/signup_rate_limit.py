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
import os
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


# ── Limits (env-tunable) ────────────────────────────────────────────────

def _attempt_limit() -> int:
    return int(os.environ.get("SIGNUP_ATTEMPT_PER_HOUR_PER_SUBNET", "10"))


def _success_limit() -> int:
    return int(os.environ.get("SIGNUP_SUCCESS_PER_DAY_PER_SUBNET", "3"))


def _trusted_proxies() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """
    CIDRs we trust as our own reverse proxy (nginx in our compose).

    Audit R3-14 — default narrowed to loopback only. The previous
    default (`127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16`)
    treated EVERY container on the Docker bridge network as a trusted
    proxy, so a compromised worker could spoof `X-Forwarded-For` and
    bypass the per-subnet rate limit.

    Production deployments override via the `TRUSTED_PROXIES` env var
    (Lockbox-injected) with the actual nginx bridge CIDR, e.g.:
        TRUSTED_PROXIES=127.0.0.0/8,172.20.0.0/16

    The fail-closed default means dev/single-container setups skip the
    X-Forwarded-For walk entirely and use the peer address directly —
    correct behaviour when there's no real proxy in front.
    """
    raw = os.environ.get("TRUSTED_PROXIES", "127.0.0.0/8")
    out: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for c in raw.split(","):
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
    """Return a Redis connection or None if unreachable. Caller fails open."""
    try:
        from src.pipeline.task_queue import get_redis_connection
        return get_redis_connection()
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


def check_signup_attempt(ip: str) -> None:
    """
    Increment the per-hour attempt counter for `ip`'s subnet.
    Raises RateLimited if the new count exceeds SIGNUP_ATTEMPT_PER_HOUR_PER_SUBNET.

    Called BEFORE expensive work (captcha verify, email send) on
    /auth/signup. Counts every attempt, success or fail — bot scripts
    that fail captcha should still get throttled.
    """
    r = _redis()
    if r is None:
        return                  # fail open

    bucket = subnet(ip)
    hour = datetime.now(timezone.utc).strftime("%Y%m%d%H")
    key = f"rl:signup:attempt:{bucket}:{hour}"

    count = _incr_with_ttl(r, key, 3600)

    limit = _attempt_limit()
    if count > limit:
        ttl = int(r.ttl(key) or 3600)
        raise RateLimited(
            f"Too many signup attempts from your network. Try again in {ttl // 60 + 1} minutes.",
            retry_after_sec=max(1, ttl),
        )


def check_predict_attempt(client_id: str, limit_per_hour: int | None) -> None:
    """
    Per-client rate-limit for /predict + /predict/batch.

    Authenticated endpoints — the natural subject is `client_id`, not
    IP. Limit comes from the plan spec (`predict_requests_per_hour`);
    None = unlimited (Business tier), early-return.

    Sliding hour-bucket keyed by client_id. Failing open on Redis
    outage matches the rest of the rate-limit family.

    Audit R2-10 (2026-05-15) — free-tier abuse otherwise floods
    inference pipeline and exhausts capacity for other tenants.
    """
    if limit_per_hour is None or limit_per_hour <= 0:
        return                  # unlimited

    r = _redis()
    if r is None:
        return                  # fail open

    hour = datetime.now(timezone.utc).strftime("%Y%m%d%H")
    key = f"rl:predict:{client_id}:{hour}"

    count = _incr_with_ttl(r, key, 3600)

    if count > limit_per_hour:
        ttl = int(r.ttl(key) or 3600)
        raise RateLimited(
            f"Predict request rate exceeded for this plan "
            f"({limit_per_hour}/hour). Try again in {ttl // 60 + 1} minutes "
            f"or upgrade for higher throughput.",
            retry_after_sec=max(1, ttl),
        )


def check_token_attempt(ip: str) -> None:
    """
    Per-IP rate-limit for /auth/token (api-key → JWT exchange).

    bcrypt cost=12 already throttles single-host brute-force to ~4 req/sec,
    but distributed attacks aren't bounded by that. Without an IP-level
    cap, an attacker with botnet-scale fanout can iterate the api_key
    keyspace across many origins.

    Bucket: same /24 subnet logic as `check_signup_attempt`. Limit
    defaults to 20/hour (override via env `TOKEN_ATTEMPT_PER_HOUR_PER_SUBNET`).
    Failing open on Redis outage matches signup behaviour — auth must
    not fail-closed on a Redis hiccup.

    Audit R2-4 (2026-05-15).
    """
    r = _redis()
    if r is None:
        return                  # fail open

    bucket = subnet(ip)
    hour = datetime.now(timezone.utc).strftime("%Y%m%d%H")
    key = f"rl:token:attempt:{bucket}:{hour}"

    count = _incr_with_ttl(r, key, 3600)

    limit = int(os.environ.get("TOKEN_ATTEMPT_PER_HOUR_PER_SUBNET", "20"))
    if count > limit:
        ttl = int(r.ttl(key) or 3600)
        raise RateLimited(
            f"Too many token-exchange attempts from your network. "
            f"Try again in {ttl // 60 + 1} minutes.",
            retry_after_sec=max(1, ttl),
        )


def check_rotate_attempt(client_id: str) -> None:
    """
    Per-client rate-limit for /clients/{id}/api-key/rotate.

    Audit R3-24 — without this, a compromised client (or a benign
    misbehaving script) can spam rotates: each call invalidates the
    previous key, mints a new one, AND fires an audit-log entry. Run
    in a tight loop it pollutes audit_log and burns the bcrypt cost-12
    hashing budget. Cap defaults to 5/hour/client (env-tunable via
    ROTATE_ATTEMPT_PER_HOUR_PER_CLIENT). Failing open on Redis outage
    matches the rest of the rate-limit family.
    """
    r = _redis()
    if r is None:
        return                  # fail open

    hour = datetime.now(timezone.utc).strftime("%Y%m%d%H")
    key = f"rl:apikey:rotate:{client_id}:{hour}"

    count = _incr_with_ttl(r, key, 3600)

    limit = int(os.environ.get("ROTATE_ATTEMPT_PER_HOUR_PER_CLIENT", "5"))
    if count > limit:
        ttl = int(r.ttl(key) or 3600)
        raise RateLimited(
            f"API key rotation rate exceeded ({limit}/hour). "
            f"Try again in {ttl // 60 + 1} minutes.",
            retry_after_sec=max(1, ttl),
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
