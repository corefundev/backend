"""
Authentication API — token exchange, self-service signup +
email-OTP login, JWT logout, OAuth (Google + Yandex + VK).

R5-M1 (2026-05-18) slice 9: the biggest single slice — extracted
from src/api/main.py. 9 routes + 9 request/response schemas + 4
helpers. Critical hot path (/auth/token is hit on every authed
request that doesn't have a valid JWT yet).

Preserves verbatim
──────────────────
* R2-4 per-subnet rate-limit on /auth/token + /auth/signup
* R2-6 max-length bounds on TokenRequest fields
* R3-9 anti-enumeration on duplicate-email signup (silent 202)
* R4-3 per-subnet rate-limit on /auth/login (R5-M7 DI shape)
* R4-4 cross-email OTP-spray cap on /auth/login/verify
* R5-9 hmac.compare_digest for STATIC_API_KEY / ADMIN_API_KEY (no raw `==`)
* R4-12 HS256 key length floor enforced inside jwt_auth (not here)
* R6-2 model_config = ConfigDict(protected_namespaces=()) on
  SignupVerifyResponse (`model_name` field)
* All audit events (EVT_SIGNUP/EVT_LOGIN/EVT_OTP_*/EVT_OAUTH_CALLBACK)
* OAuth state CSRF token round-trip (encode/decode via state.py)

Lazy imports preserved
──────────────────────
Email-sender, OTP, captcha, OAuth providers, api_keys helpers,
disposable_domains, email_normalize — all kept as function-local
imports because (a) some are optional deps (email_sender, captcha
provider config), (b) the OAuth provider modules pull external HTTP
clients that we don't want to load on /auth/token cold path. A
follow-up M6-style hoist can land later if observability says
import overhead matters.
"""
from __future__ import annotations

import logging
import os
import re as _re
from typing import Optional, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from src.api._helpers import email_hash as _email_hash
from src.audit import (
    EVT_LOGIN,
    EVT_OAUTH_CALLBACK,
    EVT_OTP_SEND,
    EVT_OTP_VERIFY,
    EVT_SIGNUP,
    recent_failed_logins,
    record_event,
)
from src.auth.jwt_auth import (
    AuthContext,
    assert_account_open,
    create_access_token,
    get_current_client,
)
from src.auth.signup_rate_limit import (
    RateLimited,
    assert_signup_allowed,
    check_login_attempt,
    check_otp_verify_attempt,
    check_signup_attempt,
    check_token_attempt,
    client_ip,
    record_signup_success,
)
from src.clients.registry import ClientRecord, get_registry
from src.plans.plans import Plan, get_plan_spec
from src.storage.backend import ClientStorage


logger = logging.getLogger(__name__)
router = APIRouter(tags=["auth"])


# R11-L14: _parse_origins() deduped into src.api._origins.parse_frontend_origins
# (the OAuth open-redirect guard below validates FRONTEND_OAUTH_RETURN_URL
# against the same trusted-origin set the CORS middleware uses).
from src.api._origins import parse_frontend_origins

class TokenRequest(BaseModel):
    # Length bounds: client_id matches our generated id format
    # (≤64 chars); secret holds either ADMIN_API_KEY, an `sku_*` api-key
    # (~40 chars), or the legacy API_KEY env value (≤256 chars).
    # Audit R2-6 (2026-05-15) — DoS-via-huge-body protection.
    client_id: str = Field(..., min_length=1, max_length=64)
    secret:    str = Field(..., min_length=1, max_length=256)

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"

# ══════════════════════════════════════════════════════════════════
# Auth
# ══════════════════════════════════════════════════════════════════



def _notify_ops_admin_login(ip) -> None:
    """ADM-7 H3: fire-and-forget ops-Telegram tripwire on admin token
    issuance. Uses the ALERT bot (ops channel), not the client bot. Void
    best-effort: never raises past this frame, 3s cap so the login path
    can't hang on Telegram."""
    import json as _json
    import urllib.request as _rq
    tok  = os.environ.get("TELEGRAM_ALERT_BOT_TOKEN") or ""
    chat = os.environ.get("TELEGRAM_ALERT_CHAT_ID") or ""
    if not tok or not chat:
        return
    try:
        body = _json.dumps({
            "chat_id": chat,
            "text": f"🔑 ADMIN token issued (ip={ip}). Not you? Rotate "
                    "ADMIN_API_KEY now (docs/OPERATIONS.md → rotation).",
        }).encode()
        req_ = _rq.Request(
            f"https://api.telegram.org/bot{tok}/sendMessage",
            data=body, headers={"Content-Type": "application/json"})
        _rq.urlopen(req_, timeout=3).read()
    except Exception as e:    # noqa: BLE001 — void tripwire, login must not fail
        logger.warning("admin-login tripwire telegram failed: %s", e)

