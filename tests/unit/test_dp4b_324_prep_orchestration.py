"""
DP-4b (#324): prep orchestration — sniff decision, confirmed-mapping storage,
and the registry field plumbing that backs «Подготовка данных».
"""
from __future__ import annotations

import types

import pytest

from src.storage import upload_registry as ur
from src.storage import zones as z
from src.storage.sandbox import SandboxResult
from src.storage.upload_pipeline import sniff_needs_mapping


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


# ── registry field plumbing ───────────────────────────────────────────────────

def test_update_fields_roundtrip_no_status_change():
    reg = ur.get_upload_registry()
    _mk(reg)
    reg.update_fields("u1", sniff_report={"headers": ["a"]},
                      confirmed_mapping={"date": "a"})
    got = reg.get("u1")
    assert got.sniff_report == {"headers": ["a"]}
    assert got.confirmed_mapping == {"date": "a"}
    assert got.status == ur.SCANNED_CLEAN            # unchanged — not a transition


def test_update_fields_rejects_non_prep_column():
    reg = ur.get_upload_registry()
    _mk(reg)
    with pytest.raises(ValueError):
        reg.update_fields("u1", status="processed")


# ── sniff decision ────────────────────────────────────────────────────────────

def test_sniff_autoconfirms_canonical(monkeypatch):
    _patch_quarantine(monkeypatch)
    reg = ur.get_upload_registry()
    rec = _mk(reg)
    report = {"format": "csv", "headers": ["date", "sku", "sales"], "sample_rows": []}
    needs = sniff_needs_mapping(rec, sandbox=_FakeSandbox(report))
    assert needs is False                            # zero-click
    got = reg.get("u1")
    assert got.confirmed_mapping == {"date": "date", "sku": "sku", "sales": "sales"}
    assert got.sniff_report["headers"] == ["date", "sku", "sales"]
    assert got.mapping_proposal["auto_confirmable"] is True


def test_sniff_needs_mapping_when_not_high_confidence(monkeypatch):
    _patch_quarantine(monkeypatch)
    reg = ur.get_upload_registry()
    rec = _mk(reg)
    # sales via a medium (token) match → not auto-confirmable
    report = {"format": "csv", "headers": ["Дата продажи", "Артикул", "Кол-во, шт"],
              "sample_rows": []}
    needs = sniff_needs_mapping(rec, sandbox=_FakeSandbox(report))
    assert needs is True                             # park in NEEDS_MAPPING
    got = reg.get("u1")
    assert got.confirmed_mapping is None             # user must confirm
    assert got.mapping_proposal["mapping"]["sales"] == "Кол-во, шт"


def test_sniff_failsafe_on_unusable_report(monkeypatch):
    _patch_quarantine(monkeypatch)
    reg = ur.get_upload_registry()
    rec = _mk(reg)
    needs = sniff_needs_mapping(rec, sandbox=_FakeSandbox(None, ok=False))
    assert needs is False                            # broken sniff never blocks


# ── confirm ───────────────────────────────────────────────────────────────────

def test_confirm_prep_stores_mapping_and_enqueues(monkeypatch):
    reg = ur.get_upload_registry()
    _mk(reg, status=ur.NEEDS_MAPPING)
    from src.pipeline import upload_workers as uw
    monkeypatch.setattr(uw, "enqueue_process", lambda upload_id, client_id: "job1")
    mapping = {"date": "Дата продажи", "sku": "Артикул", "sales": "Кол-во, шт"}
    jid = uw.confirm_prep("u1", mapping=mapping)
    assert jid == "job1"
    assert reg.get("u1").confirmed_mapping == mapping


def test_confirm_prep_noop_when_not_needs_mapping(monkeypatch):
    reg = ur.get_upload_registry()
    _mk(reg, status=ur.SCANNED_CLEAN)               # not awaiting mapping
    from src.pipeline import upload_workers as uw
    called = []
    monkeypatch.setattr(uw, "enqueue_process",
                        lambda upload_id, client_id: called.append(upload_id))
    assert uw.confirm_prep("u1", mapping={"date": "d"}) is None
    assert called == []
