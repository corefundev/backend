"""
DP-4b (#324): user-triggered «Подготовка данных».

Model: the scan worker ends at SCANNED_CLEAN; the user clicks «Подготовить»,
which runs the prep worker (run_prepare): sniff → SYSTEM auto-mapping (no user
column editing) → parse. Missing required column → fail with a human hint.
Sniff failure → canonical parse (never blocks). Training stays blocked until the
upload is PROCESSED (enforced in the training endpoint).
"""
from __future__ import annotations

import types

import pytest

from src.storage import upload_registry as ur
from src.storage import zones as z
from src.storage import upload_pipeline as up
from src.storage.sandbox import SandboxResult


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("UPLOAD_REGISTRY_PATH", str(tmp_path / "uploads.json"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    ur.reset_registry_for_tests()
    yield


def _mk(reg, status=ur.SCANNED_CLEAN):
    rec = ur.UploadRecord(upload_id="u1", client_id="c1", filename="f.csv",
                          size_bytes=10, sha256="x", status=status)
    reg.create(rec)
    return rec


class _FakeSandbox:
    def __init__(self, report, ok=True):
        self._report, self._ok = report, ok

    def sniff(self, data, filename):
        return SandboxResult(ok=self._ok, exit_code=0 if self._ok else 3,
                             stdout="", stderr="", output_path=None,
                             manifest=self._report if self._ok else None)


def _patch_quarantine(monkeypatch):
    monkeypatch.setattr(z, "get_zone_backend",
                        lambda zone: types.SimpleNamespace(download_bytes=lambda key: b"bytes"))


# ── FSM: NEEDS_MAPPING is gone; scan stops at SCANNED_CLEAN ────────────────────

def test_no_needs_mapping_state():
    assert not hasattr(ur, "NEEDS_MAPPING")
    assert "needs_mapping" not in ur.ALL_STATES


def test_scanned_clean_only_advances_to_processing():
    assert ur.ALLOWED_NEXT[ur.SCANNED_CLEAN] == {ur.PROCESSING}
    ur._assert_transition(ur.SCANNED_CLEAN, ur.PROCESSING)
    with pytest.raises(ur.InvalidTransition):
        ur._assert_transition(ur.SCANNED_CLEAN, ur.PROCESSED)   # can't skip


def test_scan_worker_does_not_auto_prepare():
    # the scan worker must NOT chain into prep — no sniff/prepare CALL there
    # (check call sites, not comments, so wording can mention them)
    import inspect
    from src.pipeline import upload_workers as uw
    src = inspect.getsource(uw._scan_job)
    assert "enqueue_process(" not in src
    assert "enqueue_prepare(" not in src
    assert "run_prepare(" not in src
    assert "sniff_needs_mapping" not in src


# ── registry field plumbing (reused from DP-4b) ───────────────────────────────

def test_update_fields_roundtrip_no_status_change():
    reg = ur.get_upload_registry()
    _mk(reg)
    reg.update_fields("u1", confirmed_mapping={"date": "Дата"})
    got = reg.get("u1")
    assert got.confirmed_mapping == {"date": "Дата"}
    assert got.status == ur.SCANNED_CLEAN

    with pytest.raises(ValueError):
        reg.update_fields("u1", status="processed")


# ── run_prepare: system auto-mapping ──────────────────────────────────────────

def test_run_prepare_auto_maps_then_parses(monkeypatch):
    _patch_quarantine(monkeypatch)
    reg = ur.get_upload_registry()
    _mk(reg)
    report = {"format": "csv", "headers": ["Дата продажи", "Артикул", "Количество"],
              "sample_rows": []}
    seen = {}
    monkeypatch.setattr(up, "run_process",
                        lambda upload_id, sandbox=None: seen.setdefault("called", upload_id))
    up.run_prepare("u1", sandbox=_FakeSandbox(report))
    assert seen["called"] == "u1"                       # parse delegated
    got = reg.get("u1")
    # system auto-mapped RU headers → canonical, no user step
    assert got.confirmed_mapping == {"date": "Дата продажи", "sku": "Артикул",
                                     "sales": "Количество"}


def test_run_prepare_missing_required_fails_with_human_hint(monkeypatch):
    _patch_quarantine(monkeypatch)
    reg = ur.get_upload_registry()
    _mk(reg)
    called = []
    monkeypatch.setattr(up, "run_process",
                        lambda upload_id, sandbox=None: called.append(upload_id))
    # no sales-like column → required 'sales' missing
    report = {"format": "csv", "headers": ["Дата", "Артикул", "Категория"], "sample_rows": []}
    rec = up.run_prepare("u1", sandbox=_FakeSandbox(report))
    assert rec.status == ur.PROCESSING_FAIL
    assert "продажи" in (rec.error_message or "")       # human hint, not a mapping editor
    assert called == []                                 # never reached the parser


def test_run_prepare_sniff_failure_falls_back_to_parse(monkeypatch):
    _patch_quarantine(monkeypatch)
    reg = ur.get_upload_registry()
    _mk(reg)
    called = []
    monkeypatch.setattr(up, "run_process",
                        lambda upload_id, sandbox=None: called.append(upload_id))
    up.run_prepare("u1", sandbox=_FakeSandbox(None, ok=False))
    assert called == ["u1"]                             # canonical parse still runs
    assert reg.get("u1").confirmed_mapping is None       # no mapping stored


def test_run_prepare_skips_when_not_scanned_clean(monkeypatch):
    _patch_quarantine(monkeypatch)
    reg = ur.get_upload_registry()
    _mk(reg, status=ur.PROCESSED)
    called = []
    monkeypatch.setattr(up, "run_process",
                        lambda upload_id, sandbox=None: called.append(upload_id))
    up.run_prepare("u1", sandbox=_FakeSandbox({"headers": ["date"]}))
    assert called == []


# ── enqueue_prepare trigger ───────────────────────────────────────────────────

def test_enqueue_prepare_delegates_to_process_queue(monkeypatch):
    from src.pipeline import upload_workers as uw
    seen = {}
    monkeypatch.setattr(uw, "enqueue_process",
                        lambda upload_id, client_id: seen.update(id=upload_id, c=client_id) or "job1")
    assert uw.enqueue_prepare("u1", client_id="c1") == "job1"
    assert seen == {"id": "u1", "c": "c1"}