@router.post("/auth/token", response_model=TokenResponse)
def get_token(req: TokenRequest, http_req: Request):    # #184: sync body (bcrypt cost-12) → threadpool, off the event loop
    """
    Exchange client_id + secret for JWT.

    Three resolution paths, in this strict order:

      1. ADMIN_API_KEY (env)
         → JWT with roles=['admin','forecast'] for any client_id.
         Used by the back-office UI and ops scripts.

      2. Per-client api_key (sku_clients.api_key_hash)
         → JWT with roles=['forecast'] for THAT client_id only.
         The preferred path. Each client has a unique secret stored
         as bcrypt hash; knowing one client's key gives no access to
         any other client's data.

      3. Legacy global API_KEY (env), ONLY when the client has no
         api_key_hash yet. Transition path for clients registered
         before per-client keys shipped — they keep working until
         their next key rotation. New clients always use path 2.

    Timing safety: the bcrypt verify in path 2 always runs (even when
    the client_id doesn't exist) so response time can't be used to
    enumerate valid client_ids.

    Rate-limit (audit R2-4): per-/24 subnet, 20/hour by default. bcrypt
    cost=12 throttles single-host brute-force to ~4 req/sec but a
    distributed attack across botnets isn't bounded by that. The IP
    cap forces an attacker to fan out across many subnets to brute-
    force the api_key keyspace, raising operational cost meaningfully.
    """
    import hmac as _hmac
    from src.auth.api_keys import is_well_formed, verify_api_key

    try:
        check_token_attempt(client_ip(http_req))
    except RateLimited as e:
        raise HTTPException(
            status_code=429,
            detail=str(e),
            headers={"Retry-After": str(e.retry_after_sec or 60)},
        )

    api_key_legacy = os.environ.get("API_KEY") or ""
    admin_api_key  = os.environ.get("ADMIN_API_KEY") or ""

    secret_bytes = req.secret.encode()

    # ── 1. ADMIN_API_KEY ────────────────────────────────────────
    if admin_api_key and _hmac.compare_digest(secret_bytes, admin_api_key.encode()):
        token = create_access_token(client_id=req.client_id, roles=["admin", "forecast"])
        # ADM-7 H3 (#260): admin-login tripwire. Single-operator reality =
        # every admin token issuance is either us or an incident; make each
        # one visible (audit row + ops Telegram). Best-effort — the login
        # itself must not fail on a notification hiccup.
        _ip = client_ip(http_req)
        record_event(
            event_type=EVT_LOGIN, event_subtype="admin_token_issued",
            client_id=req.client_id, ip=_ip,
            user_agent=http_req.headers.get("user-agent"),
            target_type="admin_session", target_id=req.client_id,
        )
        _notify_ops_admin_login(_ip)
        return TokenResponse(access_token=token)

    # ── 2. Per-client key ───────────────────────────────────────
    # Always look up the client and run bcrypt verify, even if the
    # client doesn't exist (verify_api_key with hash=None still burns
    # bcrypt time on a dummy). This keeps "client unknown" and
    # "wrong key" indistinguishable by response time.
    record = get_registry().get(req.client_id)
    record_hash = record.api_key_hash if record else None

    if is_well_formed(req.secret):
        if verify_api_key(req.secret, record_hash):
            # ADM-10 (#278) + AUD-5 (#357): a suspended OR closed account
            # gets no NEW tokens on any issuance path — shared gate.
            assert_account_open(record)
            token = create_access_token(client_id=req.client_id, roles=["forecast"])
            # AUD-10 (#362): this path issued tokens with NO audit trace —
            # a 1C/API-key-only client looked forever-inactive to the
            # staleness nudge and the admin activity card, and key-auth
            # left nothing for brute-force forensics. Counted as activity
            # by both queries (event_type='login').
            record_event(
                event_type=EVT_LOGIN, event_subtype="api_key_token_issued",
                client_id=req.client_id, ip=client_ip(http_req),
                user_agent=http_req.headers.get("user-agent"),
                metadata={"method": "api_key"},
            )
            return TokenResponse(access_token=token)
        # Well-formed sku_* key that doesn't match → explicit fail.
        # We do NOT fall through to legacy global key here — once a
        # client has a hash, only that hash is valid for them.
        if record_hash is not None:
            raise HTTPException(status_code=401, detail="Invalid credentials")

    # ── 3. Legacy global API_KEY ────────────────────────────────
    # Only reachable when:
    #   • secret is NOT a well-formed per-client key, OR
    #   • the client exists but has no api_key_hash (pre-Phase-6 record)
    # Once every client has been re-registered with a per-client key,
    # API_KEY env can be unset and this branch becomes dead — at which
    # point the env-check below returns 503 for legacy attempts.
    if record_hash is None and api_key_legacy and \
       _hmac.compare_digest(secret_bytes, api_key_legacy.encode()):
        # AUD-5 (#357): the legacy branch skipped the account-state gate —
        # a suspended/closed pre-Phase-6 client could still mint here.
        assert_account_open(record)
        token = create_access_token(client_id=req.client_id, roles=["forecast"])
        # AUD-10 (#362): same missing-audit gap as the per-client branch.
        record_event(
            event_type=EVT_LOGIN, event_subtype="api_key_token_issued",
            client_id=req.client_id, ip=client_ip(http_req),
            user_agent=http_req.headers.get("user-agent"),
            metadata={"method": "api_key_legacy"},
        )
        return TokenResponse(access_token=token)

    raise HTTPException(status_code=401, detail="Invalid credentials")


# ══════════════════════════════════════════════════════════════════
# Self-service signup + email login (Phase 7)
# ══════════════════════════════════════════════════════════════════
#
# Two parallel two-step flows, modeled on claude.ai:
#
#   Signup  (new client):
#     1. POST /auth/signup        { email, desired_client_id, captcha }
#                                 → if captcha + uniqueness ok, send OTP
#     2. POST /auth/signup/verify { email, code }
#                                 → create client, mint api_key, return JWT
#
#   Login   (existing client, email path):
#     1. POST /auth/login         { email, captcha }
#                                 → if user verified, send OTP
#     2. POST /auth/login/verify  { email, code }
#                                 → return JWT (no api_key here)
#
# Existing /auth/token (client_id + api_key) stays — for ops scripts and
# legacy integrations.
#
# ── Why two paths instead of one
# Signup creates state (client + api_key); login does not. Mixing them
# would mean /auth/login could accidentally provision new clients.
# Separation makes intent explicit at the URL level.
# ══════════════════════════════════════════════════════════════════

_EMAIL_RE = _re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_CLIENT_ID_RE = _re.compile(r"^[a-z0-9][a-z0-9\-]{1,62}[a-z0-9]$")


class SignupRequest(BaseModel):
    email: str = Field(..., max_length=254)
    desired_client_id: str = Field(..., max_length=64)
    captcha_token: Optional[str] = Field(None, description="Cloudflare Turnstile token")
    # LEG-2 #428: явное принятие условий — сервер требует и фиксирует факт
    # согласия в аудите (с версиями документов на момент согласия)
    accepted_terms: bool = False


class LoginEmailRequest(BaseModel):
    email: str = Field(..., max_length=254)
    captcha_token: Optional[str] = None


class VerifyOtpRequest(BaseModel):
    email: str = Field(..., max_length=254)
    code:  str = Field(..., min_length=6, max_length=6)


class SignupAcceptedResponse(BaseModel):
    """Returned by both /auth/signup and /auth/login — the OTP is in email."""
    status: str = "otp_sent"
    email:  str
    expires_in_minutes: int


def _normalize_email(s: str) -> str:
    return s.strip().lower()


def _validate_email_or_400(email: str) -> str:
    e = _normalize_email(email)
    if not _EMAIL_RE.match(e) or len(e) > 254:
        raise HTTPException(status_code=422, detail="Invalid email format")
    return e


def _validate_client_id_or_400(cid: str) -> str:
    cid = cid.strip().lower()
    if not _CLIENT_ID_RE.match(cid):
        raise HTTPException(
            status_code=422,
            detail="client_id must be 3–64 chars, [a-z0-9-], no leading/trailing dash",
        )
    return cid


def _verify_captcha_or_400(token: Optional[str]) -> None:
    from src.auth.captcha import is_disabled, verify_token, CaptchaError
    if is_disabled():
        return
    if not token:
        raise HTTPException(status_code=422, detail="captcha_token is required")
    try:
        if not verify_token(token):
            raise HTTPException(status_code=403, detail="Captcha rejected")
    except CaptchaError as e:
        # Misconfigured server; don't leak details
        logger.error("captcha config: %s", e)
        raise HTTPException(status_code=503, detail="Captcha unavailable")


