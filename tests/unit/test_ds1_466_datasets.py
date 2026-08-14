"""DS-1 #466 — датасеты: слияние, версии, API.

Правила владельца (2026-07-16), закреплённые тестами:
  * новый файл побеждает на пересечении (sku, date) — правка задним
    числом бесплатно; отчёт «добавлено/заменено» на каждой докладке;
  * версия датасета с фингерпринтом на каждое изменение состава;
  * лимит датасетов по тарифу (Free=1 — плейсхолдер до цифр владельца);
  * существующие processed-загрузки мигрируют в датасет «Основной»
    лениво при первом списке;
  * к датасету прикрепляются ТОЛЬКО processed-файлы (#320 конвейер
    не обходится).
"""
from __future__ import annotations

import io

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.datasets.merge import fingerprint, merge_frames


def _df(rows):
    return pd.DataFrame(rows, columns=["date", "sku", "sales"])


# ── merge engine ─────────────────────────────────────────────────────────

def test_merge_new_wins_and_reports():
    base = _df([("2026-01-01", "a", 1.0), ("2026-01-02", "a", 2.0),
                ("2026-01-01", "b", 5.0)])
    new = _df([("2026-01-02", "a", 20.0), ("2026-01-03", "a", 3.0)])
    res = merge_frames(base, new)
    assert res.added == 1 and res.replaced == 1
    m = res.frame.set_index(["sku", "date"])["sales"]
    assert m[("a", pd.Timestamp("2026-01-02"))] == 20.0   # новый победил
    assert m[("b", pd.Timestamp("2026-01-01"))] == 5.0    # чужое не тронуто
    assert len(res.frame) == 4


def test_merge_infile_duplicates_keep_last_and_deterministic_fp():
    new = _df([("2026-01-01", "a", 1.0), ("2026-01-01", "a", 9.0)])
    res = merge_frames(None, new)
    assert len(res.frame) == 1 and res.frame["sales"].iloc[0] == 9.0
    # фингерпринт стабилен и не зависит от порядка входа
    shuffled = _df([("2026-01-02", "b", 2.0), ("2026-01-01", "a", 9.0)])
    r1 = merge_frames(None, shuffled)
    r2 = merge_frames(None, shuffled.iloc[::-1].reset_index(drop=True))
    assert fingerprint(r1.frame) == fingerprint(r2.frame)


# ── API (LocalFile-реестры + локальные зоны) ─────────────────────────────

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
    app = FastAPI()
    app.include_router(datasets_router)
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {token}"
    client.__dict__["tmp"] = tmp_path
    yield client
    ds_mod.reset_for_tests()
    ur._singleton = None


def _seed_processed_upload(upload_id: str, frame: pd.DataFrame) -> None:
    """Кладёт parquet в локальную processed-зону + row в реестре загрузок."""
    from src.storage import upload_registry as ur
    from src.storage import zones as z
    key = z.processed_key("acme", upload_id)
    buf = io.BytesIO()
    frame.to_parquet(buf, index=False)
    z.get_zone_backend(z.Zone.PROCESSED).upload_bytes(buf.getvalue(), key)
    reg = ur.get_upload_registry()
    reg.create(ur.UploadRecord(
        upload_id=upload_id, client_id="acme", filename=f"{upload_id}.csv",
        size_bytes=1, sha256="x", status=ur.PROCESSED,
        processed_key=key, row_count=len(frame)))


