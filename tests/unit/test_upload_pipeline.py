"""
Unit tests for the secure upload pipeline.

Covers:
  • zones.validate_component / safe_join   — path injection guards
  • upload_registry state machine          — transitions, rejections
  • upload_pipeline.accept_upload          — extension/size validation
  • storage.backend HMAC signing           — tamper detection
  • scanner response parsing               — CLEAN / INFECTED / ERROR

Does NOT cover (integration — need clamd + Docker):
  • end-to-end scan via real clamd
  • sandbox container launch

Those live in tests/integration/test_upload_integration.py and are marked
skipif-no-env.
"""
from __future__ import annotations

import io
import os
from pathlib import Path

import pandas as pd
import pytest

from src.storage import zones
from src.storage import backend as sb
from src.storage import upload_registry as ur
from src.storage import upload_pipeline as pipeline
from src.storage.scanner import ScanResult, ScanVerdict, scan_bytes


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Each test gets its own ARTIFACTS_DIR + in-memory registry."""
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("UPLOAD_REGISTRY_PATH", str(tmp_path / "uploads.json"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    ur.reset_registry_for_tests()
    yield


# ── zones.validate_component ──────────────────────────────────────────────────

@pytest.mark.parametrize("value", [
    "acme", "client-123", "ABC_def.csv", "v1.2.3", "a" * 128,
])
def test_validate_component_accepts_safe(value):
    assert zones.validate_component(value) == value


@pytest.mark.parametrize("value", [
    "../etc/passwd", "a/b", "a\\b", "", ".hidden",
    "a" * 129,                    # length cap
    "with space", "../foo",
    "foo/../bar", "/abs/path",
])
def test_validate_component_rejects_dangerous(value):
    with pytest.raises(ValueError):
        zones.validate_component(value)


def test_safe_join():
    assert zones.safe_join("a", "b", "c") == "a/b/c"


def test_safe_join_refuses_traversal():
    with pytest.raises(ValueError):
        zones.safe_join("client", "..", "data.csv")


# ── LocalStorageBackend path traversal ────────────────────────────────────────

def test_local_backend_rejects_absolute_path(tmp_path):
    store = sb.LocalStorageBackend(base_dir=str(tmp_path))
    with pytest.raises(ValueError):
        store.upload_bytes(b"x", "/etc/passwd")


def test_local_backend_rejects_parent_escape(tmp_path):
    store = sb.LocalStorageBackend(base_dir=str(tmp_path))
    with pytest.raises(ValueError):
        store.upload_bytes(b"x", "../outside.bin")


def test_local_backend_allows_nested(tmp_path):
    store = sb.LocalStorageBackend(base_dir=str(tmp_path))
    store.upload_bytes(b"hello", "a/b/c.txt")
    assert store.download_bytes("a/b/c.txt") == b"hello"


# ── HMAC-signed pickle ────────────────────────────────────────────────────────

def test_pickle_hmac_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_SIGNING_KEY", "00" * 32)  # 32 bytes of zeros (hex)
    store = sb.LocalStorageBackend(base_dir=str(tmp_path))
    store.save_pickle({"w": [1, 2, 3]}, "model.pkl")
    obj = store.load_pickle("model.pkl")
    assert obj == {"w": [1, 2, 3]}


def test_pickle_hmac_rejects_tampered(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_SIGNING_KEY", "aabbccddeeff" * 8)
    store = sb.LocalStorageBackend(base_dir=str(tmp_path))
    store.save_pickle({"w": 1}, "m.pkl")

    # Tamper with payload bytes (past header + signature).
    raw = store.download_bytes("m.pkl")
    corrupt = raw[:-2] + (b"\x00\x00" if raw[-2:] != b"\x00\x00" else b"\xff\xff")
    store.upload_bytes(corrupt, "m.pkl")

    with pytest.raises(ValueError, match="tamper"):
        store.load_pickle("m.pkl")


def test_pickle_hmac_rejects_unsigned_when_key_set(tmp_path, monkeypatch):
    """Signed mode: plain pickle without header is refused."""
    import pickle as _p
    monkeypatch.setenv("MODEL_SIGNING_KEY", "deadbeef" * 8)
    store = sb.LocalStorageBackend(base_dir=str(tmp_path))
    # write legacy unsigned blob
    store.upload_bytes(_p.dumps({"a": 1}), "legacy.pkl")
    with pytest.raises(ValueError, match="unsigned"):
        store.load_pickle("legacy.pkl")


# ── upload_registry state machine ─────────────────────────────────────────────

def _fresh_registry():
    return ur.get_upload_registry()


def _mk(upload_id="u1", client_id="acme"):
    return ur.UploadRecord(
        upload_id=upload_id, client_id=client_id,
        filename="data.csv", size_bytes=100, sha256="a" * 64,
    )


def test_registry_state_forward_transitions():
    r = _fresh_registry()
    r.create(_mk())

    rec = r.update_status("u1", ur.SCANNING)
    assert rec.status == ur.SCANNING

    rec = r.update_status("u1", ur.SCANNED_CLEAN, scan_result="clean")
    assert rec.status == ur.SCANNED_CLEAN
    assert rec.scan_result == "clean"

    rec = r.update_status("u1", ur.PROCESSING)
    rec = r.update_status("u1", ur.PROCESSED, processed_key="acme/u1/data.parquet", row_count=42)
    assert rec.status == ur.PROCESSED
    assert rec.processed_key.endswith("data.parquet")
    assert rec.row_count == 42


def test_registry_rejects_backwards_transition():
    r = _fresh_registry()
    r.create(_mk())
    r.update_status("u1", ur.SCANNING)
    r.update_status("u1", ur.SCANNED_CLEAN, scan_result="clean")
    with pytest.raises(ur.InvalidTransition):
        r.update_status("u1", ur.UPLOADED)


def test_registry_rejects_skipping_states():
    r = _fresh_registry()
    r.create(_mk())
    # can't jump uploaded → processed
    with pytest.raises(ur.InvalidTransition):
        r.update_status("u1", ur.PROCESSED, processed_key="x")


def test_registry_rejects_unknown_state():
    r = _fresh_registry()
    r.create(_mk())
    with pytest.raises(ValueError):
        r.update_status("u1", "banana")


def test_registry_rejects_unknown_column():
    r = _fresh_registry()
    r.create(_mk())
    r.update_status("u1", ur.SCANNING)
    with pytest.raises(ValueError):
        r.update_status("u1", ur.SCANNED_CLEAN, who_knows="x")


def test_registry_terminal_states():
    assert ur.INFECTED in ur.TERMINAL_STATES
    assert ur.PROCESSED in ur.TERMINAL_STATES
    assert ur.PROCESSING_FAIL in ur.TERMINAL_STATES


# ── pipeline.accept_upload validation ─────────────────────────────────────────

def test_accept_upload_rejects_bad_extension():
    with pytest.raises(pipeline.UploadRejected, match="extension"):
        pipeline.accept_upload("acme", "malware.exe", b"AAA")


def test_accept_upload_rejects_empty():
    with pytest.raises(pipeline.UploadRejected, match="empty"):
        pipeline.accept_upload("acme", "data.csv", b"")


def test_accept_upload_rejects_oversize(monkeypatch):
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "16")
    with pytest.raises(pipeline.UploadRejected, match="exceeds"):
        pipeline.accept_upload("acme", "data.csv", b"A" * 32)


def test_accept_upload_rejects_traversal_filename():
    with pytest.raises(pipeline.UploadRejected):
        pipeline.accept_upload("acme", "../etc/passwd", b"data")


def test_accept_upload_rejects_bad_client_id():
    with pytest.raises(ValueError):
        pipeline.accept_upload("../etc", "data.csv", b"xyz")


def test_accept_upload_ok_stores_file(tmp_path):
    data = b"date,sku,sales\n2024-01-01,A,10\n"
    rec = pipeline.accept_upload("acme", "ok.csv", data)
    assert rec.status == ur.UPLOADED
    assert rec.size_bytes == len(data)
    # file should be in the untrusted zone
    bk = zones.get_zone_backend(zones.Zone.UNTRUSTED)
    assert bk.exists(zones.upload_key("acme", rec.upload_id, "ok.csv"))


# ── scanner response parsing ──────────────────────────────────────────────────

class _FakeClamd:
    def __init__(self, response: str):
        self._resp = response
    def instream(self, data: bytes) -> str:
        return self._resp


def test_scanner_parses_clean():
    r = scan_bytes(b"abc", _FakeClamd("stream: OK"))
    assert r.verdict == ScanVerdict.CLEAN
    assert r.signature is None


def test_scanner_parses_infected():
    r = scan_bytes(b"abc", _FakeClamd("stream: Eicar-Test-Signature FOUND"))
    assert r.verdict == ScanVerdict.INFECTED
    assert r.signature == "Eicar-Test-Signature"


def test_scanner_parses_error():
    r = scan_bytes(b"abc", _FakeClamd("stream: some-reason ERROR"))
    assert r.verdict == ScanVerdict.ERROR


def test_scanner_rejects_oversize(monkeypatch):
    monkeypatch.setenv("CLAMAV_MAX_BYTES", "8")
    r = scan_bytes(b"X" * 100, _FakeClamd("stream: OK"))
    assert r.verdict == ScanVerdict.ERROR
    assert "too large" in r.raw_response