def _assert_otp_verify_rate_ok(http_req: Request) -> None:
    """Audit R4-4 — per-IP (/24-subnet) cap on OTP verify. Lockout-via-
    audit_log keys on actor_email, so an attacker rotating across many
    emails never trips a per-email limit; this bucket turns cross-email
    OTP-spray into a per-network cost. Shared by /auth/signup/verify and
    /auth/login/verify (ARCH-2 #206 — was verbatim-duplicated in both)."""
    try:
        check_otp_verify_attempt(client_ip(http_req))
    except RateLimited as e:
        raise HTTPException(
            status_code=429, detail=str(e),
            headers={"Retry-After": str(e.retry_after_sec or 60)},
        )


def _assert_not_locked_out(
    canonical: str,
    http_req: Request,
    *,
    event_type: str,
    event_subtype: str,
    attempt_label: str,
) -> None:
    """Per-email brute-force lockout over a sliding audit-log window.

    Shared by /auth/signup/verify and /auth/login/verify (ARCH-2 #206). The
    two endpoints MUST enforce the SAME window/threshold, or an attacker
    dodges the cap by alternating them against one email — a correctness
    invariant a shared helper now guarantees structurally (previously two
    verbatim copies that had to be kept in sync by hand).

    Fail-closed when enabled; fail-open if the audit DB is unavailable
    (recent_failed_logins returns 0). `event_type`/`event_subtype` and
    `attempt_label` are the only per-endpoint differences.
    """
    threshold  = int(os.environ.get("LOGIN_LOCKOUT_THRESHOLD", "10"))
    window_min = int(os.environ.get("LOGIN_LOCKOUT_WINDOW_MIN", "15"))
    if threshold <= 0:
        return
    recent_fails = recent_failed_logins(canonical, window_minutes=window_min)
    if recent_fails < threshold:
        return
    record_event(
        event_type=event_type, event_subtype=event_subtype,
        actor_email=canonical,
        ip=client_ip(http_req), user_agent=http_req.headers.get("user-agent"),
        success=False,
        metadata={
            "recent_fails":  recent_fails,
            "window_min":    window_min,
            "threshold":     threshold,
        },
    )
    raise HTTPException(
        status_code=429,
        detail=(
            f"Too many failed {attempt_label}; try again in "
            f"{window_min} minutes."
        ),
        headers={"Retry-After": str(window_min * 60)},
    )


@router.post("/auth/signup", response_model=SignupAcceptedResponse)
async def auth_signup(req: SignupRequest, http_req: Request):
    """
    Step 1 of self-service registration.

    Anti-abuse stack (in order of execution):
      1. Captcha (Cloudflare Turnstile)
      2. Subnet rate-limit (per-hour attempts, /24)
      3. Email shape validation (regex)
      4. Disposable-domain blacklist
      5. Email canonicalization (gmail dots, +alias, provider aliases)
      6. Uniqueness — by canonical_email AND by client_id
      7. OTP generation + send

    Why this order:
      • Captcha first — cheapest reject for botnets.
      • Rate-limit before any DB work — protects DB under flood.
      • Disposable check before sending email — saves Resend quota.
      • Canonicalization happens in the registry lookup (get_by_email
        already canonicalizes inside) — we also stash canonical_email
        in the OTP row so /verify uses the same value at insert time.
    """
    from src.auth.disposable_domains import is_disposable
    from src.auth.email_normalize import canonical_email

    # 1. Captcha
    _verify_captcha_or_400(req.captcha_token)

    # 2. Subnet rate-limit (any attempt, valid or not, counts)
    ip = client_ip(http_req) or "0.0.0.0"
    try:
        check_signup_attempt(ip)
    except RateLimited as e:
        raise HTTPException(
            status_code=429, detail=str(e),
            headers={"Retry-After": str(e.retry_after_sec)},
        )

    # 2b. LEG-2 #428: без явного согласия регистрация не принимается.
    # Факт согласия пишем в аудит СРАЗУ (не на /verify): юридически
    # значим момент проставления галочки; версии документов — на этот
    # момент. Лог best-effort не бывает — согласие без следа бессмысленно,
    # поэтому сбой аудита роняет запрос (fail-closed).
    if not req.accepted_terms:
        raise HTTPException(
            status_code=422,
            detail="Необходимо принять пользовательское соглашение и политику конфиденциальности",
        )
    from src.storage.legal import get_legal_store
    _store = get_legal_store()
    _doc_versions = {}
    for _d in ("terms", "privacy", "consent"):
        _doc = _store.get(_d)
        if _doc is not None:
            _doc_versions[_d] = _doc.version
    record_event(
        event_type=EVT_SIGNUP, event_subtype="consent_accepted",
        client_id=None, ip=ip,
        user_agent=http_req.headers.get("user-agent"),
        target_type="signup", target_id=req.desired_client_id[:64],
        metadata={"email": req.email[:254],
                  "doc_versions": _doc_versions},
    )

    # 3+4. Email shape + disposable
    email     = _validate_email_or_400(req.email)
    if is_disposable(email):
        raise HTTPException(
            status_code=422,
            detail="Disposable / temporary email addresses are not accepted. "
                   "Use your real corporate or personal email.",
        )
    client_id = _validate_client_id_or_400(req.desired_client_id)

    # 5. Canonicalize for uniqueness check
    try:
        canonical = canonical_email(email)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid email format")

    registry = get_registry()

    from src.auth.otp_store import otp_ttl, PURPOSE_SIGNUP
    ttl_min = int(otp_ttl().total_seconds() / 60)

    # 6. Uniqueness check — audit R3-9.
    #
    # Email duplicates are NOT surfaced to the caller. Returning 409 vs
    # 202 on the same flow lets an attacker enumerate registered emails
    # by polling /auth/signup — a known anti-pattern (OWASP, CWE-203).
    # Instead we return the same 202 shape, skip OTP creation and email
    # send, and emit an audit event so operators can monitor abuse.
    #
    # Client_id duplicates DO still surface as 409 — client_id is a
    # user-chosen workspace handle (like a public @username), not PII,
    # and silently accepting would lock honest users out of obvious
    # alternatives. Caller's frontend explains the collision.
    existing = registry.get_by_email(email)
    if existing is not None:
        logger.info("signup: duplicate email_hash=%s (silent 202)", _email_hash(canonical))
        try:
            record_event(
                event_type=EVT_SIGNUP,
                event_subtype="duplicate_email_ignored",
                client_id=existing.client_id,
                actor_email=canonical,
                ip=client_ip(http_req),
                user_agent=http_req.headers.get("user-agent"),
                success=False,
                metadata={"reason": "email_already_registered"},
            )
        except Exception as e:    # noqa: BLE001
            logger.warning("signup audit event failed: %s", e)
        return SignupAcceptedResponse(email=email, expires_in_minutes=ttl_min)

    if registry.get(client_id) is not None:
        # client_id collision with a *different* email — the caller picked
        # a workspace handle someone else owns. Surface so they can choose
        # another. NOT an enumeration concern: client_id is the public
        # workspace identifier.
        logger.info("signup: duplicate client_id=%s", client_id)
        raise HTTPException(status_code=409, detail="Workspace identifier already taken; choose another")

    # 7. OTP + email
    from src.auth.otp import generate_otp, hash_otp
    from src.auth.otp_store import get_otp_store
    from src.auth.email_sender import get_email_sender, render_otp_email, EmailDeliveryError

    store = get_otp_store()
    code  = generate_otp()
    # Store under canonical email — that's how /verify will find it.
    store.create(email=canonical, purpose=PURPOSE_SIGNUP, code_hash=hash_otp(code))
    # Stash desired client_id + display email alongside (for /verify).
    store.create(email=canonical, purpose=f"{PURPOSE_SIGNUP}:cid", code_hash=client_id)
    store.create(email=canonical, purpose=f"{PURPOSE_SIGNUP}:display", code_hash=email)

    subject, body, html = render_otp_email(code=code, purpose=PURPOSE_SIGNUP, ttl_minutes=ttl_min)
    try:
        # Send to the user-typed address (not the canonical) — that's
        # what they read; canonical is internal only.
        get_email_sender().send(to=email, subject=subject, body=body, html=html)
    except EmailDeliveryError as e:
        # Hash the email — operator log access shouldn't be an enumeration
        # vector. Same hash as the dedup log line above so ops can still
        # correlate "this address signed up earlier" with "send failed".
        logger.error("signup email failed for email_hash=%s: %s", _email_hash(email), e)
        raise HTTPException(status_code=503, detail="Could not send email; try again")

    return SignupAcceptedResponse(email=email, expires_in_minutes=ttl_min)


