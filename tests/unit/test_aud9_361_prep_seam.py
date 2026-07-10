"""
AUD-9 (#361) — three data-prep seam defects.

1. QUOTE-BLIND DELIMITER: `_delimiter_scores` counted delimiters with raw
   `line.count(d)`, including inside quoted cells. A comma-delimited file
   with `;` in a quoted description («болт; гайка») scored ';' once per
   row — the exact stable-count signature the #348 override trusts — and
   detection flipped to ';': the mirror image of the bug #348 fixed.
   Counting is now quote-aware.

2. PADDED HEADERS: sniff strips header whitespace, so the auto-map's
   source names are stripped — but pandas keeps `" Дата "` verbatim.
   `_apply_mapping`'s `source in df.columns` matched nothing, every column
   was dropped, and `_validate` failed "missing required columns" on a
   file the system had just said it mapped. `_apply_mapping` now strips
   df headers first (mirroring `_validate`).

3. DELETE-DURING-PREP ORPHAN: cancel removes the row + objects existing
   at that moment; an in-flight prep job then writes parquet+manifest to
   PROCESSED and only hits the missing row at the final registry write.
   Those objects were ownerless customer PII — invisible to the uploads
   UI, cancel, and the #310/AUD-4 retention purge. The final
   `update_status` is now the publish CAS: on KeyError the just-written
   objects are removed. cancel_upload also sweeps the manifest sibling
   it always left behind.
"""
from __future__ import annotations

import types

import pandas as pd
import pytest

from src.storage import upload_registry as ur
from src.storage import upload_pipeline as up
from src.storage import zones as z
from src.storage.sandbox import SandboxResult
from src.validation.secure_parser import (
    _apply_mapping,
    _count_outside_quotes,
    detect_delimiter,
)


# ── 1. quote-aware delimiter detection ───────────────────────────────────

def test_count_outside_quotes():
    assert _count_outside_quotes('a,b,c', ",") == 2
    assert _count_outside_quotes('a,"b;c",d', ";") == 0
    assert _count_outside_quotes('a,"b;c",d', ",") == 2
    # doubled-quote escape inside a quoted cell stays inside
    assert _count_outside_quotes('"болт; ""гайка""; шайба",1', ";") == 0
    assert _count_outside_quotes('"болт; ""гайка""; шайба",1', ",") == 1


def test_comma_file_with_semicolons_inside_quotes():
    """The AUD-9 victim: 2-column comma CSV whose text column carries a ';'
    per row inside quotes. Raw counting scores ';' as stable → override
    flips to ';'. Quote-aware counting must keep ','."""
    text = "\n".join(
        ['Дата,Название', ]
        + [f'2026-01-{d:02d},"болт; гайка; шайба {d}"' for d in range(1, 21)]
    )
    assert detect_delimiter(text) == ","


def test_348_ru_semicolon_with_unit_comma_still_detected():
    """The original #348 case must keep working: ';' file with a comma in
    a header cell («Цена, руб») putting ',' on every line."""
    text = "\n".join(
        ['Дата;Артикул;"Цена, руб";Продажи']
        + [f"2026-01-{d:02d};SKU{d};1,5;{d}" for d in range(1, 21)]
    )
    assert detect_delimiter(text) == ";"


# ── 2. padded headers survive mapping ────────────────────────────────────

def test_apply_mapping_strips_padded_headers():
    df = pd.DataFrame({" Дата ": ["2026-01-01"], " Артикул ": ["A"],
                       "Продажи ": [5]})
    out = _apply_mapping(df, {"date": "Дата", "sku": "Артикул",
                              "sales": "Продажи"})
    assert list(out.columns) == ["date", "sku", "sales"]
    assert out.iloc[0]["sku"] == "A"


def test_apply_mapping_unpadded_unchanged():
    df = pd.DataFrame({"Дата": ["2026-01-01"], "Артикул": ["A"], "Продажи": [5]})
    out = _apply_mapping(df, {"date": "Дата", "sku": "Артикул", "sales": "Продажи"})
    assert list(out.columns) == ["date", "sku", "sales"]


