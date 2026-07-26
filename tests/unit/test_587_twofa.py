"""2FA-1 (#587) slice A: криптоядро + контракты эндпоинтов.

Реестр стабится (Postgres в юнитах нет); криптофункции и TOTP — настоящие
(pyotp детерминирован по секрету+времени).
"""
import pytest
from fastapi import HTTPException

import src.api.routers.twofa as tf
from src.auth.jwt_auth import AuthContext
from src.auth.twofa import (
    TwoFAStatus,
    TwoFAUnavailable,
    _code_hmac,
    decrypt_secret,
    encrypt_secret,
    generate_backup_codes,
    new_totp_secret,
    provisioning_uri,
    verify_totp,
)


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("TWOFA_ENC_KEY", "unit-test-2fa-key-0123456789")


def _auth(cid="c1"):
    return AuthContext(client_id=cid, roles=[])


class _Req:
    headers = {}
    class client:  # noqa: D106 — минимальный Request-стаб
        host = "127.0.0.1"


# ── криптоядро ───────────────────────────────────────────────────────────

def test_secret_roundtrip_and_key_required(monkeypatch):
    s = new_totp_secret()
    assert decrypt_secret(encrypt_secret(s)) == s
    monkeypatch.delenv("TWOFA_ENC_KEY")
    with pytest.raises(TwoFAUnavailable):
        encrypt_secret(s)


def test_totp_verify_real_codes():
    import pyotp
    s = new_totp_secret()
    assert verify_totp(s, pyotp.TOTP(s).now())
    assert not verify_totp(s, "000000")
    assert not verify_totp(s, "12345")      # не 6 цифр
    assert not verify_totp(s, "abcdef")
    assert "otpauth://totp/" in provisioning_uri(s, "user@example.com")
    assert "Sprosly" in provisioning_uri(s, "user@example.com")


def test_backup_codes_shape_and_hmac_normalization():
    codes = generate_backup_codes()
    assert len(codes) == 10 and len(set(codes)) == 10
    for c in codes:
        assert len(c) == 9 and c[4] == "-"
        for ch in c.replace("-", ""):
            assert ch not in "01OIL"        # без похожих символов
    # HMAC нормализует регистр и дефис — пользователь может ввести как угодно
    assert _code_hmac("k7q2-m9x4") == _code_hmac("K7Q2M9X4")


# ── стаб реестра ─────────────────────────────────────────────────────────

class _Reg:
    def __init__(self, status=None, pending=None, secret=None):
        self._status = status or TwoFAStatus(False, False, False, 0)
        self._pending = pending
        self._secret = secret
        self.calls = []

    def status(self, cid): return self._status
    def pending_secret(self, cid): return self._pending
    def totp_secret(self, cid): return self._secret
    def start_totp_enrollment(self, cid, s): self.calls.append(("enroll", s))
    def confirm_totp(self, cid): self.calls.append(("confirm",))
    def disable_totp(self, cid): self.calls.append(("disable",))
    def set_email(self, cid, on): self.calls.append(("email", on))
    def regenerate_backup_codes(self, cid):
        self.calls.append(("regen",))
        return generate_backup_codes()
    def consume_backup_code(self, cid, code): return False


def _wire(monkeypatch, reg):
    monkeypatch.setattr(tf, "_registry_or_503", lambda: reg)
    monkeypatch.setattr(tf, "require_client_access", lambda cid, auth: None)
    monkeypatch.setattr(tf, "_audit", lambda *a, **k: None)


# ── контракты эндпоинтов ─────────────────────────────────────────────────

def test_confirm_without_enroll_is_409(monkeypatch):
    _wire(monkeypatch, _Reg(pending=None))
    monkeypatch.setattr(
        "src.auth.signup_rate_limit.check_otp_verify_attempt", lambda ip: None)
    with pytest.raises(HTTPException) as e:
        tf.twofa_totp_confirm("c1", tf.ConfirmRequest(code="123456"), _Req(), auth=_auth())
    assert e.value.status_code == 409


def test_confirm_wrong_code_403_right_code_enables(monkeypatch):
    import pyotp
    s = new_totp_secret()
    reg = _Reg(pending=s)
    _wire(monkeypatch, reg)
    monkeypatch.setattr(
        "src.auth.signup_rate_limit.check_otp_verify_attempt", lambda ip: None)
    with pytest.raises(HTTPException) as e:
        tf.twofa_totp_confirm("c1", tf.ConfirmRequest(code="000000"), _Req(), auth=_auth())
    assert e.value.status_code == 403
    out = tf.twofa_totp_confirm(
        "c1", tf.ConfirmRequest(code=pyotp.TOTP(s).now()), _Req(), auth=_auth())
    assert out == {"totp_enabled": True}
    assert ("confirm",) in reg.calls


def test_disable_requires_reauth(monkeypatch):
    reg = _Reg(secret=None)
    _wire(monkeypatch, reg)
    with pytest.raises(HTTPException) as e:
        tf.twofa_totp_disable("c1", tf.ReauthRequest(), _Req(), auth=_auth())
    assert e.value.status_code == 403
    assert ("disable",) not in reg.calls


def test_disable_via_live_totp(monkeypatch):
    import pyotp
    s = new_totp_secret()
    reg = _Reg(secret=s)
    _wire(monkeypatch, reg)
    out = tf.twofa_totp_disable(
        "c1", tf.ReauthRequest(code=pyotp.TOTP(s).now()), _Req(), auth=_auth())
    assert out == {"totp_enabled": False}
    assert ("disable",) in reg.calls


def test_email_enable_free_disable_guarded(monkeypatch):
    reg = _Reg()
    _wire(monkeypatch, reg)
    out = tf.twofa_email_toggle(
        "c1", tf.EmailToggleRequest(enabled=True), _Req(), auth=_auth())
    assert out == {"email_enabled": True}
    with pytest.raises(HTTPException) as e:
        tf.twofa_email_toggle(
            "c1", tf.EmailToggleRequest(enabled=False), _Req(), auth=_auth())
    assert e.value.status_code == 403


def test_backup_codes_need_protection_and_reauth(monkeypatch):
    reg = _Reg(status=TwoFAStatus(False, False, False, 0))
    _wire(monkeypatch, reg)
    with pytest.raises(HTTPException) as e:
        tf.twofa_backup_codes("c1", tf.ReauthRequest(), _Req(), auth=_auth())
    assert e.value.status_code == 409     # 2FA не включена вовсе

    import pyotp
    s = new_totp_secret()
    reg = _Reg(status=TwoFAStatus(True, False, False, 0), secret=s)
    _wire(monkeypatch, reg)
    out = tf.twofa_backup_codes(
        "c1", tf.ReauthRequest(code=pyotp.TOTP(s).now()), _Req(), auth=_auth())
    assert out["count"] == 10 and len(out["codes"]) == 10


def test_registry_or_503_fail_closed(monkeypatch):
    monkeypatch.delenv("TWOFA_ENC_KEY")
    monkeypatch.setenv("DATABASE_URL", "postgresql://stub")
    import src.auth.twofa as twofa_mod
    twofa_mod.reset_for_tests()
    with pytest.raises(HTTPException) as e:
        tf._registry_or_503()
    assert e.value.status_code == 503
    twofa_mod.reset_for_tests()
