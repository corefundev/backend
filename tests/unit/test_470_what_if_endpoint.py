"""WF-1 #470: гейты эндпоинта POST …/datasets/{id}/what-if.

Тарифный гейт (Business-only — решение владельца 2026-08-12), валидация
сценария и честные отказы, когда модель не может увидеть цену.
Serve-конвейер здесь НЕ прогоняется (он покрыт test_470_what_if.py) —
фикстура останавливается на первом 4xx.
"""
import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "zones"))
    monkeypatch.setenv("DATASETS_REGISTRY_PATH", str(tmp_path / "ds.json"))
    monkeypatch.setenv("REGISTRY_PATH", str(tmp_path / "registry.json"))
    monkeypatch.setenv("JWT_SECRET_KEY",
                       "unit-test-jwt-secret-key-0123456789abcdef")
    monkeypatch.setenv("API_KEY", "unit-test-api-key-0123456789abcdef")

    import src.storage.datasets as ds_mod
    ds_mod.reset_for_tests()

    from src.clients.registry import ClientRecord, get_registry
    rec = ClientRecord(client_id="acme", config={}, storage_path="/")
    rec.plan = "business"
    get_registry().register(rec)

    from src.auth.jwt_auth import create_access_token
    token = create_access_token(client_id="acme")

    from src.api.routers.inference import router as inference_router
    app = FastAPI()
    app.include_router(inference_router)
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {token}"
    yield client
    ds_mod.reset_for_tests()


def _seed_dataset(prices) -> str:
    """Датасет с одной версией и снапшотом истории SKU 'A'."""
    from src.storage import zones as z
    from src.storage.datasets import get_datasets_registry

    n = len(prices)
    frame = pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=n, freq="D"),
        "sku": ["A"] * n,
        "sales": [5.0] * n,
        "price": prices,
    })
    from src.storage.datasets import DatasetRecord, DatasetVersion
    reg = get_datasets_registry()
    reg.create(DatasetRecord(dataset_id="ds-t1", client_id="acme", name="тест"))
    key = "acme/datasets/ds-t1/v1.parquet"
    z.get_zone_backend(z.Zone.PROCESSED).save_dataframe(frame, key)
    reg.add_version(DatasetVersion(dataset_id="ds-t1", version=1,
                                   snapshot_key=key, row_count=n, sku_count=1))
    return "ds-t1"


def _set_plan(plan: str) -> None:
    # файловый реестр отдаёт КОПИЮ записи — мутация не сохраняется;
    # меняем тариф честной пере-регистрацией
    from src.clients.registry import ClientRecord, get_registry
    rec = ClientRecord(client_id="acme", config={}, storage_path="/")
    rec.plan = plan
    get_registry().register(rec)


def test_free_plan_gets_403(app_client):
    _set_plan("free")
    r = app_client.post("/clients/acme/datasets/whatever/what-if",
                        json={"sku": "A", "price_mult": 0.9})
    assert r.status_code == 403
    assert "Business" in r.json()["detail"]


def test_start_plan_gets_403(app_client):
    _set_plan("start")
    r = app_client.post("/clients/acme/datasets/whatever/what-if",
                        json={"sku": "A", "price_mult": 0.9})
    assert r.status_code == 403


def test_empty_scenario_422(app_client):
    ds_id = _seed_dataset([100.0] * 60)
    r = app_client.post(f"/clients/acme/datasets/{ds_id}/what-if",
                        json={"sku": "A"})
    assert r.status_code == 422
    assert "цены" in r.json()["detail"] or "промо" in r.json()["detail"]


def test_price_mult_bounds_422(app_client):
    ds_id = _seed_dataset([100.0] * 60)
    r = app_client.post(f"/clients/acme/datasets/{ds_id}/what-if",
                        json={"sku": "A", "price_mult": 3.0})
    assert r.status_code == 422


def test_unknown_dataset_404(app_client):
    r = app_client.post("/clients/acme/datasets/nope/what-if",
                        json={"sku": "A", "price_mult": 0.9})
    assert r.status_code == 404


def test_constant_price_honest_409(app_client):
    ds_id = _seed_dataset([100.0] * 60)
    r = app_client.post(f"/clients/acme/datasets/{ds_id}/what-if",
                        json={"sku": "A", "price_mult": 0.9})
    assert r.status_code == 409
    assert "константна" in r.json()["detail"]


def test_missing_price_column_honest_409(app_client):
    from src.storage import zones as z
    from src.storage.datasets import get_datasets_registry
    frame = pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=60, freq="D"),
        "sku": ["A"] * 60,
        "sales": [5.0] * 60,
    })
    from src.storage.datasets import DatasetRecord, DatasetVersion
    reg = get_datasets_registry()
    reg.create(DatasetRecord(dataset_id="ds-t2", client_id="acme", name="безцены"))
    key = "acme/datasets/ds-t2/v1.parquet"
    z.get_zone_backend(z.Zone.PROCESSED).save_dataframe(frame, key)
    reg.add_version(DatasetVersion(dataset_id="ds-t2", version=1,
                                   snapshot_key=key, row_count=60, sku_count=1))
    r = app_client.post("/clients/acme/datasets/ds-t2/what-if",
                        json={"sku": "A", "price_mult": 0.9})
    assert r.status_code == 409
    assert "нет" in r.json()["detail"]


def test_soft_deleted_dataset_404(app_client):
    """review #616 F5: what-if не сервит модель удалённого датасета."""
    ds_id = _seed_dataset([100.0, 101.0] * 30)
    from src.storage.datasets import get_datasets_registry
    get_datasets_registry().soft_delete(ds_id)
    r = app_client.post(f"/clients/acme/datasets/{ds_id}/what-if",
                        json={"sku": "A", "price_mult": 0.9})
    assert r.status_code == 404