class SignupVerifyResponse(BaseModel):
    # R6-2 — same `model_` namespace collision as PredictResponse.
    model_config = ConfigDict(protected_namespaces=())

    client_id:   str
    plan:        str
    model_name:  str
    api_key:     str
    access_token: str
    token_type:   str = "bearer"
    warning:      str = "Сохраните api_key — он показан один раз."


@router.post("/auth/signup/verify", response_model=SignupVerifyResponse)
async def auth_signup_verify(req: VerifyOtpRequest, http_req: Request):
    """
    Step 2 of signup. Verifies OTP, creates the client_id record, mints
    a fresh api_key (returned ONCE), and issues a JWT.

    Anti-abuse:
      • OTP attempts cap (per-row, in OTP store)
      • Per-email sliding-window lockout via audit_log — catches the
        OTP-store loophole (attacker requests fresh OTP after each
        max-attempts, repeats indefinitely). Same threshold/window as
        /auth/login/verify so behaviour stays consistent.
      • Daily new-account cap per /24 subnet (assert before insert,
        record after success)
    """
    from src.auth.email_normalize import canonical_email

    _assert_otp_verify_rate_ok(http_req)

    email = _validate_email_or_400(req.email)
    try:
        canonical = canonical_email(email)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid email format")

    _audit_ip = client_ip(http_req)
    _audit_ua = http_req.headers.get("user-agent")

    # Per-email brute-force lockout — shares the audit-window with
    # login_verify so an attacker can't dodge the cap by alternating
    # /auth/login/verify and /auth/signup/verify against the same email.
    _assert_not_locked_out(
        canonical, http_req,
        event_type=EVT_OTP_VERIFY, event_subtype="locked_out_signup",
        attempt_label="attempts",
    )

    # Daily cap pre-check — fail fast before touching the DB.
    ip = client_ip(http_req) or "0.0.0.0"
    try:
        assert_signup_allowed(ip)
    except RateLimited as e:
        raise HTTPException(
            status_code=429, detail=str(e),
            headers={"Retry-After": str(e.retry_after_sec)},
        )

    from src.auth.otp import verify_otp
    from src.auth.otp_store import get_otp_store, otp_max_attempts, PURPOSE_SIGNUP

    store  = get_otp_store()
    # OTP rows were stored under canonical email by /auth/signup
    record     = store.find_active(canonical, PURPOSE_SIGNUP)
    cid_record = store.find_active(canonical, f"{PURPOSE_SIGNUP}:cid")
    display_record = store.find_active(canonical, f"{PURPOSE_SIGNUP}:display")

    # Always run verify_otp (constant-time on miss)
    is_match = verify_otp(req.code, record.code_hash if record else None)
    if not is_match:
        if record is not None:
            new_attempts = store.increment_attempts(record.id)
            if new_attempts >= otp_max_attempts():
                store.mark_used(record.id)
        record_event(
            event_type=EVT_OTP_VERIFY, event_subtype="failure",
            actor_email=canonical, ip=_audit_ip, user_agent=_audit_ua,
            success=False,
            metadata={"reason": "invalid_otp", "purpose": "signup"},
        )
        raise HTTPException(status_code=401, detail="Invalid or expired code")

    if record is None or cid_record is None:
        record_event(
            event_type=EVT_OTP_VERIFY, event_subtype="failure",
            actor_email=canonical, ip=_audit_ip, user_agent=_audit_ua,
            success=False,
            metadata={"reason": "expired_or_no_otp", "purpose": "signup"},
        )
        raise HTTPException(status_code=401, detail="Invalid or expired code")
    if record.attempts >= otp_max_attempts():
        record_event(
            event_type=EVT_OTP_VERIFY, event_subtype="failure",
            actor_email=canonical, ip=_audit_ip, user_agent=_audit_ua,
            success=False,
            metadata={"reason": "max_attempts", "purpose": "signup"},
        )
        raise HTTPException(status_code=429, detail="Too many attempts; request a new code")

    # R11-M2: the code is valid — now atomically CLAIM this OTP row before
    # any state mutation. Of two concurrent correct-code requests exactly
    # one wins the claim and proceeds; the other gets False and is rejected.
    # This closes the find_active → verify → mark_used race (single-use
    # bypass) while preserving the per-row attempt counter above. (Login
    # uses claim_active; signup needs claim-by-id because it must keep the
    # attempt counter + its 3-record stash.)
    if not store.claim_by_id(record.id):
        record_event(
            event_type=EVT_OTP_VERIFY, event_subtype="failure",
            actor_email=canonical, ip=_audit_ip, user_agent=_audit_ua,
            success=False,
            metadata={"reason": "already_claimed", "purpose": "signup"},
        )
        raise HTTPException(status_code=401, detail="Invalid or expired code")

    desired_cid = cid_record.code_hash
    display_email = (display_record.code_hash if display_record else email)

    # Re-check uniqueness (race window of ~10 min between /signup and verify)
    registry = get_registry()
    if registry.get_by_email(canonical) is not None or registry.get(desired_cid) is not None:
        raise HTTPException(status_code=409, detail="Email or client_id already in use")

    from src.auth.api_keys import generate_api_key, hash_api_key
    from datetime import datetime as _dt, timezone as _tz

    api_key = generate_api_key()
    storage = ClientStorage(desired_cid)
    record_ = ClientRecord(
        client_id=desired_cid,
        config={},
        storage_path=storage.path(""),
        plan=Plan.FREE.value,
        api_key_hash=hash_api_key(api_key),
        email=display_email,
        email_canonical=canonical,
        email_verified_at=_dt.now(_tz.utc).isoformat(),
    )
    # Postgres UNIQUE indexes are the actual source of truth for
    # uniqueness — the pre-checks above are best-effort. A concurrent
    # signup with the same email_canonical or client_id will surface
    # here as ClientAlreadyExists; we map to the same generic 409 the
    # pre-check uses, so the response is concurrency-invariant.
    from src.clients.registry import ClientAlreadyExists
    try:
        registry.register(record_)
    except ClientAlreadyExists:
        raise HTTPException(status_code=409, detail="Email or client_id already in use")

    # record.id was already claimed atomically above (R11-M2); just clear
    # the auxiliary cid/display stashes (gated by that claim → no race).
    store.mark_used(cid_record.id)
    if display_record:
        store.mark_used(display_record.id)

    # Bump the daily success counter for this /24 subnet.
    try:
        record_signup_success(ip)
    except RateLimited:
        # Rare: limit was tripped between assert_signup_allowed and now
        # by a concurrent signup. The account is created — we don't
        # roll it back (audit-ugly), but we log loudly.
        logger.warning("signup: post-create rate-limit hit for ip=%s", ip)

    record_event(
        event_type=EVT_SIGNUP,
        client_id=desired_cid, actor_email=canonical,
        ip=ip, user_agent=http_req.headers.get("user-agent"),
        metadata={"plan": Plan.FREE.value, "method": "email_otp"},
    )

    token = create_access_token(client_id=desired_cid, roles=["forecast"])
    return SignupVerifyResponse(
        client_id=desired_cid,
        plan=Plan.FREE.value,
        model_name=get_plan_spec(Plan.FREE).model_display_name,
        api_key=api_key,
        access_token=token,
    )


