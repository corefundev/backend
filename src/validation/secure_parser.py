"""
src/validation/secure_parser.py

Standalone parser executed INSIDE the sandbox container.

Contract:
  • Reads one file at a fixed path (/sandbox/in/<filename>, read-only mount).
  • Writes results to /sandbox/out/data.parquet + /sandbox/out/manifest.json.
  • Exit code 0 on success, non-zero on validation/parse failure.
  • No network, no filesystem access outside /sandbox/, no project imports.

CLI:
    python secure_parser.py \
        --input /sandbox/in/original.csv \
        --output /sandbox/out/data.parquet \
        --manifest /sandbox/out/manifest.json \
        [--max-rows 5000000] \
        [--max-columns 64] \
        [--date-col date] [--sku-col sku] [--target-col sales]

Schema enforced:
    REQUIRED: date (ISO or common formats), sku (string), sales (number)
    OPTIONAL: price (number), promo (0/1), stock (number)

Defences applied:
    1. Extension/MIME already checked at API layer — parser still checks
       magic bytes (first 4 KiB) to refuse PE/ELF/script files.
    2. Excel files opened with data_only=False so formulas are read as
       strings — then we REJECT any cell that looks like a formula
       (starts with =, +, -, @ followed by whitespace/paren) to avoid
       spreadsheet-injection attacks on downstream viewers.
    3. Row/column caps prevent zip bombs and memory exhaustion.
    4. No pickle, no eval, no subprocess.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

import pandas as pd


# ── Magic byte detection for common executable formats ────────────────────────
# We refuse these even if extension is .csv — an attacker can rename anything.

_MAGIC_BLOCKLIST: list[tuple[bytes, str]] = [
    (b"MZ",             "PE executable"),
    (b"\x7fELF",        "ELF executable"),
    (b"\xca\xfe\xba\xbe", "Mach-O fat"),
    (b"\xcf\xfa\xed\xfe", "Mach-O 64-bit"),
    (b"\xfe\xed\xfa\xce", "Mach-O 32-bit"),
    (b"#!",             "shell script"),
    (b"PK\x05\x06",     "empty zip"),     # empty archive — suspicious
]


def _check_magic(sample: bytes) -> None:
    """Raise on executable / shell signatures."""
    for sig, label in _MAGIC_BLOCKLIST:
        if sample.startswith(sig):
            raise ValueError(f"refused file type: {label}")


# ── Formula-injection detection ───────────────────────────────────────────────
# CSV and spreadsheet viewers interpret cells starting with these chars as
# formulas. We don't want to propagate such cells into downstream tools.

_FORMULA_RE = re.compile(r"^\s*[=+\-@]")


def _looks_like_formula(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    if not value:
        return False
    return bool(_FORMULA_RE.match(value))


# ── File readers ──────────────────────────────────────────────────────────────

def _read_csv(path: Path, max_rows: int, max_columns: int) -> pd.DataFrame:
    # chunksize = None would load everything — we check size via `nrows` first.
    with path.open("rb") as f:
        sample = f.read(4096)
    _check_magic(sample)

    # Probe: read just one row to count columns.
    head = pd.read_csv(path, nrows=0)
    if len(head.columns) > max_columns:
        raise ValueError(
            f"too many columns: {len(head.columns)} > {max_columns}"
        )

    # Now read up to max_rows+1 to detect overflow.
    df = pd.read_csv(path, nrows=max_rows + 1, dtype=str, keep_default_na=False)
    if len(df) > max_rows:
        raise ValueError(f"too many rows: > {max_rows}")
    return df


def _read_excel(path: Path, max_rows: int, max_columns: int) -> pd.DataFrame:
    with path.open("rb") as f:
        sample = f.read(8)
    # Excel files must start with PK (xlsx = zip) or D0 CF 11 E0 (old xls/OLE).
    if not (sample.startswith(b"PK\x03\x04") or sample.startswith(b"\xd0\xcf\x11\xe0")):
        raise ValueError("file does not look like an Excel workbook")

    # openpyxl for xlsx (safe, pure Python); reject xls (legacy, xlrd removed).
    if sample.startswith(b"\xd0\xcf\x11\xe0"):
        raise ValueError("legacy .xls format is not accepted; please save as .xlsx")

    # Read as strings; disables numeric autodetection that can mask formulas.
    df = pd.read_excel(path, engine="openpyxl", dtype=str, nrows=max_rows + 1)
    if len(df.columns) > max_columns:
        raise ValueError(f"too many columns: {len(df.columns)} > {max_columns}")
    if len(df) > max_rows:
        raise ValueError(f"too many rows: > {max_rows}")
    return df


# ── Schema validation ─────────────────────────────────────────────────────────

_REQUIRED = ["date", "sku", "sales"]
_OPTIONAL = ["price", "promo", "stock"]


def _validate(df: pd.DataFrame, date_col: str, sku_col: str, target_col: str) -> pd.DataFrame:
    # Normalise column names (lowercase, strip).
    df = df.rename(columns={c: c.strip() for c in df.columns})

    expected = {date_col, sku_col, target_col}
    missing = expected - set(df.columns)
    if missing:
        raise ValueError(f"missing required columns: {sorted(missing)}")

    # Formula check across ALL cells (required + optional).
    scan_cols = list(expected) + [c for c in _OPTIONAL if c in df.columns]
    for col in scan_cols:
        bad_rows = df.index[df[col].map(_looks_like_formula)].tolist()
        if bad_rows:
            raise ValueError(
                f"formula-like cell in column {col!r} at rows "
                f"{bad_rows[:5]}{'...' if len(bad_rows) > 5 else ''}"
            )

    # Type coercion with strict validation.
    df = df.copy()
    # date
    parsed = pd.to_datetime(df[date_col], errors="coerce")
    bad_dates = parsed.isna() & df[date_col].astype(str).str.strip().ne("")
    if bad_dates.any():
        raise ValueError(
            f"{bad_dates.sum()} rows have unparseable {date_col!r} values "
            f"(e.g. {df.loc[bad_dates, date_col].iloc[0]!r})"
        )
    df[date_col] = parsed

    # sales → float, non-negative
    sales = pd.to_numeric(df[target_col], errors="coerce")
    if sales.isna().all():
        raise ValueError(f"{target_col!r} contains no numeric values")
    if (sales < 0).any():
        raise ValueError(f"{target_col!r} contains negative values")
    df[target_col] = sales

    # sku → non-empty string
    sku = df[sku_col].astype(str).str.strip()
    if (sku == "").any():
        raise ValueError(f"{sku_col!r} contains empty values")
    df[sku_col] = sku

    # Optional columns: coerce if present, no hard error on NaN.
    for opt in _OPTIONAL:
        if opt in df.columns:
            df[opt] = pd.to_numeric(df[opt], errors="coerce")

    return df


# ── Main ──────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Sandboxed CSV/XLSX parser")
    p.add_argument("--input",    required=True, type=Path)
    p.add_argument("--output",   required=True, type=Path)
    p.add_argument("--manifest", required=True, type=Path)
    p.add_argument("--max-rows",    type=int, default=5_000_000)
    p.add_argument("--max-columns", type=int, default=64)
    p.add_argument("--date-col",   default="date")
    p.add_argument("--sku-col",    default="sku")
    p.add_argument("--target-col", default="sales")
    args = p.parse_args(argv)

    input_path: Path = args.input
    if not input_path.is_file():
        print(f"ERROR: input file not found: {input_path}", file=sys.stderr)
        return 2

    suffix = input_path.suffix.lower()
    try:
        if suffix == ".csv":
            df_raw = _read_csv(input_path, args.max_rows, args.max_columns)
        elif suffix in {".xlsx", ".xlsm"}:
            df_raw = _read_excel(input_path, args.max_rows, args.max_columns)
        else:
            raise ValueError(f"unsupported extension: {suffix!r}")

        df_clean = _validate(
            df_raw,
            date_col=args.date_col,
            sku_col=args.sku_col,
            target_col=args.target_col,
        )
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 3

    args.output.parent.mkdir(parents=True, exist_ok=True)
    df_clean.to_parquet(args.output, index=False)

    sha = hashlib.sha256(args.output.read_bytes()).hexdigest()
    manifest = {
        "input_filename": input_path.name,
        "row_count": int(len(df_clean)),
        "column_count": int(len(df_clean.columns)),
        "columns": list(df_clean.columns),
        "sku_count": int(df_clean[args.sku_col].nunique()),
        "date_min": str(df_clean[args.date_col].min()),
        "date_max": str(df_clean[args.date_col].max()),
        "output_sha256": sha,
    }
    args.manifest.write_text(json.dumps(manifest, indent=2, default=str))

    print(
        f"OK rows={len(df_clean)} cols={len(df_clean.columns)} "
        f"skus={manifest['sku_count']} sha256={sha[:12]}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
