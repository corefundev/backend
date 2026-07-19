"""#347: SCANNED_CLEAN→PROCESSING must be an atomic single-winner claim.

Prereq for scaling sku-process past one replica: with the old
check-then-update pattern two workers could both pass the pre-check and
parse the same upload twice.
"""
import threading

import pytest

import src.storage.upload_registry as ur
from src.storage.upload_registry import LocalFileUploadRegistry, UploadRecord


def _reg(tmp_path):
    reg = LocalFileUploadRegistry(path=str(tmp_path / "uploads.json"))
    reg.create(UploadRecord(
        upload_id="u1", client_id="c", filename="f.csv",
        size_bytes=1, sha256="x", status=ur.SCANNED_CLEAN))
    return reg


def test_claim_single_winner_under_concurrency(tmp_path):
    reg = _reg(tmp_path)
    wins: list = []
    barrier = threading.Barrier(8)

    def racer():
        barrier.wait()
        got = reg.claim("u1", ur.SCANNED_CLEAN, ur.PROCESSING)
        if got is not None:
            wins.append(got)

    threads = [threading.Thread(target=racer) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert len(wins) == 1, f"exactly one winner expected, got {len(wins)}"
    assert reg.get("u1").status == ur.PROCESSING


def test_claim_refuses_invalid_transition(tmp_path):
    reg = _reg(tmp_path)
    with pytest.raises(ur.InvalidTransition):
        reg.claim("u1", ur.SCANNED_CLEAN, ur.PROCESSED)


def test_claim_loser_is_none_not_exception(tmp_path):
    reg = _reg(tmp_path)
    assert reg.claim("u1", ur.SCANNED_CLEAN, ur.PROCESSING) is not None
    assert reg.claim("u1", ur.SCANNED_CLEAN, ur.PROCESSING) is None


def test_run_process_uses_atomic_claim_not_check_then_update():
    # Статический гвард: путь воркера не должен вернуться к TOCTOU-паттерну.
    from pathlib import Path
    src = Path("src/storage/upload_pipeline.py").read_text()
    assert "registry.claim(upload_id, ur.SCANNED_CLEAN, ur.PROCESSING)" in src
    # безусловный переход в PROCESSING (проигравший бы его повторил) запрещён
    assert "registry.update_status(upload_id, ur.PROCESSING)" not in src, (
        "unconditional PROCESSING transition reintroduced — see #347"
    )


def test_pg_claim_is_conditional_update():
    # PG-бэкенд: условие по старому статусу обязано жить В САМОМ UPDATE.
    from pathlib import Path
    src = Path("src/storage/upload_registry.py").read_text()
    assert "WHERE upload_id = %s AND status = %s RETURNING *" in src