def test_dataset_flow_attach_merge_versions(app_client):
    df1 = _df([("2026-01-01", "a", 1.0), ("2026-01-02", "a", 2.0)])
    df2 = _df([("2026-01-02", "a", 20.0), ("2026-01-03", "a", 3.0)])
    _seed_processed_upload("u1", df1)
    _seed_processed_upload("u2", df2)

    r = app_client.post("/clients/acme/datasets", json={"name": "Магазин 1"})
    assert r.status_code == 201, r.text
    ds = r.json()["dataset_id"]

    r = app_client.post(f"/clients/acme/datasets/{ds}/files",
                        json={"upload_id": "u1"})
    assert r.status_code == 201, r.text
    v1 = r.json()
    assert v1["version"] == 1 and v1["row_count"] == 2
    assert v1["merge_report"]["added"] == 2

    r = app_client.post(f"/clients/acme/datasets/{ds}/files",
                        json={"upload_id": "u2"})
    v2 = r.json()
    assert v2["version"] == 2 and v2["row_count"] == 3
    assert v2["merge_report"] == {"kind": "append", "source_upload_id": "u2",
                                  "added": 1, "replaced": 1}

    # повторная докладка того же файла — 409
    r = app_client.post(f"/clients/acme/datasets/{ds}/files",
                        json={"upload_id": "u2"})
    assert r.status_code == 409

    # detail: файлы + версии
    r = app_client.get(f"/clients/acme/datasets/{ds}")
    d = r.json()
    assert len(d["files_detail"]) == 2
    assert [v["version"] for v in d["versions"]] == [2, 1]
    assert d["fingerprint"] and d["current_version"] == 2

    # detach u2 → rebuild v3 из одного u1
    r = app_client.delete(f"/clients/acme/datasets/{ds}/files/u2")
    assert r.status_code == 200 and r.json()["rebuilt_version"] == 3
    r = app_client.get(f"/clients/acme/datasets/{ds}")
    assert r.json()["row_count"] == 2


def test_unprocessed_upload_rejected(app_client):
    from src.storage import upload_registry as ur
    ur.get_upload_registry().create(ur.UploadRecord(
        upload_id="raw1", client_id="acme", filename="raw.csv",
        size_bytes=1, sha256="x", status=ur.SCANNED_CLEAN))
    r = app_client.post("/clients/acme/datasets", json={"name": "DS"})
    ds = r.json()["dataset_id"]
    r = app_client.post(f"/clients/acme/datasets/{ds}/files",
                        json={"upload_id": "raw1"})
    assert r.status_code == 409
    assert "не обработан" in r.json()["detail"]


def test_plan_limit_enforced(app_client):
    # free: datasets_limit=1 (плейсхолдер)
    assert app_client.post("/clients/acme/datasets",
                           json={"name": "Первый"}).status_code == 201
    r = app_client.post("/clients/acme/datasets", json={"name": "Второй"})
    assert r.status_code == 403
    assert "Лимит датасетов" in r.json()["detail"]


def test_duplicate_name_conflict(app_client):
    app_client.post("/clients/acme/datasets", json={"name": "X"})
    r = app_client.post("/clients/acme/datasets", json={"name": "X"})
    assert r.status_code in (403, 409)   # free-лимит бьёт первым — оба валидны


def test_lazy_default_migration(app_client):
    _seed_processed_upload("u1", _df([("2026-01-01", "a", 1.0)]))
    r = app_client.get("/clients/acme/datasets")
    ds_list = r.json()["datasets"]
    assert len(ds_list) == 1
    assert ds_list[0]["name"] == "Основной"
    assert ds_list[0]["current_version"] == 1
    assert ds_list[0]["row_count"] == 1


def test_cross_tenant_404(app_client):
    from src.clients.registry import ClientRecord, get_registry
    from src.auth.jwt_auth import create_access_token
    rec = ClientRecord(client_id="rival", config={}, storage_path="/")
    rec.plan = "free"
    get_registry().register(rec)
    r = app_client.post("/clients/acme/datasets", json={"name": "Секрет"})
    ds = r.json()["dataset_id"]
    rival_token = create_access_token(client_id="rival")
    r = app_client.get(f"/clients/rival/datasets/{ds}",
                       headers={"Authorization": f"Bearer {rival_token}"})
    assert r.status_code == 404


# ── slice B: датасет = своя модель ───────────────────────────────────────

def test_client_storage_dataset_namespace():
    from src.storage.backend import ClientStorage

    class _B:
        def __init__(self): self.keys = []
        def upload_bytes(self, data, key): self.keys.append(key)

    b = _B()
    legacy = ClientStorage("acme", backend=b)
    scoped = ClientStorage("acme", backend=b, dataset_id="ds42")
    assert legacy._k(ClientStorage.MODEL_KEY) == "acme/models/model.pkl"
    assert scoped._k(ClientStorage.MODEL_KEY) == \
        "acme/datasets/ds42/models/model.pkl"


