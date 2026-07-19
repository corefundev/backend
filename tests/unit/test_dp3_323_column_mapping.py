"""
DP-3 (#323): column-mapping engine — synonym auto-map + confidence over the
canonical schema (date, sku, sales, price, promo, stock). Extensive dictionary
covering 1С / Wildberries / Ozon / ERP / hand-Excel column names, RU + EN.
"""
from __future__ import annotations

from src.validation import column_mapping as cm


def _m(headers):
    return cm.propose_mapping(headers)


# ── canonical / zero-click ────────────────────────────────────────────────────

def test_canonical_headers_auto_confirmable():
    p = _m(["date", "sku", "sales", "price"])
    assert p.mapping["date"] == "date"
    assert p.mapping["sku"] == "sku"
    assert p.mapping["sales"] == "sales"
    assert p.mapping["price"] == "price"
    assert all(p.field_confidence[f] == "high" for f in cm.REQUIRED_FIELDS)
    assert p.auto_confirmable is True
    assert p.missing_required == []


# ── Russian 1С-style ──────────────────────────────────────────────────────────

def test_ru_1c_headers():
    p = _m(["Дата продажи", "Артикул", "Количество", "Цена"])
    assert p.mapping["date"] == "Дата продажи"
    assert p.mapping["sku"] == "Артикул"
    assert p.mapping["sales"] == "Количество"
    assert p.mapping["price"] == "Цена"
    assert p.auto_confirmable is True


def test_marketplace_headers():
    p = _m(["Артикул поставщика", "Дата заказа", "Продано штук", "Цена продавца"])
    assert p.mapping["sku"] == "Артикул поставщика"
    assert p.mapping["date"] == "Дата заказа"
    assert p.mapping["sales"] == "Продано штук"
    assert p.mapping["price"] == "Цена продавца"
    assert p.missing_required == []


def test_english_headers():
    p = _m(["Date", "Item", "Qty Sold", "Unit Price"])
    assert p.mapping["date"] == "Date"
    assert p.mapping["sku"] == "Item"
    assert p.mapping["sales"] == "Qty Sold"
    assert p.mapping["price"] == "Unit Price"


def test_optional_promo_and_stock():
    p = _m(["дата", "артикул", "продажи", "скидка", "остаток на складе"])
    assert p.mapping["promo"] == "скидка"
    assert p.mapping["stock"] == "остаток на складе"


# ── partial / missing required ────────────────────────────────────────────────

def test_missing_required_sales():
    p = _m(["Дата", "Артикул"])
    assert p.mapping["date"] == "Дата" and p.mapping["sku"] == "Артикул"
    assert p.mapping["sales"] is None
    assert p.missing_required == ["sales"]
    assert p.auto_confirmable is False


# ── fuzzy typos ───────────────────────────────────────────────────────────────

def test_fuzzy_typo_maps_but_not_high():
    p = _m(["дата", "артикл", "прдажи"])   # missing letters
    assert p.mapping["sku"] == "артикл"
    assert p.mapping["sales"] == "прдажи"
    # fuzzy hits are medium → NOT auto-confirmable (user confirms once)
    assert p.field_confidence["sku"] in ("medium", "high")
    assert p.auto_confirmable is False


def test_kolvo_abbreviation_normalized():
    p = _m(["Дата", "Артикул", "Кол-во, шт"])
    assert p.mapping["sales"] == "Кол-во, шт"   # 'кол-во' → 'количество'


# ── unmapped extras & conflicts ───────────────────────────────────────────────

def test_unmapped_extra_column_reported():
    # #545: «Категория» теперь КАНОНИЧЕСКОЕ поле (category_te) — примером
    # немаппящейся колонки служит настоящий сирота.
    p = _m(["дата", "артикул", "продажи", "Комментарий менеджера"])
    assert "Комментарий менеджера" in p.unmapped_headers
    assert p.mapping["date"] and p.mapping["sku"] and p.mapping["sales"]


