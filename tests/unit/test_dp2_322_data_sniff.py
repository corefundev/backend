"""
DP-2 (#322): in-sandbox format sniffing + robust parsing for «Подготовка данных».

Exercises secure_parser's detection primitives + sniff report + the robust
CSV read path + RU-tolerant coercion, on realistic fixtures (cp1251, ';'-
separated, comma decimals, day-first dates, wide layout, xlsx). The sniff logic
lives inside secure_parser.py because the sandbox image copies that file alone.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from src.validation import secure_parser as sp


# ── detection primitives ──────────────────────────────────────────────────────

def test_detect_encoding_utf8_and_cp1251():
    assert sp.detect_encoding("дата,артикул\n".encode("utf-8")) in ("utf-8", "utf-8-sig")
    # cyrillic that is INVALID utf-8 but valid cp1251 → must fall back to cp1251
    assert sp.detect_encoding("Товар;Цена\n".encode("cp1251")) == "cp1251"


def test_detect_delimiter_semicolon_comma_tab():
    assert sp.detect_delimiter("a;b;c\n1;2;3\n") == ";"
    assert sp.detect_delimiter("a,b,c\n1,2,3\n") == ","
    assert sp.detect_delimiter("a\tb\tc\n1\t2\t3\n") == "\t"


def test_detect_decimal_comma_vs_dot():
    assert sp.detect_decimal(["1 234,50", "12,5", "0,99"]) == ","
    assert sp.detect_decimal(["1234.50", "12.5", "0.99"]) == "."
    assert sp.detect_decimal(["", "abc"]) == "."   # nothing numeric → default dot


def test_detect_shape_wide_vs_long():
    assert sp.detect_shape(["sku", "2024-01-01", "2024-01-02", "2024-01-03"]) == "wide"
    assert sp.detect_shape(["date", "sku", "sales", "price"]) == "long"


# ── sniff_file report ─────────────────────────────────────────────────────────

def test_sniff_csv_ru_export(tmp_path):
    src = tmp_path / "in.csv"
    src.write_bytes(
        "дата;артикул;продажи;цена\n"
        "01.02.2024;А1;10;5,50\n"
        "02.02.2024;А1;12;5,50\n".encode("cp1251")
    )
    r = sp.sniff_file(src)
    assert r["format"] == "csv"
    assert r["encoding"] == "cp1251"
    assert r["delimiter"] == ";"
    assert r["decimal"] == ","
    assert r["row_shape"] == "long"
    assert r["headers"] == ["дата", "артикул", "продажи", "цена"]
    assert r["n_columns"] == 4
    assert len(r["sample_rows"]) == 2
    assert r["confidence"] == "medium"   # decoded via cp1251 fallback → flagged


def test_sniff_csv_canonical_is_high_confidence(tmp_path):
    src = tmp_path / "in.csv"
    src.write_text("date,sku,sales\n2024-01-01,A,10\n", encoding="utf-8")
    r = sp.sniff_file(src)
    assert r["format"] == "csv" and r["delimiter"] == "," and r["confidence"] == "high"


def test_sniff_xlsx(tmp_path):
    pytest.importorskip("openpyxl")   # sandbox/CI dep; skip on bare local venv
    src = tmp_path / "in.xlsx"
    pd.DataFrame({"date": ["2024-01-01"], "sku": ["A"], "sales": [10]}).to_excel(
        src, index=False, engine="openpyxl"
    )
    r = sp.sniff_file(src)
    assert r["format"] == "xlsx"
    assert r["headers"] == ["date", "sku", "sales"]
    assert r["encoding"] is None and r["delimiter"] is None


def test_sniff_rejects_legacy_xls(tmp_path):
    src = tmp_path / "in.xls"
    src.write_bytes(b"\xd0\xcf\x11\xe0" + b"\x00" * 64)
    with pytest.raises(ValueError, match="legacy .xls"):
        sp.sniff_file(src)


def test_sniff_sample_capped(tmp_path):
    src = tmp_path / "big.csv"
    src.write_text("date,sku,sales\n" + "".join(f"2024-01-01,A,{i}\n" for i in range(200)))
    r = sp.sniff_file(src)
    assert len(r["sample_rows"]) == sp._SNIFF_SAMPLE_ROWS   # never dumps the whole file


# ── RU-tolerant coercion ──────────────────────────────────────────────────────

def test_to_numeric_ru_and_dot_untouched():
    ru = sp._to_numeric(pd.Series(["1 234,50", "12,5", "0,99"]))
    assert list(ru) == [1234.5, 12.5, 0.99]
    dot = sp._to_numeric(pd.Series(["1234.50", "12.5", "0.99"]))
    assert list(dot) == [1234.5, 12.5, 0.99]


def test_to_datetime_dayfirst_and_iso():
    out = sp._to_datetime(pd.Series(["31.01.2024", "2024-02-01"]))
    assert out.iloc[0] == pd.Timestamp("2024-01-31")
    assert out.iloc[1] == pd.Timestamp("2024-02-01")


# ── end-to-end robust parse (the payoff) ──────────────────────────────────────

def _run(input_path: Path, tmp_path: Path, sniff: bool = False, **overrides):
    output = tmp_path / "out.parquet"
    manifest = tmp_path / "manifest.json"
    argv = ["--input", str(input_path), "--output", str(output),
            "--manifest", str(manifest)]
    if sniff:
        argv.append("--sniff")
    for k, v in overrides.items():
        argv += [f"--{k.replace('_', '-')}", str(v)]
    return sp.main(argv), output, manifest


def test_ru_csv_parses_end_to_end(tmp_path):
    """cp1251 + ';' + comma decimals + DD.MM.YYYY dates → canonical parquet."""
    src = tmp_path / "in.csv"
    src.write_bytes(
        "date;sku;sales;price\n"
        "01.02.2024;A1;10;5,50\n"
        "02.02.2024;A1;12,0;5,50\n"
        "03.02.2024;B2;7;2,00\n".encode("cp1251")
    )
    rc, out, man = _run(src, tmp_path)
    assert rc == 0, "RU export must parse, not reject"
    df = pd.read_parquet(out)
    assert list(df.columns) == ["date", "sku", "sales", "price"]
    assert df["sales"].tolist() == [10.0, 12.0, 7.0]
    assert df["price"].tolist() == [5.5, 5.5, 2.0]
    assert str(df["date"].iloc[0].date()) == "2024-02-01"


def test_sniff_cli_writes_report_no_parquet(tmp_path):
    src = tmp_path / "in.csv"
    src.write_text("date,sku,sales\n2024-01-01,A,10\n", encoding="utf-8")
    rc, out, man = _run(src, tmp_path, sniff=True)
    assert rc == 0
    assert not out.exists(), "sniff mode must NOT write a parquet"
    report = json.loads(man.read_text())
    assert report["format"] == "csv" and report["headers"] == ["date", "sku", "sales"]
