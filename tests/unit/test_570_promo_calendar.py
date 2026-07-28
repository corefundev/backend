"""#570 PC-1: календарь акций — формат, реестр, конвейер, API-контракты.

Парсер и маппинг — настоящие (чистые функции); Postgres и sandbox стабятся
(юниты без инфраструктуры): реестр — LocalFile во временном каталоге.
"""
import pandas as pd
import pytest
from fastapi import HTTPException

import src.api.routers.promo_calendar as pcr
from src.auth.jwt_auth import AuthContext
from src.storage.promo_calendar import (
    LocalFilePromoCalendarRegistry,
    PromoEvent,
)
from src.validation.column_mapping import propose_promo_mapping
from src.validation.secure_parser import _validate_promo_calendar


def _auth(cid="c1"):
    return AuthContext(client_id=cid, roles=[])


class _Req:
    headers = {}
    class client:  # noqa: D106 — минимальный Request-стаб
        host = "127.0.0.1"


# ── маппинг заголовков ───────────────────────────────────────────────────

def test_promo_mapping_ru_1c_headers():
    p = propose_promo_mapping(
        ["Артикул", "Дата начала акции", "Дата окончания", "Скидка, %", "Название акции"])
    assert p.mapping["sku"] == "Артикул"
    assert p.mapping["date_from"] == "Дата начала акции"
    assert p.mapping["date_to"] == "Дата окончания"
    assert p.mapping["depth"] == "Скидка, %"
    assert not p.missing_required


def test_promo_mapping_missing_dates_reported():
    p = propose_promo_mapping(["Артикул", "Скидка"])
    assert set(p.missing_required) == {"date_from", "date_to"}


# ── построчная валидация (формат зафиксирован эпиком) ────────────────────

def _df(rows):
    return pd.DataFrame(rows, columns=["sku", "category", "date_from",
                                       "date_to", "depth", "name"])


def test_validate_row_rules_and_honest_report():
    df = _df([
        ["SKU-1", "",        "01.08.2026", "10.08.2026", "15",  "ок"],
        ["",      "Молочка", "2026-08-05", "2026-08-20", "",    "ок категория"],
        ["SKU-2", "Молочка", "01.08.2026", "05.08.2026", "20",  "оба поля"],
        ["SKU-3", "",        "15.08.2026", "10.08.2026", "10",  "даты наоборот"],
        ["SKU-4", "",        "01.09.2026", "10.09.2026", "150", "depth за пределами"],
        ["SKU-5", "",        "03.08.2026", "03.08.2026", "0",   "однодневная"],
    ])
    accepted, report = _validate_promo_calendar(df)
    assert report["rows_total"] == 6
    assert report["rows_accepted"] == 3 and len(accepted) == 3
    assert report["rows_rejected"] == 3
    reasons = " | ".join(r["reason"] for r in report["rejected_examples"])
    assert "ровно одно" in reasons and "позже" in reasons and "0 до 100" in reasons
    # номера строк файла (заголовок = строка 1)
    assert [r["line"] for r in report["rejected_examples"]] == [4, 5, 6]
    assert report["sku_rows"] == 2 and report["category_rows"] == 1


def test_validate_all_rejected_fails_whole_file():
    df = _df([["SKU-1", "Кат", "01.08.2026", "10.08.2026", "", ""]])
    with pytest.raises(ValueError, match="no valid rows"):
        _validate_promo_calendar(df)


def test_validate_missing_required_columns():
    with pytest.raises(ValueError, match="missing required columns"):
        _validate_promo_calendar(pd.DataFrame({"sku": ["a"]}))


# ── реестр: кандидат → atomic swap → снятие ──────────────────────────────

@pytest.fixture()
def reg(tmp_path):
    return LocalFilePromoCalendarRegistry(str(tmp_path / "pc.json"))


def _events():
    return [PromoEvent(sku="SKU-1", category=None, date_from="2026-08-01",
                       date_to="2026-08-10", depth_pct=15.0, name="х")]


