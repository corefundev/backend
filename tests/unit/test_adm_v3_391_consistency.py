"""ADM-v3-6 (#391) — отчёт консистентности зон vs sku_uploads.

Функциональные контракты (in-memory зоны + файловый реестр):
  • подсаженная сирота (объект без строки реестра) НАХОДИТСЯ (drill из
    acceptance);
  • UNTRUSTED-остаток после скана и terminal-QUARANTINE старше порога —
    leftovers;
  • строка processed без data.parquet — missing;
  • здоровый processed-набор ложных срабатываний не даёт;
  • запуск аудируется с актором и counts; сбой стораджа = 503 (не пустой
    «здоровый» отчёт — AUD-12 класс);
  • read-only: отчёт НИЧЕГО не удаляет (пин: у зон дергается только
    list_keys).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import src.api.uploads as up_api
from src.storage import upload_registry as ur
from src.storage import zones as z


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("UPLOAD_REGISTRY_PATH", str(tmp_path / "uploads.json"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    ur.reset_registry_for_tests()
    yield


def _http():
    return SimpleNamespace(client=SimpleNamespace(host="1.2.3.4"),
                           headers={"user-agent": "t"})


def _auth():
    return SimpleNamespace(require_role=lambda r: None, client_id="admin-ops",
                           auth_method="jwt", jti="j" * 32)


class _Zone:
    """In-memory zone: only listing is expected from the report."""
    def __init__(self, keys=()):
        self.keys = list(keys)
        self.deletes: list[str] = []

    def list_keys(self, prefix):
        return [k for k in self.keys if k.startswith(prefix)]

    def delete(self, key):    # the report must NEVER call this
        self.deletes.append(key)


def _wire(monkeypatch, zones: dict, clients=("acme",)):
    monkeypatch.setattr(z, "get_zone_backend", lambda zone: zones[zone])
    monkeypatch.setattr(
        "src.clients.registry.get_registry",
        lambda: SimpleNamespace(list_clients=lambda: [
            SimpleNamespace(client_id=c) for c in clients]))
    events = []
    # endpoint делает lazy `from src.audit import record_event` — патчим
    # имя в пакетном namespace, из которого он импортирует
    monkeypatch.setattr("src.audit.record_event",
                        lambda **kw: events.append(kw))
    return events


def _row(client_id, upload_id, status, filename="f.csv", **kw):
    rec = ur.UploadRecord(upload_id=upload_id, client_id=client_id,
                          filename=filename, size_bytes=1, sha256="0" * 64,
                          status=status, **kw)
    reg = ur.get_upload_registry()
    reg.create(rec)
    return rec


def test_planted_orphan_is_found(monkeypatch):
    zones = {z.Zone.UNTRUSTED: _Zone(), z.Zone.QUARANTINE: _Zone(),
             z.Zone.PROCESSED: _Zone(["acme/ghost123/data.parquet"])}
    events = _wire(monkeypatch, zones)
    out = up_api.admin_data_consistency_check(_http(), auth=_auth())
    assert out["counts"]["orphans"] == 1
    o = out["orphans"][0]
    assert o["zone"] == "processed" and "ghost123" in o["key"]
    assert events[0]["event_subtype"] == "data_consistency_check"
    assert events[0]["metadata"]["orphans"] == 1
    assert events[0]["metadata"]["actor_client_id"] == "admin-ops"


def test_untrusted_leftover_and_stale_quarantine(monkeypatch):
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    _row("acme", "u1", ur.PROCESSED, processed_key="acme/u1/data.parquet",
         updated_at=old)
    zones = {
        z.Zone.UNTRUSTED: _Zone(["acme/u1/f.csv"]),       # должен был уйти после скана
        z.Zone.QUARANTINE: _Zone(["acme/u1/f.csv"]),      # terminal + старый
        z.Zone.PROCESSED: _Zone(["acme/u1/data.parquet"]),
    }
    _wire(monkeypatch, zones)
    out = up_api.admin_data_consistency_check(_http(), auth=_auth())
    reasons = {l["reason"] for l in out["leftovers"]}
    assert any("untrusted" in r for r in reasons)
    assert any("quarantine terminal" in r for r in reasons)
    assert out["counts"]["missing"] == 0 and out["counts"]["orphans"] == 0


def test_processed_row_without_object_is_missing(monkeypatch):
    _row("acme", "u2", ur.PROCESSED, processed_key="acme/u2/data.parquet")
    zones = {z.Zone.UNTRUSTED: _Zone(), z.Zone.QUARANTINE: _Zone(),
             z.Zone.PROCESSED: _Zone()}
    _wire(monkeypatch, zones)
    out = up_api.admin_data_consistency_check(_http(), auth=_auth())
    assert out["counts"]["missing"] == 1
    assert out["missing"][0]["upload_id"] == "u2"


def test_healthy_state_no_false_positives(monkeypatch):
    _row("acme", "u3", ur.PROCESSED, processed_key="acme/u3/data.parquet")
    zones = {z.Zone.UNTRUSTED: _Zone(), z.Zone.QUARANTINE: _Zone(),
             z.Zone.PROCESSED: _Zone(["acme/u3/data.parquet",
                                      "acme/u3/manifest.json"])}
    _wire(monkeypatch, zones)
    out = up_api.admin_data_consistency_check(_http(), auth=_auth())
    assert out["counts"] == {"orphans": 0, "leftovers": 0, "missing": 0}


def test_report_is_read_only(monkeypatch):
    zones = {z.Zone.UNTRUSTED: _Zone(["acme/ghost/f.csv"]),
             z.Zone.QUARANTINE: _Zone(), z.Zone.PROCESSED: _Zone()}
    _wire(monkeypatch, zones)
    up_api.admin_data_consistency_check(_http(), auth=_auth())
    assert all(not zn.deletes for zn in zones.values()), (
        "отчёт read-only — удаления в этой итерации запрещены design of record"
    )


def test_storage_failure_is_503_not_empty_healthy_report(monkeypatch):
    class _Boom:
        def list_keys(self, prefix):
            raise RuntimeError("s3 down")
    zones = {z.Zone.UNTRUSTED: _Boom(), z.Zone.QUARANTINE: _Boom(),
             z.Zone.PROCESSED: _Boom()}
    _wire(monkeypatch, zones)
    with pytest.raises(HTTPException) as ei:
        up_api.admin_data_consistency_check(_http(), auth=_auth())
    assert ei.value.status_code == 503


def test_bad_threshold_is_422():
    with pytest.raises(HTTPException) as ei:
        up_api.admin_data_consistency_check(
            _http(), quarantine_stale_days=0, auth=_auth())
    assert ei.value.status_code == 422