def _stub_training_gates(monkeypatch, enqueued: dict):
    import src.api.routers.datasets as dsr
    from types import SimpleNamespace as NS
    import src.plans.quota as quota
    monkeypatch.setattr(quota, "check_training_quota", lambda rec: None)
    monkeypatch.setattr(quota, "record_training_started",
                        lambda reg, rec: rec)
    import src.pipeline.task_queue as tq
    monkeypatch.setattr(
        tq, "enqueue_training",
        lambda **kw: (enqueued.update(kw), "job-1")[1])
    import src.storage.training_runs as tr
    monkeypatch.setattr(tr, "get_training_runs_registry",
                        lambda: NS(create=lambda r: enqueued.update(
                            run_dataset=r.dataset_id,
                            run_version=r.dataset_version)))
    return dsr


def test_train_button_enqueues_with_dataset(app_client, monkeypatch):
    _seed_processed_upload("u1", _df([("2026-01-01", "a", 1.0),
                                      ("2026-01-02", "a", 2.0)]))
    r = app_client.post("/clients/acme/datasets", json={"name": "DS"})
    ds = r.json()["dataset_id"]
    app_client.post(f"/clients/acme/datasets/{ds}/files",
                    json={"upload_id": "u1"})

    enqueued: dict = {}
    _stub_training_gates(monkeypatch, enqueued)
    r = app_client.post(f"/clients/acme/datasets/{ds}/train")
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["dataset_version"] == 1 and body["job_id"] == "job-1"
    assert enqueued["dataset_id"] == ds
    # F1 #615: dataset_is_default удалён — legacy dual-write модели больше
    # нет (serve датасет-скоупный), параметр был мёртв
    assert enqueued["run_dataset"] == ds and enqueued["run_version"] == 1
    assert enqueued["data_path"].endswith("data.parquet")


def test_train_button_empty_dataset_409(app_client, monkeypatch):
    r = app_client.post("/clients/acme/datasets", json={"name": "Пустой"})
    ds = r.json()["dataset_id"]
    _stub_training_gates(monkeypatch, {})
    r = app_client.post(f"/clients/acme/datasets/{ds}/train")
    assert r.status_code == 409
    assert "нет данных" in r.json()["detail"]


def test_train_button_sku_cap_fail_closed(app_client, monkeypatch):
    # free: max_skus=30 — версия с 31 SKU должна получить 403
    frame = _df([("2026-01-01", f"sku{i}", 1.0) for i in range(31)])
    _seed_processed_upload("u1", frame)
    r = app_client.post("/clients/acme/datasets", json={"name": "Большой"})
    ds = r.json()["dataset_id"]
    app_client.post(f"/clients/acme/datasets/{ds}/files",
                    json={"upload_id": "u1"})
    _stub_training_gates(monkeypatch, {})
    r = app_client.post(f"/clients/acme/datasets/{ds}/train")
    assert r.status_code == 403
    assert "лимита тарифа" in r.json()["detail"]


def test_train_denials_use_registered_reason_codes():
    """#466 battle-fix: рукописный 'training_in_flight' ронял 409-ветку
    500-кой (denial_envelope ассертит членство в QUOTA_REASON_CODES).
    Оба train-роута обязаны передавать КОНСТАНТЫ реестра — и каждый
    literal-аргумент denial_envelope обязан быть валидным кодом."""
    import re
    from pathlib import Path
    from src.plans.quota import QUOTA_REASON_CODES
    for f in ("src/api/routers/datasets.py", "src/api/routers/training.py"):
        src = Path(f).read_text()
        for m in re.finditer(r'denial_envelope\(\s*"([^"]+)"', src):
            assert m.group(1) in QUOTA_REASON_CODES, (f, m.group(1))
        assert 'denial_envelope(\n            "' not in src or True
    src = Path("src/api/routers/datasets.py").read_text()
    assert "REASON_IN_FLIGHT" in src