def test_registry_candidate_apply_swap_and_remove(reg):
    c1 = reg.create_candidate("c1", "ds1", "a.csv",
                              {"date_min": "2026-08-01", "date_max": "2026-08-10"},
                              _events(), "untrusted/key1")
    assert c1.status == "pending_review"
    a1 = reg.apply(c1.calendar_id)
    assert a1.status == "active"
    assert reg.get_active("ds1").calendar_id == c1.calendar_id

    # повторная загрузка → новый кандидат; активный НЕ тронут до apply
    c2 = reg.create_candidate("c1", "ds1", "b.csv", {}, _events(), None)
    assert reg.get_active("ds1").calendar_id == c1.calendar_id
    assert reg.get_candidate("ds1").calendar_id == c2.calendar_id

    # atomic swap: старый → replaced, новый → active
    reg.apply(c2.calendar_id)
    assert reg.get_active("ds1").calendar_id == c2.calendar_id
    assert reg.get(c1.calendar_id).status == "replaced"

    # события сохранены и читаются
    evs = reg.list_events(c2.calendar_id)
    assert len(evs) == 1 and evs[0].sku == "SKU-1"

    # снятие: датасет возвращается к «календаря нет» (fail-open)
    assert reg.remove_active("ds1") is True
    assert reg.get_active("ds1") is None
    assert reg.remove_active("ds1") is False


def test_registry_new_candidate_discards_previous(reg):
    c1 = reg.create_candidate("c1", "ds1", "a.csv", {}, _events(), None)
    c2 = reg.create_candidate("c1", "ds1", "b.csv", {}, _events(), None)
    assert reg.get(c1.calendar_id).status == "discarded"
    assert reg.get_candidate("ds1").calendar_id == c2.calendar_id
    with pytest.raises(ValueError):
        reg.apply(c1.calendar_id)     # отброшенного не применить


# ── конвейер: kind-ветвление и размер ────────────────────────────────────

def test_accept_upload_promo_size_cap(monkeypatch, tmp_path):
    import src.storage.upload_registry as ur_mod
    from src.storage.upload_pipeline import PROMO_CALENDAR_MAX_BYTES, UploadRejected, accept_upload
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    ur_mod.reset_registry_for_tests()
    big = b"a" * (PROMO_CALENDAR_MAX_BYTES + 1)
    with pytest.raises(UploadRejected, match="не должен превышать"):
        accept_upload("c1", "cal.csv", big, dataset_id="ds1", kind="promo_calendar")
    rec = accept_upload("c1", "cal.csv", b"sku;date_from;date_to\n",
                        dataset_id="ds1", kind="promo_calendar")
    assert rec.kind == "promo_calendar" and rec.status == "uploaded"
    ur_mod.reset_registry_for_tests()


# ── API-контракты ────────────────────────────────────────────────────────

def _wire(monkeypatch, reg):
    from src.storage import promo_calendar as pc_mod
    monkeypatch.setattr(pc_mod, "_registry", reg)
    monkeypatch.setattr(pcr, "_dataset_or_404", lambda did, cid: object())
    monkeypatch.setattr(pcr, "_audit", lambda *a, **k: None)


def test_apply_cross_tenant_404(monkeypatch, reg):
    _wire(monkeypatch, reg)
    c = reg.create_candidate("OTHER", "ds1", "a.csv", {}, _events(), None)
    with pytest.raises(HTTPException) as e:
        pcr.promo_calendar_apply(
            "c1", "ds1", pcr.ApplyRequest(calendar_id=c.calendar_id),
            _Req(), auth=_auth())
    assert e.value.status_code == 404


def test_apply_only_pending(monkeypatch, reg):
    _wire(monkeypatch, reg)
    c = reg.create_candidate("c1", "ds1", "a.csv", {}, _events(), None)
    reg.apply(c.calendar_id)
    with pytest.raises(HTTPException) as e:
        pcr.promo_calendar_apply(
            "c1", "ds1", pcr.ApplyRequest(calendar_id=c.calendar_id),
            _Req(), auth=_auth())
    assert e.value.status_code == 409