@router.post("/auth/login", response_model=SignupAcceptedResponse)
async def auth_login_email(req: LoginEmailRequest, http_req: Request):
    """
    Email-OTP login (claude.ai-style). Step 1: send OTP if user exists.

    Important: we ALWAYS return 202 with the same response shape, even
    if the email isn't registered. This prevents email enumeration via
    "user exists / doesn't exist" responses. Real users get an email,
    fake users don't — but the API doesn't tell you which.

    Audit R4-3 — per-IP rate-limit. Captcha alone is insufficient against
    solver farms ($1/1k commodity). Without this cap, an attacker could
    flood Resend quota, spam real users' mailboxes, and probe registration
    via response timing.
    """
    try:
        check_login_attempt(client_ip(http_req))
    except RateLimited as e:
        raise HTTPException(
            status_code=429, detail=str(e),
            headers={"Retry-After": str(e.retry_after_sec or 60)},
        )

    _verify_captcha_or_400(req.captcha_token)
    email = _validate_email_or_400(req.email)

    # Canonical form is the key to ALL OTP storage and lookups — without
    # it, `vasya@gmail.com` and `vasya+spam@gmail.com` become different
    # accounts at the OTP layer, even though they are the same to the
    # registry. The /auth/signup path canonicalizes; this one MUST too.
    from src.auth.email_normalize import canonical_email
    try:
        canonical = canonical_email(email)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid email format")

    from src.auth.otp import generate_otp, hash_otp
    from src.auth.otp_store import get_otp_store, otp_ttl, PURPOSE_LOGIN
    from src.auth.email_sender import get_email_sender, render_otp_email, EmailDeliveryError

    ttl_min = int(otp_ttl().total_seconds() / 60)
    registry = get_registry()
    record = registry.get_by_email(canonical)

    # Only actually issue + send when the user exists AND is verified.
    if record is not None and record.email_verified_at is not None:
        store = get_otp_store()
        code  = generate_otp()
        store.create(email=canonical, purpose=PURPOSE_LOGIN, code_hash=hash_otp(code))
        subject, body, html = render_otp_email(code=code, purpose=PURPOSE_LOGIN, ttl_minutes=ttl_min)
        try:
            get_email_sender().send(to=email, subject=subject, body=body, html=html)
        except EmailDeliveryError as e:
            # Hash email — same enumeration-defence pattern as signup.
            logger.error("login email failed for email_hash=%s: %s", _email_hash(email), e)
            # Still return 202 — don't leak that the user exists by
            # returning 503 selectively. Better the user sees "email
            # never arrived" once than enumeration becomes possible.

    # Audit: only the user-facing intent — we don't audit the "user does
    # not exist" branch to keep the enumeration story consistent.
    record_event(
        event_type=EVT_OTP_SEND,
        actor_email=canonical,
        client_id=record.client_id if record else None,
        ip=client_ip(http_req),
        user_agent=http_req.headers.get("user-agent"),
        success=record is not None and record.email_verified_at is not None,
        metadata={"purpose": "login"},
    )

    return SignupAcceptedResponse(
        status="otp_sent",
        email=email,
        expires_in_minutes=ttl_min,
    )


class LoginVerifyResponse(BaseModel):
    client_id:    str
    access_token: str
    token_type:   str = "bearer"


