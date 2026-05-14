"""
Unit tests for src/validation/secure_parser.py.

Runs the parser as a regular function (not in the sandbox) — the sandbox
itself is integration-tested elsewhere.
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from src.validation import secure_parser as sp


def _run(input_path: Path, tmp_path: Path, **overrides) -> tuple[int, Path, Path]:
    output   = tmp_path / "out.parquet"
    manifest = tmp_path / "manifest.json"
    argv = [
        "--input",    str(input_path),
        "--output",   str(output),
        "--manifest", str(manifest),
    ]
    for k, v in overrides.items():
        argv += [f"--{k.replace('_', '-')}", str(v)]
    exit_code = sp.main(argv)
    return exit_code, output, manifest


# ── happy path ────────────────────────────────────────────────────────────────

def test_csv_happy_path(tmp_path):
    src = tmp_path / "in.csv"
    src.write_text(
        "date,sku,sales,price,promo,stock\n"
        "2024-01-01,A,10,5.5,0,100\n"
        "2024-01-02,A,12,5.5,1,90\n"
        "2024-01-03,B,7,2.0,0,50\n"
    )
    rc, out, man = _run(src, tmp_path)
    assert rc == 0
    assert out.exists()
    df = pd.read_parquet(out)
    assert list(df.columns) == ["date", "sku", "sales", "price", "promo", "stock"]
    assert len(df) == 3
    m = json.loads(man.read_text())
    assert m["row_count"] == 3
    assert m["sku_count"] == 2


# ── rejections ────────────────────────────────────────────────────────────────

def test_rejects_missing_columns(tmp_path):
    src = tmp_path / "in.csv"
    src.write_text("date,sku\n2024-01-01,A\n")
    rc, _, _ = _run(src, tmp_path)
    assert rc == 3


def test_sanitizes_formula_injection(tmp_path):
    """Audit R2-22 (2026-05-15) changed the contract from "reject" to
    "sanitize-by-prefix". The malicious cell value (`=cmd|" /c calc"!A1`)
    is no longer rejected — instead it gets a leading `'` prepended so
    Excel/LibreOffice render it as literal text, not a formula. From
    the pipeline's perspective the row is benign data; the test just
    asserts (1) the parser succeeds (rc=0) and (2) the output Parquet
    contains the sanitised value, not the original."""
    import pandas as pd

    src = tmp_path / "in.csv"
    src.write_text(
        "date,sku,sales\n"
        '2024-01-01,=cmd|" /c calc"!A1,10\n'
    )
    rc, out_path, _ = _run(src, tmp_path)
    assert rc == 0, "formula injection should be sanitized, not rejected"

    df = pd.read_parquet(out_path)
    # SKU column starts with apostrophe — Excel-safe.
    sku_value = df["sku"].iloc[0]
    assert sku_value.startswith("'="), (
        f"expected leading-apostrophe sanitisation, got {sku_value!r}"
    )


def test_rejects_negative_sales(tmp_path):
    src = tmp_path / "in.csv"
    src.write_text("date,sku,sales\n2024-01-01,A,-5\n")
    rc, _, _ = _run(src, tmp_path)
    assert rc == 3


def test_rejects_bad_dates(tmp_path):
    src = tmp_path / "in.csv"
    src.write_text("date,sku,sales\nnot-a-date,A,10\n")
    rc, _, _ = _run(src, tmp_path)
    assert rc == 3


def test_rejects_executable_magic(tmp_path):
    src = tmp_path / "in.csv"
    src.write_bytes(b"MZ\x90\x00" + b"date,sku,sales\n2024-01-01,A,10\n")
    rc, _, _ = _run(src, tmp_path)
    assert rc == 3


def test_rejects_too_many_rows(tmp_path):
    src = tmp_path / "in.csv"
    lines = ["date,sku,sales"] + [f"2024-01-{(i%28)+1:02d},A,{i}" for i in range(10)]
    src.write_text("\n".join(lines) + "\n")
    rc, _, _ = _run(src, tmp_path, max_rows=3)
    assert rc == 3


def test_rejects_unsupported_extension(tmp_path):
    src = tmp_path / "in.tsv"
    src.write_text("date\tsku\tsales\n2024-01-01\tA\t10\n")
    rc, _, _ = _run(src, tmp_path)
    assert rc == 3


def test_rejects_legacy_xls(tmp_path):
    # OLE2 / CFB magic
    src = tmp_path / "in.xlsx"
    src.write_bytes(b"\xd0\xcf\x11\xe0" + b"\x00" * 200)
    rc, _, _ = _run(src, tmp_path)
    assert rc == 3
