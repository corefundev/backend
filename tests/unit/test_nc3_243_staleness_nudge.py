"""
NC-3 (#243) — staleness nudges to the USER: 45d/90d thresholds, dedup-driven
push idempotency, 30-day activity gating, suspended skipped, promoted-run
age semantics (#248/#268). The inbox dedup insert is the ONLY push trigger —
a daily re-run never double-pushes.
"""
from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import src.notifications.staleness_nudge as sn


def _client(age, login=5.0, suspended=False, cid="acme", epoch=1700000000):
    return {"client_id": cid, "email": f"{cid}@x", "suspended": suspended,
            "age_days": age, "model_epoch": epoch, "last_login_days": login}


def _wire(monkeypatch, clients, fresh=True):
    created, pushed = [], []
    monkeypatch.setattr(sn, "_stale_clients", lambda: clients)
    import src.storage.notifications as ns
    monkeypatch.setattr(ns, "get_notifications_registry", lambda: SimpleNamespace(
        create=lambda cid, **kw: created.append((cid, kw["dedup_key"])) or fresh))
    monkeypatch.setattr(sn, "_push", lambda cid, email, t, b: pushed.append(cid) or True)
    return created, pushed


def test_45d_active_gets_inbox_and_push(monkeypatch):
    created, pushed = _wire(monkeypatch, [_client(50)])
    s = sn.run_staleness_nudge()
    assert created == [("acme", "model_stale_45:1700000000")] and pushed == ["acme"]
    assert s["emitted"] == 1 and s["pushed"] == 1


def test_90d_escalation_key(monkeypatch):
    created, _ = _wire(monkeypatch, [_client(120)])
    sn.run_staleness_nudge()
    assert created == [("acme", "model_stale_90:1700000000")]


def test_dedup_suppresses_push_on_rerun(monkeypatch):
    created, pushed = _wire(monkeypatch, [_client(50)], fresh=False)
    s = sn.run_staleness_nudge()
    assert created and pushed == []                 # insert attempted, no push
    assert s["emitted"] == 0 and s["pushed"] == 0


def test_inactive_gets_silent_inbox_only(monkeypatch):
    created, pushed = _wire(monkeypatch, [_client(50, login=40.0)])
    s = sn.run_staleness_nudge()
    assert created and pushed == []
    assert s["inactive_silent"] == 1


def test_never_logged_in_counts_inactive(monkeypatch):
    _, pushed = _wire(monkeypatch, [_client(50, login=None)])
    sn.run_staleness_nudge()
    assert pushed == []


def test_suspended_skipped_entirely(monkeypatch):
    created, pushed = _wire(monkeypatch, [_client(50, suspended=True)])
    s = sn.run_staleness_nudge()
    assert created == [] and pushed == []
    assert s["suspended_skipped"] == 1


def test_fresh_model_untouched(monkeypatch):
    created, _ = _wire(monkeypatch, [_client(10)])
    s = sn.run_staleness_nudge()
    assert created == [] and s["stale"] == 0


# ── wiring pins ───────────────────────────────────────────────────────────────

def test_age_uses_promoted_run_semantics():
    src = inspect.getsource(sn._stale_clients)
    assert "model_path IS NOT NULL" in src, "#248/#268: blocked runs must not look fresh"
    assert "max(ts)" in src and "admin_token_issued" in src


def test_cron_and_endpoint_wired():
    dk = Path("docker/Dockerfile.backup").read_text()
    assert "staleness_nudge.sh" in dk
    sh = Path("scripts/staleness_nudge.sh").read_text()
    assert "/internal/staleness-nudge" in sh and "ADMIN_API_KEY" in sh
    import src.api.routers.ops as ops
    assert 'auth.require_role("admin")' in inspect.getsource(ops.internal_staleness_nudge)


# ── AUD-3 (#355): dedup is EPISODE-scoped, not lifetime ──────────────────────

def test_355_retrain_then_restale_nudges_again(monkeypatch):
    """The lifetime-key bug: nudge → retrain (fresh model) → months later the
    NEW model goes stale → the nudge must fire again. The key carries the
    promoted model's epoch, so a new episode gets a new key even though the
    old inbox row still exists (create() only collides on identical keys)."""
    seen_keys: set[str] = set()
    created, pushed = [], []
    import src.storage.notifications as ns

    def _create(cid, **kw):
        key = kw["dedup_key"]
        if key in seen_keys:
            return False
        seen_keys.add(key)
        created.append((cid, key))
        return True

    monkeypatch.setattr(ns, "get_notifications_registry",
                        lambda: SimpleNamespace(create=_create))
    monkeypatch.setattr(sn, "_push", lambda cid, email, t, b: pushed.append(cid) or True)

    # Episode 1: model from epoch 1000 is 50d old → nudged
    monkeypatch.setattr(sn, "_stale_clients", lambda: [_client(50, epoch=1000)])
    sn.run_staleness_nudge()
    # cron rerun same episode → idempotent, no second push
    sn.run_staleness_nudge()
    assert pushed == ["acme"]

    # Client retrains (epoch 2000, fresh) → below threshold, nothing fires
    monkeypatch.setattr(sn, "_stale_clients", lambda: [_client(3, epoch=2000)])
    sn.run_staleness_nudge()
    assert pushed == ["acme"]

    # Episode 2: the NEW model goes stale → nudge fires AGAIN
    monkeypatch.setattr(sn, "_stale_clients", lambda: [_client(50, epoch=2000)])
    sn.run_staleness_nudge()
    assert pushed == ["acme", "acme"], "second episode must re-arm the nudge"
    assert created == [("acme", "model_stale_45:1000"),
                       ("acme", "model_stale_45:2000")]


def test_355_sql_exposes_the_model_epoch():
    src = inspect.getsource(sn._stale_clients)
    assert "model_epoch" in src and "max(ended_at)" in src