@router.post("/auth/login/verify", response_model=LoginVerifyResponse)
async def auth_login_verify(req: VerifyOtpRequest, http_req: Request):
    """
    Step 2 of email login. Verifies OTP, returns a JWT under the client_id
    bound to that email. No api_key here — login by email is meant to be
    used INSTEAD of the api_key path, not in addition.

    Brute-force defence: counts failed attempts for this canonical email
    over a sliding 15-minute window via audit_log; locks out at LOCKOUT_THRESHOLD.
    The OTP store has its own per-row attempt cap, but it only protects a
    SINGLE OTP — a credential-stuffer can issue a fresh OTP, fail max-attempts,
    issue another, repeat. The audit_log window catches that pattern.
    """
    _assert_otp_verify_rate_ok(http_req)

    email = _validate_email_or_400(req.email)
    from src.auth.email_normalize import canonical_email
    try:
        canonical = canonical_email(email)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid email format")

    _audit_ip = client_ip(http_req)
    _audit_ua = http_req.headers.get("user-agent")

    # ── Brute-force lockout (fail-closed when enabled, fail-open if
    # the audit DB is unavailable: recent_failed_logins returns 0). ──
    _assert_not_locked_out(
        canonical, http_req,
        event_type=EVT_LOGIN, event_subtype="locked_out",
        attempt_label="login attempts",
    )

    from src.auth.otp import verify_otp
    from src.auth.otp_store import get_otp_store, otp_max_attempts, PURPOSE_LOGIN

    store  = get_otp_store()
    # OTP rows for this purpose were stored under canonical email by /auth/login.
    #
    # `claim_active` atomically picks the latest active OTP and stamps
    # `used_at = NOW()` in one transaction (SELECT ... FOR UPDATE SKIP
    # LOCKED on Postgres). This closes the audit R2-3 race where two
    # parallel /auth/login/verify requests could both pass with the
    # same code before either had marked it used. After this call the
    # row is single-use regardless of how the verify_otp / lockout
    # branches resolve.
    record = store.claim_active(canonical, PURPOSE_LOGIN)
    is_match = verify_otp(req.code, record.code_hash if record else None)

    if not is_match:
        if record is not None:
            # The row is already used (claim_active stamped it).
            # increment_attempts is a no-op in terms of replay safety
            # but keeps the audit counter accurate; cap is enforced by
            # the existing audit-log lockout (recent_failed_logins).
            store.increment_attempts(record.id)
        record_event(
            event_type=EVT_LOGIN, event_subtype="failure",
            actor_email=canonical, ip=_audit_ip, user_agent=_audit_ua,
            success=False,
            metadata={"reason": "invalid_otp"},
        )
        raise HTTPException(status_code=401, detail="Invalid or expired code")

    if record is None or record.attempts >= otp_max_attempts():
        record_event(
            event_type=EVT_LOGIN, event_subtype="failure",
            actor_email=canonical, ip=_audit_ip, user_agent=_audit_ua,
            success=False,
            metadata={"reason": "expired_or_no_otp"},
        )
        raise HTTPException(status_code=401, detail="Invalid or expired code")

    registry = get_registry()
    client = registry.get_by_email(canonical)
    if client is None or client.email_verified_at is None:
        # The email had a valid OTP but the user is gone (deleted between
        # /auth/login and /auth/login/verify). Don't leak this — keep the
        # response indistinguishable from a wrong-code path. Row is
        # already used by claim_active; no extra cleanup needed.
        record_event(
            event_type=EVT_LOGIN, event_subtype="failure",
            actor_email=canonical, ip=_audit_ip, user_agent=_audit_ua,
            success=False,
            metadata={"reason": "client_gone"},
        )
        raise HTTPException(status_code=401, detail="Invalid or expired code")
    # Row already marked used by claim_active — no extra mark_used here.

    # AUD-5 (#357): OTP proves email ownership, not that the account is
    # open — a suspended/closed client could re-enter here for the whole
    # 30-day retention window. Runs AFTER OTP verification, so the 403
    # discloses account state only to someone holding the mailbox (same
    # post-credential disclosure /auth/token has made since ADM-10).
    assert_account_open(client)

    # Email-login → forecast role. Admin role is reserved for the
    # ADMIN_API_KEY path; you don't get to be admin via email-OTP.
    token = create_access_token(client_id=client.client_id, roles=["forecast"])
    record_event(
        event_type=EVT_LOGIN, event_subtype="success",
        client_id=client.client_id, actor_email=canonical,
        ip=_audit_ip, user_agent=_audit_ua, success=True,
        metadata={"method": "email_otp"},
    )
    return LoginVerifyResponse(
        client_id=client.client_id,
        access_token=token,
    )


@router.post("/auth/logout", status_code=204)
async def auth_logout(auth: AuthContext = Depends(get_current_client)):
    """
    Revoke the current JWT. Future requests bearing this token will
    receive 401, even though the token's natural `exp` may still be
    in the future.

    Implementation: per-token revocation via the `jti` claim
    (UUID4 hex, set by `create_access_token`). The revocation entry
    is stored in Redis with TTL = JWT_EXPIRE_MIN + slack, so storage
    is bounded — anything older than that would have expired
    naturally anyway.

    No state on the client: the frontend should delete its stored
    token after a 204. If the user is on an API-key auth method
    (no JWT, no jti) the call is a no-op — there's nothing to revoke
    because API keys are revoked via /clients/{id}/api-key/rotate.
    """
    from src.auth.jwt_auth import revoke_token
    jti = auth.jti
    if jti:
        revoke_token(jti)
        logger.info("revoked jti=%s for client=%s", jti, auth.client_id)


# ══════════════════════════════════════════════════════════════════
# OAuth (Phase 8) — Google, Yandex
#
# Each provider has 3 endpoints under /auth/oauth/{provider}:
#
#   GET  /auth/oauth/providers
#       Public list of enabled providers. Frontend reads this to decide
#       which buttons to render.
#
#   GET  /auth/oauth/{provider}/start
#       Issues a signed state token + nonce cookie, then 302's to the
#       provider's authorize URL.
#
#   GET  /auth/oauth/{provider}/callback?code=...&state=...
#       1. Validates state against the nonce cookie.
#       2. Exchanges code for tokens.
#       3. Resolves identity (id_token claims for Google, /info for Yandex).
#       4. Looks up or creates a client_id.
#       5. Issues a JWT and 302's to the frontend's /oauth/return.
#
# Account linking strategy:
#   • If `(provider, subject)` already maps to a client_id → just login.
#   • Else if the provider's verified email matches an existing client's
#     email_canonical → LINK (set oauth_provider/subject), then login.
#   • Else → AUTO-CREATE a new client_id derived from the email's local
#     part, plus suffix on collision. New api_key is generated and
#     passed to the frontend as ?api_key=… ONCE.
# ══════════════════════════════════════════════════════════════════

class OAuthProviderInfo(BaseModel):
    id:           str
    display_name: str
    start_url:    str


@router.get("/auth/oauth/providers")
async def list_oauth_providers():
    """Frontend reads this to decide which OAuth buttons to render."""
    from src.auth.oauth.registry import public_provider_info
    return {"providers": public_provider_info()}


