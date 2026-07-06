"""
NC-5 (#250) — inbox coverage: upload terminal states + quota friction.

Uploads emit at the WORKER choke points (_scan_job/_process_job) on
terminal states only, dedup by upload_id (RQ retries safe). Quota emits are
day-deduped per kind — a hammering client gets ONE row per day, not a
flood; every emit is best-effort and precedes the HTTP raise. Taxonomy is
the single source in storage.notifications; unknown types emit loudly
(forward-compat during rolling deploys).
"""
from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import src.pipeline.upload_workers as uw


def _rec(status, **kw):
    base = dict(upload_id="u1", client_id="acme", filename="f.csv",
                row_count=100, sku_count=5, scan_result=None,
                error_message=None)
    base.update(kw)
    return SimpleNamespace(status=status, **base)


def _capture(monkeypatch):
    calls = []
    import src.storage.notifications as ns
    monkeypatch.setattr(ns, "emit_notification",
                        lambda cid, **kw: calls.append((cid, kw)))
    return calls


def test_processed_emits_success(monkeypatch):
    calls = _capture(monkeypatch)
    from src.storage import upload_registry as ur
    uw._emit_upload_inbox(_rec(ur.PROCESSED))
    (cid, kw), = calls
    assert kw["type"] == "upload_processed" and kw["severity"] == "success"
    assert kw["dedup_key"] == "upload:u1"
    assert "100 строк" in kw["body"]


def test_infected_is_generic_no_malware_education(monkeypatch):
    calls = _capture(monkeypatch)
    from src.storage import upload_registry as ur
    uw._emit_upload_inbox(_rec(ur.INFECTED, scan_result="Eicar-Test-Signature"))
    (_, kw), = calls
    assert kw["type"] == "upload_failed"
    assert "Eicar" not in kw["body"]                  # verdict stays internal


def test_processing_fail_carries_trimmed_reason(monkeypatch):
    calls = _capture(monkeypatch)
    from src.storage import upload_registry as ur
    uw._emit_upload_inbox(_rec(ur.PROCESSING_FAIL, error_message="x" * 500))
    (_, kw), = calls
    assert len(kw["body"]) < 260


def test_non_terminal_states_are_silent(monkeypatch):
    calls = _capture(monkeypatch)
    from src.storage import upload_registry as ur
    uw._emit_upload_inbox(_rec(ur.SCANNED_CLEAN))
    uw._emit_upload_inbox(_rec(ur.PROCESSING))
    assert calls == []


def test_emit_failure_never_breaks_the_job(monkeypatch):
    import src.storage.notifications as ns
    monkeypatch.setattr(ns, "emit_notification",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("pg")))
    from src.storage import upload_registry as ur
    uw._emit_upload_inbox(_rec(ur.PROCESSED))          # no raise


def test_jobs_call_the_emitter():
    src = inspect.getsource(uw._scan_job) + inspect.getsource(uw._process_job)
    assert src.count("_emit_upload_inbox(record)") == 2


def test_quota_sites_emit_with_day_dedup():
    tr = Path("src/api/routers/training.py").read_text()
    inf = Path("src/api/routers/inference.py").read_text()
    assert 'dedup_key=f"quota_training_' in tr
    assert 'dedup_key=f"quota_sku_' in tr
    assert 'dedup_key=f"quota_predict_' in inf
    # emits precede the raises
    for srcs, key in ((tr, "quota_training_"), (tr, "quota_sku_"), (inf, "quota_predict_")):
        i_emit = srcs.index(key)
        i_raise = srcs.index("raise HTTPException", i_emit)
        assert i_raise - i_emit < 400


def test_taxonomy_is_single_source():
    from src.storage.notifications import NOTIFICATION_TYPES
    for t in ("upload_processed", "upload_failed", "quota_warning",
              "training_finished", "gate_blocked", "model_stale",
              "announcement"):
        assert t in NOTIFICATION_TYPES
