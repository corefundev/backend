"""
AUD-4 (#356) — 152-ФЗ erasure now erases the DATA, not just the identity row.

pii_purge anonymised sku_clients columns while the client's uploaded sales
files (the main PII carrier), the model derived from them, and the rows
naming both (sku_uploads.filename/sha256, sku_training_runs.data_path/
model_path) lived on forever. erase_client_data() removes them, and the purge
is FAIL-CLOSED: the identity is only anonymised when erasure reported zero
failures, so a storage outage leaves the client in the queue for the next run
instead of recording a half-erased account as erased.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import src.pipeline.pii_erasure as pe
import src.pipeline.pii_purge as pp


class _Store:
    """Minimal StorageBackend stand-in with an injectable failure."""

    def __init__(self, keys, fail_on=None, fail_list=False):
        self.keys = list(keys)
        self.deleted: list[str] = []
        self.fail_on = fail_on
        self.fail_list = fail_list

    def list_keys(self, prefix):
        if self.fail_list:
            raise RuntimeError("s3 down")
        return [k for k in self.keys if k.startswith(prefix)]

    def delete(self, key):
        if key == self.fail_on:
            raise RuntimeError("delete denied")
        self.deleted.append(key)


def _wire(monkeypatch, zone_store, model_store,
          uploads=(), training_deleted=0, uploads_raise=False):
    import src.storage.zones as z
    monkeypatch.setattr(z, "get_zone_backend", lambda zone: zone_store)
    monkeypatch.setattr("src.storage.backend.get_storage", lambda: model_store)

    def _uploads_reg():
        if uploads_raise:
            raise RuntimeError("pg down")
        return SimpleNamespace(
            list_for_client=lambda cid, limit: list(uploads),
            delete=lambda uid: True,
        )
    monkeypatch.setattr("src.storage.upload_registry.get_upload_registry", _uploads_reg)
    monkeypatch.setattr(
        "src.storage.training_runs.get_training_runs_registry",
        lambda: SimpleNamespace(delete_for_client=lambda cid: training_deleted),
    )


# ── erase_client_data ────────────────────────────────────────────────────────

def test_erases_objects_rows_and_reports_ok(monkeypatch):
    zone = _Store(["acme/u1/sales.csv", "acme/u1/data.parquet"])
    model = _Store(["acme/models/model.pkl", "acme/predictions/2026-01-01.parquet"])
    _wire(monkeypatch, zone, model,
          uploads=[SimpleNamespace(upload_id="u1")], training_deleted=3)

    r = pe.erase_client_data("acme")
    assert r.ok
    # 3 zones share the stub → its 2 keys are deleted once per zone (6) + 2 model
    assert r.objects_deleted == 8
    assert r.upload_rows_deleted == 1 and r.training_rows_deleted == 3
    assert "acme/models/model.pkl" in model.deleted


def test_other_tenants_are_never_touched(monkeypatch):
    zone = _Store(["acme/u1/a.csv", "acme-corp/u9/b.csv", "acmex/u2/c.csv"])
    model = _Store([])
    _wire(monkeypatch, zone, model)
    r = pe.erase_client_data("acme")
    assert r.ok
    # trailing-slash prefix: `acme/` must not match `acme-corp/` or `acmex/`
    assert set(zone.deleted) == {"acme/u1/a.csv"}


def test_out_of_prefix_key_from_a_buggy_backend_is_refused(monkeypatch):
    class _Evil(_Store):
        def list_keys(self, prefix):
            return ["acme/ok.csv", "victim/secret.csv"]   # backend bug
    zone, model = _Evil([]), _Store([])
    _wire(monkeypatch, zone, model)
    r = pe.erase_client_data("acme")
    assert not r.ok                                   # refused → fail-closed
    assert "victim/secret.csv" not in zone.deleted
    assert any("escaped prefix" in f for f in r.failures)


def test_delete_failure_is_recorded_not_swallowed(monkeypatch):
    zone = _Store(["acme/u1/a.csv"], fail_on="acme/u1/a.csv")
    _wire(monkeypatch, zone, _Store([]))
    r = pe.erase_client_data("acme")
    assert not r.ok and r.objects_deleted == 0


def test_list_failure_is_recorded(monkeypatch):
    _wire(monkeypatch, _Store([], fail_list=True), _Store([]))
    r = pe.erase_client_data("acme")
    assert not r.ok and any("list failed" in f for f in r.failures)


def test_db_failure_is_recorded(monkeypatch):
    _wire(monkeypatch, _Store([]), _Store([]), uploads_raise=True)
    r = pe.erase_client_data("acme")
    assert not r.ok and any("sku_uploads" in f for f in r.failures)


def test_erase_never_raises(monkeypatch):
    import src.storage.zones as z
    monkeypatch.setattr(z, "get_zone_backend",
                        lambda zone: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr("src.storage.backend.get_storage",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr("src.storage.upload_registry.get_upload_registry",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr("src.storage.training_runs.get_training_runs_registry",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    r = pe.erase_client_data("acme")     # must not raise
    assert not r.ok and len(r.failures) >= 4


def test_idempotent_second_run_is_a_noop(monkeypatch):
    zone, model = _Store([]), _Store([])
    _wire(monkeypatch, zone, model)
    r = pe.erase_client_data("acme")
    assert r.ok and r.objects_deleted == 0


# ── purge is fail-closed on incomplete erasure ───────────────────────────────

def _purge_wire(monkeypatch, erasure_ok):
    old = SimpleNamespace(client_id="acme", deleted_at="2020-01-01T00:00:00+00:00",
                          status="deleted")
    updates, audits = [], []
    monkeypatch.setattr(pp, "get_registry", lambda: SimpleNamespace(
        list_clients=lambda: [old],
        update=lambda cid, **f: updates.append(cid)))
    monkeypatch.setattr(pp, "record_event", lambda **kw: audits.append(kw))
    monkeypatch.setattr(
        "src.pipeline.pii_erasure.erase_client_data",
        lambda cid: SimpleNamespace(
            ok=erasure_ok, failures=[] if erasure_ok else ["s3 down"],
            as_dict=lambda: {"objects_deleted": 2}),
    )
    return updates, audits


def test_purge_anonymises_only_after_a_complete_erasure(monkeypatch):
    updates, audits = _purge_wire(monkeypatch, erasure_ok=True)
    s = pp.run_pii_purge()
    assert updates == ["acme"] and len(audits) == 1
    assert s["purged"] == 1 and s["failed"] == 0
    assert audits[0]["metadata"]["objects_deleted"] == 2   # erasure counts audited


def test_purge_keeps_the_client_queued_when_erasure_fails(monkeypatch):
    updates, audits = _purge_wire(monkeypatch, erasure_ok=False)
    s = pp.run_pii_purge()
    assert updates == [] and audits == []          # identity NOT anonymised
    assert s["purged"] == 0 and s["failed"] == 1
    assert s["failed_client_ids"] == ["acme"]      # retried next run


def test_delete_for_client_sql_is_tenant_scoped():
    import inspect
    from src.storage.training_runs import PostgresTrainingRunsRegistry
    src = inspect.getsource(PostgresTrainingRunsRegistry.delete_for_client)
    assert "WHERE client_id = %s" in src
    assert "self._conn()" in src and "_conn_read" not in src   # primary write


@pytest.mark.parametrize("cid,prefix", [("acme", "acme/"), ("acme/", "acme/")])
def test_prefix_always_ends_with_a_single_slash(cid, prefix):
    assert pe._client_prefix(cid) == prefix