# ── 3. DELETE during prep leaves no orphan ───────────────────────────────

@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("UPLOAD_REGISTRY_PATH", str(tmp_path / "uploads.json"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    ur.reset_registry_for_tests()
    yield


class _RecordingProcessed:
    """PROCESSED-zone stub: records uploads and deletes."""
    def __init__(self):
        self.stored: set[str] = set()
        self.deleted: list[str] = []

    def upload(self, local_path, key):
        self.stored.add(key)

    def upload_bytes(self, data, key):
        self.stored.add(key)

    def delete(self, key):
        self.deleted.append(key)
        self.stored.discard(key)


def _sandbox_ok(tmp_path):
    out = tmp_path / "out.parquet"
    out.write_bytes(b"pq")
    return types.SimpleNamespace(run=lambda data, filename, mapping=None: SandboxResult(
        ok=True, exit_code=0, stdout="", stderr="", output_path=str(out),
        manifest={"row_count": 1, "sku_count": 1}))


def _zones(monkeypatch, processed):
    quarantine = types.SimpleNamespace(
        download_bytes=lambda key: b"csv",
        delete=lambda key: None,
    )
    monkeypatch.setattr(z, "get_zone_backend", lambda zone: (
        processed if zone is z.Zone.PROCESSED else quarantine))


def test_cancel_mid_prep_removes_just_written_objects(tmp_path, monkeypatch):
    reg = ur.get_upload_registry()
    reg.create(ur.UploadRecord(upload_id="u1", client_id="c1", filename="f.csv",
                               size_bytes=3, sha256="x", status=ur.SCANNED_CLEAN))
    processed = _RecordingProcessed()
    _zones(monkeypatch, processed)

    # Simulate the user's DELETE landing while the sandbox parses: the row
    # vanishes right after the PROCESSED writes, before the final registry
    # publish. The manifest upload is the last write — hook the deletion there.
    orig_upload_bytes = processed.upload_bytes

    def upload_bytes_then_cancel(data, key):
        orig_upload_bytes(data, key)
        reg.delete("u1")
    processed.upload_bytes = upload_bytes_then_cancel

    with pytest.raises(KeyError):
        up.run_process("u1", sandbox=_sandbox_ok(tmp_path))

    assert processed.stored == set(), (
        f"ownerless PII survived the mid-flight cancel: {processed.stored}"
    )
    pq = z.processed_key("c1", "u1")
    mf = z.processed_manifest_key("c1", "u1")
    assert pq in processed.deleted and mf in processed.deleted


def test_successful_prep_still_publishes(tmp_path, monkeypatch):
    reg = ur.get_upload_registry()
    reg.create(ur.UploadRecord(upload_id="u1", client_id="c1", filename="f.csv",
                               size_bytes=3, sha256="x", status=ur.SCANNED_CLEAN))
    processed = _RecordingProcessed()
    _zones(monkeypatch, processed)

    rec = up.run_process("u1", sandbox=_sandbox_ok(tmp_path))
    assert rec.status == ur.PROCESSED
    assert z.processed_key("c1", "u1") in processed.stored
    assert z.processed_manifest_key("c1", "u1") in processed.stored
    assert processed.deleted == []


def test_cancel_upload_sweeps_the_manifest_too(monkeypatch):
    reg = ur.get_upload_registry()
    reg.create(ur.UploadRecord(upload_id="u1", client_id="c1", filename="f.csv",
                               size_bytes=3, sha256="x", status=ur.PROCESSED))
    deleted: list[tuple[str, str]] = []

    def backend_for(zone):
        return types.SimpleNamespace(
            delete=lambda key, _z=zone: deleted.append((_z.value, key)))
    monkeypatch.setattr(z, "get_zone_backend", backend_for)

    assert up.cancel_upload("u1") is True
    keys = [k for _, k in deleted]
    assert z.processed_key("c1", "u1") in keys
    assert z.processed_manifest_key("c1", "u1") in keys, (
        "cancel left the client-identifying manifest.json behind"
    )