def _frontend_return_url() -> str:
    """
    Where to bounce the user after a successful (or failed) OAuth round-trip.

    Open-redirect defence: the URL must:
      • Have an http/https scheme (no javascript:, data:, etc.)
      • Have an origin matching one of FRONTEND_ORIGINS — the same
        list CORS uses. This makes it impossible to point the URL at
        attacker.com by env-poisoning, even if an attacker controlled
        Lockbox/.env.
      • End in a known path (`/oauth/return`) — extra defense in depth
        so we can't be redirected to /admin or similar.

    Misconfiguration (URL not in FRONTEND_ORIGINS) → 500 instead of
    silently following a bad URL.
    """
    raw = os.environ.get(
        "FRONTEND_OAUTH_RETURN_URL",
        "http://localhost:5173/oauth/return",
    )
    from urllib.parse import urlparse
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise RuntimeError(f"FRONTEND_OAUTH_RETURN_URL has unsafe scheme: {parsed.scheme!r}")
    if not parsed.netloc:
        raise RuntimeError(f"FRONTEND_OAUTH_RETURN_URL missing host: {raw!r}")
    # Must match one of the trusted frontend origins
    origin = f"{parsed.scheme}://{parsed.netloc}"
    allowed = parse_frontend_origins()
    if origin not in allowed:
        raise RuntimeError(
            f"FRONTEND_OAUTH_RETURN_URL origin {origin!r} is not in "
            f"FRONTEND_ORIGINS — refusing open-redirect risk"
        )
    if parsed.path != "/oauth/return":
        raise RuntimeError(
            f"FRONTEND_OAUTH_RETURN_URL must end in /oauth/return, got {parsed.path!r}"
        )
    return raw


def _generate_unique_client_id(seed_email: str, registry) -> str:
    """
    Derive a client_id from an email's local part. Sanitize to our
    allowed alphabet [a-z0-9-], strip leading/trailing dashes, ensure
    length ≥ 3, and append a numeric suffix on collision.
    """
    local = seed_email.split("@", 1)[0].lower()
    base  = "".join(c if (c.isalnum() or c == "-") else "-" for c in local)
    base  = base.strip("-") or "user"
    while "--" in base:
        base = base.replace("--", "-")
    if len(base) < 3:
        base = (base + "user")[:8]
    if len(base) > 50:
        base = base[:50]

    candidate = base
    suffix = 2
    while registry.get(candidate) is not None:
        candidate = f"{base}-{suffix}"
        suffix += 1
        if suffix > 999:
            # Pathological collision — fall back to random.
            import secrets as _sec
            candidate = f"{base}-{_sec.token_hex(3)}"
            break
    return candidate


@router.get("/auth/oauth/{provider}/start")
async def oauth_start(provider: str):
    """
    Build the provider's authorize URL with a signed state token + nonce
    cookie, then 302 the browser there.
    """
    from src.auth.oauth.registry import get_provider
    from src.auth.oauth.state import issue_state, COOKIE_NAME

    p = get_provider(provider)
    if p is None:
        raise HTTPException(404, detail=f"OAuth provider {provider!r} not enabled")

    state, nonce = issue_state(provider)
    url = p.authorize_url(state=state)

    response = RedirectResponse(url, status_code=302)
    # SameSite=Lax: lets the cookie come back on the post-callback
    # top-level redirect (which is what we need), but blocks third-party
    # JS-loaded iframe POSTs.
    response.set_cookie(
        COOKIE_NAME, nonce,
        max_age=600, httponly=True, secure=True, samesite="lax",
        path="/auth/oauth",
    )
    return response