def test_category_column_is_auto_mapped():
    for header in ("Категория", "Номенклатурная группа", "товарная группа",
                   "Product Group"):
        p = _m(["дата", "артикул", "продажи", header])
        assert p.mapping["category"] == header, header


def test_duplicate_candidates_assigned_once():
    p = _m(["Артикул", "Код товара", "Дата", "Продажи"])
    # both match sku; exactly one wins, the other is left unmapped
    assert p.mapping["sku"] in ("Артикул", "Код товара")
    used = [h for h in p.mapping.values() if h is not None]
    assert len(used) == len(set(used)), "no header assigned to two fields"
    assert len(p.unmapped_headers) == 1


# ── primitives ────────────────────────────────────────────────────────────────

def test_levenshtein_ratio_bounds():
    assert cm._levenshtein_ratio("артикул", "артикул") == 1.0
    assert cm._levenshtein_ratio("артикул", "артикл") >= 0.85
    assert cm._levenshtein_ratio("дата", "остаток") < 0.85


def test_to_dict_shape():
    d = _m(["date", "sku", "sales"]).to_dict()
    assert set(d) == {"mapping", "field_confidence", "field_score",
                      "unmapped_headers", "missing_required", "auto_confirmable"}
    assert d["auto_confirmable"] is True


# ── applying a confirmed mapping in the parser (secure_parser --mapping) ───────

import json                                      # noqa: E402
from pathlib import Path                          # noqa: E402

import pandas as pd                               # noqa: E402

from src.validation import secure_parser as sp    # noqa: E402


def _run_mapping(input_path: Path, tmp_path: Path, mapping: dict):
    output = tmp_path / "out.parquet"
    manifest = tmp_path / "manifest.json"
    argv = ["--input", str(input_path), "--output", str(output),
            "--manifest", str(manifest), "--mapping", json.dumps(mapping)]
    return sp.main(argv), output


def test_apply_mapping_renames_and_subsets():
    df = pd.DataFrame({"Дата продажи": ["2024-01-01"], "Артикул": ["A"],
                       "Количество": ["5"], "Категория": ["X"]})
    out = sp._apply_mapping(df, {"date": "Дата продажи", "sku": "Артикул",
                                 "sales": "Количество"})
    assert list(out.columns) == ["date", "sku", "sales"]   # extras dropped
    assert "Категория" not in out.columns


def test_parser_applies_mapping_end_to_end(tmp_path):
    src = tmp_path / "ru.csv"
    src.write_bytes(
        "Дата продажи;Артикул;Количество;Цена\n"
        "01.02.2024;Товар-А;10;5,50\n"
        "02.02.2024;Товар-А;12,0;5,50\n".encode("cp1251")
    )
    rc, out = _run_mapping(src, tmp_path, {
        "date": "Дата продажи", "sku": "Артикул",
        "sales": "Количество", "price": "Цена",
    })
    assert rc == 0
    df = pd.read_parquet(out)
    assert list(df.columns) == ["date", "sku", "sales", "price"]
    assert df["sales"].tolist() == [10.0, 12.0]
    assert str(df["date"].iloc[0].date()) == "2024-02-01"


def test_parser_mapping_missing_required_fails_closed(tmp_path):
    src = tmp_path / "ru.csv"
    src.write_text("Дата;Артикул\n2024-01-01,A\n", encoding="utf-8")
    # mapping omits sales → validation must reject (fail-closed)
    rc, _ = _run_mapping(src, tmp_path, {"date": "Дата", "sku": "Артикул"})
    assert rc == 3


def test_invalid_mapping_json_rejected(tmp_path):
    src = tmp_path / "x.csv"
    src.write_text("date,sku,sales\n2024-01-01,A,5\n", encoding="utf-8")
    output = tmp_path / "o.parquet"
    manifest = tmp_path / "m.json"
    rc = sp.main(["--input", str(src), "--output", str(output),
                  "--manifest", str(manifest), "--mapping", "{not json"])
    assert rc == 2
