"""XP-1 (#469) slice B: GET /clients/{id}/explanation/{sku}.

Контракт: Business-гейт (403 остальным), 404-честность (нет файла / нет
SKU), факторы = top-4 групп по |вкладу| с направлением и долей, дифф с
прошлой моделью из _prev-снапшота. Прямые вызовы функции роута (идиома
test_507): auth/registry/storage стабятся, HTTP-слой не нужен.
"""
import pandas as pd
import pytest
from fastapi import HTTPException

import src.api.routers.inference as inf
from src.auth.jwt_auth import AuthContext


class _Rec:
    def __init__(self, plan):
        self.plan = plan
        self.suspended = False


class _Reg:
    def __init__(self, plan):
        self._r = _Rec(plan)

    def get(self, client_id):
        return self._r


class _Storage:
    """Стаб ClientStorage: держит текущий и прошлый фреймы объяснений."""
    def __init__(self, cur=None, prev=None):
        self._cur, self._prev = cur, prev

    def explanations_exist(self):
        return self._cur is not None

    def load_explanations(self):
        return self._cur

    def explanations_prev_exist(self):
        return self._prev is not None

    def load_explanations_prev(self):
        return self._prev


def _auth(client_id="c1"):
    return AuthContext(client_id=client_id, roles=[])


def _wire(monkeypatch, plan="business", cur=None, prev=None):
    monkeypatch.setattr(inf, "get_registry", lambda: _Reg(plan))
    monkeypatch.setattr(inf, "require_client_access", lambda cid, auth: None)
    monkeypatch.setattr(inf, "_default_dataset_id", lambda cid: None)
    import src.storage.backend as sb
    monkeypatch.setattr(
        sb, "ClientStorage",
        lambda client_id, dataset_id=None: _Storage(cur, prev))


def _cur_frame():
    # Недавний спрос доминирует ↑, Наличие ↓, Цена почти ноль (flat)
    return pd.DataFrame({
        "sku": ["S1"] * 4,
        "group": ["Недавний спрос", "Праздники и события",
                  "Наличие на складе", "Цена"],
        "contribution": [5.2, 3.1, -1.1, 0.05],
        "prediction_sum": [340.0] * 4,
        "base": [1.0] * 4,
        "heads": [14] * 4,
        "run_id": ["r2"] * 4,
        "generated_at": ["2026-07-24T10:00:00"] * 4,
    })


def _prev_frame():
    return pd.DataFrame({
        "sku": ["S1"] * 4,
        "group": ["Недавний спрос", "Праздники и события",
                  "Наличие на складе", "Цена"],
        "contribution": [3.0, 3.0, -1.0, 0.05],
        "prediction_sum": [300.0] * 4,
        "base": [1.0] * 4,
        "heads": [14] * 4,
    })


def test_non_business_plan_gets_403(monkeypatch):
    _wire(monkeypatch, plan="start", cur=_cur_frame())
    with pytest.raises(HTTPException) as e:
        inf.get_explanation("c1", "S1", auth=_auth())
    assert e.value.status_code == 403
    assert "Business" in e.value.detail


def test_no_explanations_file_404_with_honest_reason(monkeypatch):
    _wire(monkeypatch, plan="business", cur=None)
    with pytest.raises(HTTPException) as e:
        inf.get_explanation("c1", "S1", auth=_auth())
    assert e.value.status_code == 404
    assert "обучения" in e.value.detail


def test_unknown_sku_404(monkeypatch):
    _wire(monkeypatch, plan="business", cur=_cur_frame())
    with pytest.raises(HTTPException) as e:
        inf.get_explanation("c1", "NOPE", auth=_auth())
    assert e.value.status_code == 404