@router.get("/auth/oauth/{provider}/callback")
async def oauth_callback(provider: str, request: Request, code: str = "", state: str = ""):
    """
    Validate state, exchange code, resolve identity, link/create client,
    redirect to the frontend with the new JWT and (for new accounts)
    a one-time api_key.
    """
    from src.auth.oauth.registry import get_provider
    from src.auth.oauth.state import verify_state, COOKIE_NAME, StateError
    from src.auth.oauth.google import OAuthError
    from src.auth.email_normalize import canonical_email
    from src.auth.api_keys import generate_api_key, hash_api_key
    from datetime import datetime as _dt, timezone as _tz

    p = get_provider(provider)
    if p is None:
        raise HTTPException(404, detail=f"OAuth provider {provider!r} not enabled")
    if not code:
        raise HTTPException(400, detail="Missing 'code' parameter")
    if not state:
        raise HTTPException(400, detail="Missing 'state' parameter")

    _audit_ip = client_ip(request)
    _audit_ua = request.headers.get("user-agent")

    nonce = request.cookies.get(COOKIE_NAME, "")
    # Explicit non-empty check before HMAC verify. Without this, a
    # crafted state with `nonce=""` would compare equal against an
    # empty cookie via constant-time-compare and succeed — a CSRF
    # bypass if an attacker ever forged the HMAC (e.g. through
    # MODEL_SIGNING_KEY leak elsewhere).
    if not nonce:
        logger.warning("OAuth %s callback without nonce cookie", provider)
        record_event(
            event_type=EVT_OAUTH_CALLBACK, event_subtype="missing_nonce",
            ip=_audit_ip, user_agent=_audit_ua, success=False,
            metadata={"provider": provider},
        )
        raise HTTPException(400, detail="Missing OAuth session cookie")
    try:
        verify_state(state, expected_nonce=nonce, expected_provider=provider)
    except StateError as e:
        logger.warning("OAuth %s state rejected: %s", provider, e)
        # CSRF defence trip — audit this loudly. A burst of these from
        # the same IP/window should fire a Grafana alert (incident
        # response signal, not just a noisy user).
        record_event(
            event_type=EVT_OAUTH_CALLBACK, event_subtype="csrf_state_rejected",
            ip=_audit_ip, user_agent=_audit_ua, success=False,
            metadata={"provider": provider, "reason": str(e)[:200]},
        )
        raise HTTPException(400, detail="Invalid OAuth state (CSRF defence)")

    # ── Step 2 + 3: identity ─────────────────────────────────────────
    try:
        tokens = p.exchange_code(code)
    except OAuthError as e:
        logger.warning("OAuth %s exchange failed: %s", provider, e)
        raise HTTPException(502, detail="Provider token exchange failed")

    # Identity flow differs by provider. The runtime branch on the
    # provider string narrows down to a specific Protocol shape; cast
    # so mypy resolves the right method against OidcProvider or
    # UserinfoProvider rather than the broad common-surface Provider.
    if provider == "google":
        id_token = tokens.get("id_token")
        if not id_token:
            raise HTTPException(502, detail="Google did not return id_token")
        from src.auth.oauth.registry import OidcProvider
        identity = cast(OidcProvider, p).verify_id_token(id_token)
    elif provider == "yandex":
        access = tokens.get("access_token")
        if not access:
            raise HTTPException(502, detail="Yandex did not return access_token")
        from src.auth.oauth.registry import UserinfoProvider
        identity = cast(UserinfoProvider, p).fetch_userinfo(access)
    else:                       # defensive
        raise HTTPException(404, detail=f"Unknown provider {provider}")

    if not identity.email_verified:
        # Defence-in-depth — providers should already gate this, but
        # if a future provider misbehaves we refuse rather than create
        # an account against an unverified email.
        raise HTTPException(403, detail="Provider email is not verified")

    # ── Step 4: account resolution ───────────────────────────────────
    registry = get_registry()

    # 4a. Already linked? Just login.
    record = registry.get_by_oauth(identity.provider, identity.subject)
    is_new_user = False
    plain_api_key: Optional[str] = None

    if record is None:
        # 4b. Link by canonical email if a client with this email exists.
        try:
            canonical = canonical_email(identity.email)
        except ValueError:
            raise HTTPException(422, detail="Provider returned malformed email")
        existing = registry.get_by_email(canonical)

        if existing is not None and existing.email_verified_at is not None:
            # Link only if email was previously verified by us — otherwise
            # an attacker could squat on a client_id by registering with
            # someone else's email and waiting for them to OAuth-in.
            registry.update(
                existing.client_id,
                oauth_provider=identity.provider,
                oauth_subject=identity.subject,
            )
            record = registry.get(existing.client_id)
            logger.info("OAuth: linked %s to existing client %s",
                        identity.provider, existing.client_id)
        else:
            # 4c. Auto-create. Daily-cap pre-check for OAuth signups too.
            ip = client_ip(request) or "0.0.0.0"
            try:
                assert_signup_allowed(ip)
            except RateLimited as e:
                raise HTTPException(429, detail=str(e),
                                    headers={"Retry-After": str(e.retry_after_sec)})

            new_cid = _generate_unique_client_id(identity.email, registry)
            plain_api_key = generate_api_key()
            storage = ClientStorage(new_cid)
            new_record = ClientRecord(
                client_id=new_cid,
                config={},
                storage_path=storage.path(""),
                plan=Plan.FREE.value,
                api_key_hash=hash_api_key(plain_api_key),
                email=identity.email,
                email_canonical=canonical,
                email_verified_at=_dt.now(_tz.utc).isoformat(),
                oauth_provider=identity.provider,
                oauth_subject=identity.subject,
            )
            # Concurrent OAuth callbacks for the same Google/Yandex
            # account hit the UNIQUE(provider, subject) index. Re-look
            # up — likely the OTHER request just created the link, so
            # we should LOG IN that client rather than fail.
            from src.clients.registry import ClientAlreadyExists
            try:
                registry.register(new_record)
            except ClientAlreadyExists as e:
                if e.field == "oauth":
                    record = registry.get_by_oauth(identity.provider, identity.subject)
                    if record is not None:
                        is_new_user = False
                        plain_api_key = None
                        logger.info(
                            "OAuth: concurrent insert race resolved → existing client %s",
                            record.client_id,
                        )
                    else:
                        # Shouldn't happen — UNIQUE was violated but lookup
                        # finds nothing. Treat as transient, ask user to retry.
                        raise HTTPException(503, detail="OAuth registration race; retry")
                else:
                    # email or client_id collision — same generic 409 path.
                    raise HTTPException(status_code=409, detail="Account already exists")
            else:
                # No conflict — fresh client successfully inserted.
                try:
                    record_signup_success(ip)
                except RateLimited:
                    logger.warning("OAuth: post-create rate-limit tripped for ip=%s", ip)
                record = new_record
                is_new_user = True
                logger.info("OAuth: new client %s via %s", new_cid, identity.provider)

    if record is None:
        # Never reached, but appeases the type checker.
        raise HTTPException(500, detail="OAuth post-state inconsistency")

    # Audit R3-8 — defence-in-depth re-check. All record-resolution
    # branches above SHOULD have email_verified_at set:
    #   • already-linked OAuth → set when the link was created
    #   • email-link path      → gated by line 1479 check
    #   • fresh client         → stamped with now() at insert
    #   • race-resolution      → re-lookup of one of the above
    # But if a future code path lands an OAuth-linked record with
    # email_verified_at NULL (DB hand-edit, migration bug, etc), we
    # refuse to mint a JWT rather than silently grant access.
    if record.email_verified_at is None:
        logger.warning(
            "OAuth: refusing JWT for unverified record client=%s provider=%s",
            record.client_id, identity.provider,
        )
        raise HTTPException(403, detail="Email verification missing")

    # AUD-5 (#357): an existing OAuth-linked (or email-linked) record may
    # be suspended or closed — the provider only vouches for identity,
    # not account state. Same shared gate as every other issuance path.
    assert_account_open(record)

    # ── Step 5: redirect to frontend with JWT ────────────────────────
    token = create_access_token(client_id=record.client_id, roles=["forecast"])

    _audit_ip = client_ip(request)
    _audit_ua = request.headers.get("user-agent")
    record_event(
        event_type=EVT_OAUTH_CALLBACK, event_subtype="success",
        client_id=record.client_id, actor_email=record.email_canonical or record.email,
        ip=_audit_ip, user_agent=_audit_ua,
        metadata={"provider": identity.provider, "is_new_user": is_new_user},
    )
    if is_new_user:
        record_event(
            event_type=EVT_SIGNUP,
            client_id=record.client_id, actor_email=record.email_canonical or record.email,
            ip=_audit_ip, user_agent=_audit_ua,
            metadata={"plan": Plan.FREE.value, "method": f"oauth:{identity.provider}"},
        )

    return_url = _frontend_return_url()
    qs = {
        "token":       token,
        "client_id":   record.client_id,
        "new_user":    "1" if is_new_user else "0",
    }
    if plain_api_key:
        # Pass the api_key plaintext ONCE to the frontend so it can
        # show the reveal screen. After this redirect, the only place
        # it lives is in the user's clipboard / password manager.
        qs["api_key"] = plain_api_key

    import urllib.parse as _up
    # R11-L1: deliver the token / api_key in the URL FRAGMENT (#…), not the
    # query string (?…). A fragment is never sent to the server (stays out
    # of nginx/access logs and the Referer header on the next request) — the
    # query string would leak the JWT + api_key into history, logs and any
    # third-party request the return page makes. The frontend reads
    # window.location.hash and immediately history.replaceState()-scrubs it.
    final_url = f"{return_url}#{_up.urlencode(qs)}"

    response = RedirectResponse(final_url, status_code=302)
    # Defence-in-depth: a misbehaving proxy or browser cache must not
    # remember the JWT/api_key in the URL. 302 isn't cacheable by
    # default but we say it explicitly.
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    # Drop the nonce cookie now that we're done with it.
    response.delete_cookie(COOKIE_NAME, path="/auth/oauth")
    return response
