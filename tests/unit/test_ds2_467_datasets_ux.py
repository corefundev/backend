"""DS-2 #467 — dataset-centric «Данные» UX: per-file merge deltas,
dataset-aimed uploads with worker auto-attach, and the per-dataset
model card (up-to-date flag + «точнее наивного»).

Same harness as test_ds1_466: LocalFile registries + local zones.
"""
from __future__ import annotations

import io
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _df(rows):
    return pd.DataFrame(rows, columns=["date", "sku", "sales"])


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "zones"))
    monkeypatch.setenv("DATASETS_REGISTRY_PATH", str(tmp_path / "ds.json"))
    monkeypatch.setenv("UPLOAD_REGISTRY_PATH", str(tmp_path / "uploads.json"))
    monkeypatch.setenv("REGISTRY_PATH", str(tmp_path / "registry.json"))
    monkeypatch.setenv("JWT_SECRET_KEY",
                       "unit-test-jwt-secret-key-0123456789abcdef")
    monkeypatch.setenv("API_KEY", "unit-test-api-key-0123456789abcdef")

    import src.storage.datasets as ds_mod
    import src.storage.upload_registry as ur
    ds_mod.reset_for_tests()
    if hasattr(ur, "reset_for_tests"):
        ur.reset_for_tests()
    else:
        ur._singleton = None

    from src.clients.registry import ClientRecord, get_registry
    rec = ClientRecord(client_id="acme", config={}, storage_path="/")
    rec.plan = "free"
    get_registry().register(rec)

    from src.auth.jwt_auth import create_access_token
    token = create_access_token(client_id="acme")

    from src.api.routers.datasets import router as datasets_router
    from src.api.uploads import router as uploads_router
    app = FastAPI()
    app.include_router(datasets_router)
    app.include_router(uploads_router)
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {token}"
    yield client
    ds_mod.reset_for_tests()
    ur._singleton = None


def _seed_processed_upload(upload_id: str, frame: pd.DataFrame,
                           dataset_id=None) -> None:
    from src.storage import upload_registry as ur
    from src.storage import zones as z
    key = z.processed_key("acme", upload_id)
    buf = io.BytesIO()
    frame.to_parquet(buf, index=False)
    z.get_zone_backend(z.Zone.PROCESSED).upload_bytes(buf.getvalue(), key)
    ur.get_upload_registry().create(ur.UploadRecord(
        upload_id=upload_id, client_id="acme", filename=f"{upload_id}.csv",
        size_bytes=1, sha256="x", status=ur.PROCESSED,
        processed_key=key, row_count=len(frame),
        dataset_id=dataset_id))