def test_factors_ranked_with_direction_and_share(monkeypatch):
    _wire(monkeypatch, plan="business", cur=_cur_frame())
    out = inf.get_explanation("c1", "S1", auth=_auth())
    assert [f["group"] for f in out["factors"]] == [
        "Недавний спрос", "Праздники и события", "Наличие на складе", "Цена"]
    assert out["factors"][0]["direction"] == "up"
    assert out["factors"][2]["direction"] == "down"
    assert out["factors"][3]["direction"] == "flat"     # 0.05 < 2% от Σ|c|
    shares = [f["share"] for f in out["factors"]]
    assert abs(sum(shares) - 1.0) < 1e-6                # все 4 группы = вся масса
    assert shares == sorted(shares, reverse=True)
    assert out["prediction_sum"] == 340.0
    assert out["heads"] == 14
    assert out["change"] is None                        # без prev — честный None


def test_change_diff_against_prev(monkeypatch):
    _wire(monkeypatch, plan="business", cur=_cur_frame(), prev=_prev_frame())
    out = inf.get_explanation("c1", "S1", auth=_auth())
    ch = out["change"]
    assert ch is not None
    assert ch["pct"] == pytest.approx(13.3, abs=0.05)   # 300 → 340
    assert ch["main_group"] == "Недавний спрос"         # дельта вклада 2.2 — максимум
    assert ch["direction"] == "up"


def test_prev_without_sku_keeps_change_none(monkeypatch):
    prev = _prev_frame()
    prev["sku"] = "OTHER"
    _wire(monkeypatch, plan="business", cur=_cur_frame(), prev=prev)
    out = inf.get_explanation("c1", "S1", auth=_auth())
    assert out["change"] is None


# ── #469: дефолтный датасет = последнего promoted-обучения ────────────────

def test_default_dataset_prefers_last_promoted_run(monkeypatch):
    class _DS:
        def __init__(self, i): self.dataset_id = i

    class _DsReg:
        def list_for_client(self, cid):
            return [_DS("old"), _DS("fresh")]

    class _Run:
        def __init__(self, status, model_path, ds):
            self.status, self.model_path, self.dataset_id = status, model_path, ds

    class _RunReg:
        def list_for_client(self, cid, limit=20):
            return [_Run("failed", None, "fresh"),
                    _Run("finished", "s3://m", "fresh"),
                    _Run("finished", "s3://m", "old")]

    import src.storage.datasets as dsm
    import src.storage.training_runs as trm
    monkeypatch.setattr(dsm, "get_datasets_registry", lambda: _DsReg())
    monkeypatch.setattr(trm, "get_training_runs_registry", lambda: _RunReg())
    assert inf._default_dataset_id("c1") == "fresh"


def test_default_dataset_falls_back_to_created_order(monkeypatch):
    class _DS:
        def __init__(self, i): self.dataset_id = i

    class _DsReg:
        def list_for_client(self, cid): return [_DS("old"), _DS("fresh")]

    class _RunReg:
        def list_for_client(self, cid, limit=20): return []

    import src.storage.datasets as dsm
    import src.storage.training_runs as trm
    monkeypatch.setattr(dsm, "get_datasets_registry", lambda: _DsReg())
    monkeypatch.setattr(trm, "get_training_runs_registry", lambda: _RunReg())
    assert inf._default_dataset_id("c1") == "old"


def test_default_dataset_ignores_runs_of_deleted_datasets(monkeypatch):
    class _DS:
        def __init__(self, i): self.dataset_id = i

    class _DsReg:
        def list_for_client(self, cid): return [_DS("alive")]

    class _Run:
        status, model_path, dataset_id = "finished", "s3://m", "deleted-ds"

    class _RunReg:
        def list_for_client(self, cid, limit=20): return [_Run()]

    import src.storage.datasets as dsm
    import src.storage.training_runs as trm
    monkeypatch.setattr(dsm, "get_datasets_registry", lambda: _DsReg())
    monkeypatch.setattr(trm, "get_training_runs_registry", lambda: _RunReg())
    assert inf._default_dataset_id("c1") == "alive"