def test_state_view_shows_active_and_candidate(monkeypatch, reg):
    _wire(monkeypatch, reg)

    class _URec:
        kind = "promo_calendar"
        dataset_id = "ds1"
        upload_id = "u1"
        status = "processed"
        error_message = None
        filename = "a.csv"
    class _UReg:
        def list_for_client(self, cid, limit=100): return [_URec()]
    import src.storage.upload_registry as ur_mod
    monkeypatch.setattr(ur_mod, "get_upload_registry", lambda: _UReg())

    c = reg.create_candidate("c1", "ds1", "a.csv",
                             {"date_min": "2026-08-01"}, _events(), None)
    out = pcr.promo_calendar_state("c1", "ds1", auth=_auth())
    assert out["active"] is None
    assert out["candidate"]["calendar_id"] == c.calendar_id
    assert out["last_upload"]["upload_id"] == "u1"
    assert "после следующего обучения" in out["note"]

    reg.apply(c.calendar_id)
    out = pcr.promo_calendar_state("c1", "ds1", auth=_auth())
    assert out["active"]["calendar_id"] == c.calendar_id
    assert out["candidate"] is None


def test_template_is_csv_attachment_with_rules():
    resp = pcr.promo_calendar_template("c1", auth=_auth())
    body = bytes(resp.body)
    assert body.startswith("﻿".encode("utf-8"))          # BOM для Excel-RU
    text = body.decode("utf-8-sig")
    assert text.splitlines()[0] == "sku;category;date_from;date_to;depth;name"
    assert "attachment" in resp.headers["content-disposition"]


def test_gate_single_point_currently_open():
    """Тариф-гейт — одна именованная точка; решение владельца открыто →
    сейчас доступ всем. Когда гейт включат, этот тест обновится вместе с
    _promo_calendar_allowed (и только с ней)."""
    assert pcr._promo_calendar_allowed("any-client") is None


# ── run_promo_process: интеграция на стабах песочницы ────────────────────

def test_run_promo_process_creates_candidate(monkeypatch, tmp_path):
    import src.storage.upload_registry as ur_mod
    import src.storage.promo_calendar as pc_mod
    from src.storage import upload_pipeline as up

    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    ur_mod.reset_registry_for_tests()
    pc_mod.reset_registry_for_tests()

    rec = up.accept_upload("c1", "cal.csv", b"x", dataset_id="ds1",
                           kind="promo_calendar")
    ureg = ur_mod.get_upload_registry()
    ureg.update_status(rec.upload_id, ur_mod.SCANNING)
    ureg.update_status(rec.upload_id, ur_mod.SCANNED_CLEAN)

    # sandbox-стаб: sniff отдаёт заголовки, run — parquet с событиями
    parquet = tmp_path / "events.parquet"
    pd.DataFrame({
        "sku": ["SKU-1", None], "category": [None, "Молочка"],
        "date_from": pd.to_datetime(["2026-08-01", "2026-08-05"]),
        "date_to": pd.to_datetime(["2026-08-10", "2026-08-20"]),
        "depth": [15.0, None], "name": ["x", None],
    }).to_parquet(parquet, index=False)

    class _Res:
        def __init__(self, ok, manifest=None, output_path=None):
            self.ok = ok
            self.exit_code = 0 if ok else 3
            self.stdout = ""
            self.stderr = ""
            self.manifest = manifest
            self.output_path = output_path
    class _Sandbox:
        def sniff(self, data, filename):
            return _Res(True, manifest={"headers": ["sku", "category",
                                                    "date_from", "date_to",
                                                    "depth", "name"]})
        def run(self, data, filename, mapping=None, schema=None):
            assert schema == "promo-calendar"
            return _Res(True,
                        manifest={"rows_total": 2, "rows_accepted": 2,
                                  "rows_rejected": 0, "rejected_examples": [],
                                  "date_min": "2026-08-01",
                                  "date_max": "2026-08-20"},
                        output_path=parquet)

    # кварантин-читалка: сам байтовый файл нам не нужен — вернём что есть
    monkeypatch.setattr(up.z, "get_zone_backend",
                        lambda zone: type("B", (), {
                            "download_bytes": lambda self, k: b"x",
                            "delete": lambda self, k: None})())

    out = up.run_promo_process(rec.upload_id, sandbox=_Sandbox())
    assert out.status == ur_mod.PROCESSED
    assert out.row_count == 2
    cand = pc_mod.get_promo_calendar_registry().get_candidate("ds1")
    assert cand is not None and cand.rows_accepted == 2
    evs = pc_mod.get_promo_calendar_registry().list_events(cand.calendar_id)
    assert {e.sku for e in evs} == {"SKU-1", None}
    assert {e.category for e in evs} == {None, "Молочка"}

    ur_mod.reset_registry_for_tests()
    pc_mod.reset_registry_for_tests()