def _mk_dataset(app_client, name="Магазин") -> str:
    r = app_client.post("/clients/acme/datasets", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["dataset_id"]


# ── per-file merge deltas ────────────────────────────────────────────────

def test_merge_stats_persisted_per_file(app_client):
    _seed_processed_upload("u1", _df([("2026-01-01", "a", 1.0),
                                      ("2026-01-02", "a", 2.0)]))
    _seed_processed_upload("u2", _df([("2026-01-02", "a", 20.0),
                                      ("2026-01-03", "a", 3.0)]))
    ds = _mk_dataset(app_client)
    app_client.post(f"/clients/acme/datasets/{ds}/files",
                    json={"upload_id": "u1"})
    app_client.post(f"/clients/acme/datasets/{ds}/files",
                    json={"upload_id": "u2"})
    files = {f["upload_id"]: f for f in
             app_client.get(f"/clients/acme/datasets/{ds}").json()["files_detail"]}
    assert files["u1"]["merge_added"] == 2
    assert files["u1"]["merge_replaced"] == 0
    assert files["u2"]["merge_added"] == 1
    assert files["u2"]["merge_replaced"] == 1


# ── dataset-aimed uploads ────────────────────────────────────────────────

def test_upload_rejects_foreign_dataset(app_client, monkeypatch):
    import src.api.uploads as up_mod
    monkeypatch.setattr(up_mod, "enqueue_scan", lambda *a, **k: "job-1")
    r = app_client.post(
        "/clients/acme/uploads",
        files={"file": ("s.csv", b"date,sku,sales\n2026-01-01,a,1\n",
                        "text/csv")},
        data={"dataset_id": "nope"})
    assert r.status_code == 404


def test_upload_stores_dataset_target(app_client, monkeypatch):
    import src.api.uploads as up_mod
    monkeypatch.setattr(up_mod, "enqueue_scan", lambda *a, **k: "job-1")
    ds = _mk_dataset(app_client)
    r = app_client.post(
        "/clients/acme/uploads",
        files={"file": ("s.csv", b"date,sku,sales\n2026-01-01,a,1\n",
                        "text/csv")},
        data={"dataset_id": ds})
    assert r.status_code == 202, r.text
    from src.storage import upload_registry as ur
    rec = ur.get_upload_registry().get(r.json()["upload_id"])
    assert rec.dataset_id == ds
    # …and the list endpoint surfaces it (история подготовок column)
    rows = app_client.get("/clients/acme/uploads").json()
    assert any(x["dataset_id"] == ds for x in rows)


def test_auto_attach_after_prep(app_client):
    from src.pipeline.upload_workers import _auto_attach_to_dataset
    from src.storage import upload_registry as ur
    ds = _mk_dataset(app_client)
    _seed_processed_upload("u1", _df([("2026-01-01", "a", 1.0)]),
                           dataset_id=ds)
    rec = ur.get_upload_registry().get("u1")

    out = _auto_attach_to_dataset(rec)
    assert out == {"dataset_id": ds, "version": 1}
    d = app_client.get(f"/clients/acme/datasets/{ds}").json()
    assert d["current_version"] == 1 and d["row_count"] == 1
    assert d["files_detail"][0]["merge_added"] == 1

    # RQ-retry rerun is idempotent
    assert _auto_attach_to_dataset(rec)["already_attached"] is True


def test_pending_uploads_listed_on_detail(app_client):
    from src.storage import upload_registry as ur
    ds = _mk_dataset(app_client)
    ur.get_upload_registry().create(ur.UploadRecord(
        upload_id="w1", client_id="acme", filename="wait.csv",
        size_bytes=1, sha256="x", status=ur.SCANNED_CLEAN, dataset_id=ds))
    d = app_client.get(f"/clients/acme/datasets/{ds}").json()
    assert [p["upload_id"] for p in d["pending_uploads"]] == ["w1"]
    assert d["pending_uploads"][0]["status"] == "scanned_clean"


# ── model card ───────────────────────────────────────────────────────────

def _fake_runs_registry(runs):
    return SimpleNamespace(list_for_client=lambda *a, **k: runs)


def _run(ds, version, wmape=0.30, baseline=0.40, **kw):
    base = dict(status="finished", model_path="m.pkl", dataset_id=ds,
                dataset_version=version, wmape=wmape,
                wmape_order_7=0.2, wmape_order_14=0.15,
                baseline_wmape=baseline, ended_at="2026-07-18T00:00:00")
    base.update(kw)
    return SimpleNamespace(**base)


def test_model_block_up_to_date_and_improvement(app_client, monkeypatch):
    import src.storage.training_runs as tr
    _seed_processed_upload("u1", _df([("2026-01-01", "a", 1.0)]))
    ds = _mk_dataset(app_client)
    app_client.post(f"/clients/acme/datasets/{ds}/files",
                    json={"upload_id": "u1"})

    monkeypatch.setattr(tr, "get_training_runs_registry",
                        lambda *a, **k: _fake_runs_registry([_run(ds, 1)]))
    m = app_client.get(f"/clients/acme/datasets/{ds}").json()["model"]
    assert m["up_to_date"] is True
    assert m["wmape_order_14"] == 0.15
    assert abs(m["improvement_vs_naive"] - 0.25) < 1e-9

    # data moved to v2 → same model now stale; newest run wins the card
    monkeypatch.setattr(
        tr, "get_training_runs_registry",
        lambda *a, **k: _fake_runs_registry(
            [_run(ds, 1), _run(ds, 1, status="failed")]))
    _seed_processed_upload("u2", _df([("2026-01-02", "a", 2.0)]))
    app_client.post(f"/clients/acme/datasets/{ds}/files",
                    json={"upload_id": "u2"})
    m = app_client.get(f"/clients/acme/datasets/{ds}").json()["model"]
    assert m["up_to_date"] is False


def test_model_block_absent_without_runs(app_client, monkeypatch):
    import src.storage.training_runs as tr
    ds = _mk_dataset(app_client)
    monkeypatch.setattr(tr, "get_training_runs_registry",
                        lambda *a, **k: _fake_runs_registry([]))
    r = app_client.get("/clients/acme/datasets")
    row = next(d for d in r.json()["datasets"] if d["dataset_id"] == ds)
    assert row["model"] is None
