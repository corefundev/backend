"""
DP-1 (#321): prep-stage FSM foundation. Adds NEEDS_MAPPING between the AV scan
and the canonical parse, with a sniff hook (stub auto-confirms in DP-1 → flow
unchanged) and a confirm entry point. DP-2/DP-3 fill in the real sniff/mapping.
"""
from types import SimpleNamespace

import pytest

from src.storage import upload_registry as ur


def test_needs_mapping_state_and_transitions():
    assert ur.NEEDS_MAPPING in ur.ALL_STATES
    assert ur.NEEDS_MAPPING not in ur.TERMINAL_STATES          # user can still act
    # canonical auto-confirm still allowed; new mapping branch added
    assert ur.PROCESSING in ur.ALLOWED_NEXT[ur.SCANNED_CLEAN]
    assert ur.NEEDS_MAPPING in ur.ALLOWED_NEXT[ur.SCANNED_CLEAN]
    # confirmed mapping re-enters the parse
    assert ur.PROCESSING in ur.ALLOWED_NEXT[ur.NEEDS_MAPPING]


def test_transition_guard_allows_and_blocks():
    ur._assert_transition(ur.SCANNED_CLEAN, ur.NEEDS_MAPPING)  # no raise
    ur._assert_transition(ur.NEEDS_MAPPING, ur.PROCESSING)     # no raise
    with pytest.raises(ur.InvalidTransition):
        ur._assert_transition(ur.NEEDS_MAPPING, ur.PROCESSED)  # can't skip PROCESSING


def test_sniff_failsafe_auto_confirms():
    # DP-4b: with no readable quarantine file the sniff errors → fail-safe
    # auto-confirm (False), so a broken sniff never blocks the upload.
    from src.storage.upload_pipeline import sniff_needs_mapping
    rec = ur.UploadRecord(upload_id="u1", client_id="c1", filename="f.csv",
                          size_bytes=1, sha256="x", status=ur.SCANNED_CLEAN)
    assert sniff_needs_mapping(rec) is False


def test_confirm_prep_only_acts_on_needs_mapping(monkeypatch):
    from src.pipeline import upload_workers as uw
    recs = {
        "m": SimpleNamespace(status=ur.NEEDS_MAPPING, client_id="c1"),
        "o": SimpleNamespace(status=ur.PROCESSED, client_id="c1"),
    }
    monkeypatch.setattr(ur, "get_upload_registry",
                        lambda: SimpleNamespace(get=lambda uid: recs.get(uid)))
    calls: list = []
    monkeypatch.setattr(uw, "enqueue_process",
                        lambda upload_id, client_id: calls.append(upload_id) or "job1")

    assert uw.confirm_prep("m") == "job1"       # NEEDS_MAPPING → enqueues parse
    assert calls == ["m"]
    assert uw.confirm_prep("o") is None         # wrong state → no-op, idempotent
    assert calls == ["m"]


def test_run_process_accepts_needs_mapping_entry():
    # the confirmed-mapping path re-enters run_process from NEEDS_MAPPING, so the
    # guard must accept it (not only SCANNED_CLEAN).
    from pathlib import Path
    src = Path("src/storage/upload_pipeline.py").read_text()
    assert "ur.NEEDS_MAPPING" in src, "run_process guard must accept NEEDS_MAPPING"


def test_scan_job_consults_sniff_hook():
    from pathlib import Path
    src = Path("src/pipeline/upload_workers.py").read_text()
    assert "sniff_needs_mapping" in src, "_scan_job must branch on the sniff hook"
    assert "confirm_prep" in src, "confirm entry point must exist"
