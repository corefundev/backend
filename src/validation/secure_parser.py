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
import csv
import hashlib
import io
import json
import re
import sys
import warnings
from collections import Counter
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

_FORMULA_PREFIXES = ("=", "+", "@", "-")


def _strip_formula_prefix(df: pd.DataFrame) -> pd.DataFrame:
    """
    CSV/XLSX formula-injection defence (audit R2-22, 2026-05-15).

    A malicious cell value like `=cmd|'/C calc'!A0` is benign as a
    string in our DataFrame, but the moment someone opens our exported
    output in Excel / LibreOffice / Numbers, the leading `=` makes it
    a formula → code execution on the recipient's machine. Same with
    `+`, `@`, and `-` (legacy Lotus convention).

    We prepend a single `'` (apostrophe) to any string cell that starts
    with one of these prefixes — Excel treats the apostrophe as a
    text-quote and renders the rest as literal text without
    evaluating. The data we use server-side (numerics, dates) is
    unaffected because they come from numeric/date columns.
    """
    text_cols = df.select_dtypes(include="object").columns
    for c in text_cols:
        # `.str.startswith` with a tuple matches any prefix.
        mask = df[c].astype(str).str.startswith(_FORMULA_PREFIXES, na=False)
        if mask.any():
            df.loc[mask, c] = "'" + df.loc[mask, c].astype(str)
    return df


# ── Format sniffing (DP-2 #322) ───────────────────────────────────────────────
# In-house detection ONLY — no chardet/third-party (supply-chain policy). The
# sniff produces the report «Подготовка данных» shows the user (DP-4) and the
# mapping engine consumes (DP-3). It lives in THIS file because the sandbox image
# copies in secure_parser.py alone (Dockerfile.sandbox) — untrusted bytes must
# never be sniffed outside the isolation boundary.
_DELIMITERS = (",", ";", "\t", "|")
_SNIFF_SAMPLE_BYTES = 64 * 1024
_SNIFF_SAMPLE_ROWS = 20
# DP-4b (#324): a small CANONICAL sample embedded in the parse manifest so the
# read-only prep preview serves it from the (tiny) manifest JSON — never re-loads
# the full processed parquet just to show a handful of rows.
_PREVIEW_SAMPLE_ROWS = 10
_DATE_HEADER_RE = re.compile(
    r"^\d{4}[-/.]\d{1,2}([-/.]\d{1,2})?$"      # 2024-01, 2024/01/31
    r"|^\d{1,2}[.]\d{1,2}[.]\d{2,4}$"           # 31.01.2024
)
_NUMERICISH_RE = re.compile(r"^[\s\d.,+\-]+$")
_EU_DATE_RE = re.compile(r"^\d{1,2}[./]\d{1,2}[./]\d{2,4}$")   # DD.MM.YYYY (day-first)


def detect_encoding(raw: bytes) -> str:
    """Detect the text encoding. A real UTF-8 BOM → 'utf-8-sig' (so the BOM is
    stripped on read); otherwise the first encoding that decodes cleanly. cp1251
    covers Russian Excel/1С exports; latin-1 is the last resort (never fails).
    BOM-less UTF-8/ASCII is reported as plain 'utf-8', not 'utf-8-sig', so the
    label shown to the user is accurate."""
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    for enc in ("utf-8", "cp1251", "cp1252", "latin-1"):
        try:
            raw.decode(enc)
            return enc
        except (UnicodeDecodeError, LookupError):
            continue
    return "latin-1"


def _count_outside_quotes(line: str, delim: str) -> int:
    """Occurrences of `delim` OUTSIDE double-quoted cells (AUD-9 #361).

    Raw `line.count(d)` sees delimiters INSIDE quoted text — a comma file
    with `"болт; гайка"` in a description column scores ';' once per row,
    exactly the stable-count signature the #348 override trusts, flipping
    the detection to ';' (the mirror image of the bug #348 fixed). CSV
    quoting rules: a bare `"` toggles quoted state; the doubled `""`
    escape toggles twice with nothing between, so it can't miscount."""
    count, in_quotes = 0, False
    for ch in line:
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == delim and not in_quotes:
            count += 1
    return count


def _delimiter_scores(lines: "list[str]") -> "dict[str, tuple[int, float]]":
    """Per-delimiter (modal per-line count, consistency-weighted score) over the
    sample lines. The true delimiter splits every row into the same N fields → a
    high, STABLE per-line count; a stray comma in a header/decimal appears ≤1×
    and loses. score = modal_count × (fraction of lines carrying that count).
    Counting is quote-aware — a delimiter inside a quoted cell is field TEXT,
    not structure (AUD-9 #361)."""
    n = max(1, len(lines))
    scores: "dict[str, tuple[int, float]]" = {}
    for d in _DELIMITERS:
        counts = [_count_outside_quotes(ln, d) for ln in lines] or [0]
        modal, freq = Counter(counts).most_common(1)[0]
        scores[d] = (modal, modal * (freq / n))
    return scores


def detect_delimiter(sample_text: str) -> str:
    """Sniff the CSV delimiter. RU spreadsheet exports use ';' (Excel list-
    separator locale) AND comma decimals, so a unit-annotated header like
    «Цена, руб» puts a comma on every line — which fools csv.Sniffer into picking
    ',' over the real ';'. We cross-check the Sniffer against a per-line
    frequency+consistency score and override it when another delimiter recurs far
    more consistently (#348). csv.Sniffer still wins the ambiguous/quoted cases."""
    lines = [ln for ln in sample_text.splitlines() if ln.strip()][:_SNIFF_SAMPLE_ROWS]
    if not lines:
        return ","
    scores = _delimiter_scores(lines)
    freq_best = max(_DELIMITERS, key=lambda d: scores[d][1])
    try:
        dialect = csv.Sniffer().sniff(sample_text, delimiters="".join(_DELIMITERS))
        picked = dialect.delimiter
        if picked in _DELIMITERS:
            # Trust the Sniffer unless another candidate recurs with a strictly
            # higher modal per-line count AND a higher consistency score — the RU
            # ';'+unit-comma case (',' at ~1/line vs ';' at ~4/line).
            if (scores[freq_best][0] > scores[picked][0]
                    and scores[freq_best][1] > scores[picked][1]):
                return freq_best
            return picked
    except csv.Error:
        pass
    return freq_best if scores[freq_best][0] > 0 else ","


def detect_decimal(values: "list[str]") -> str:
    """Comma vs dot decimal separator from numeric-looking sample values.
    '1 234,50' → ',' ; '1234.50' → '.' ; ambiguous/empty → '.'."""
    comma = dot = 0
    for v in values:
        s = str(v or "").strip()
        if not s or not _NUMERICISH_RE.match(s):
            continue
        # a real decimal has at most ONE separator of each kind; values like
        # "01.02.2024" (a date) or "1.234.567" carry more → skip, don't pollute.
        if s.count(".") > 1 or s.count(",") > 1:
            continue
        last_comma, last_dot = s.rfind(","), s.rfind(".")
        if last_comma > last_dot:
            comma += 1
        elif last_dot > last_comma:
            dot += 1
    return "," if comma > dot else "."


def detect_shape(headers: "list[str]") -> str:
    """Coarse layout class. 'wide' = dates spread across columns (needs an
    unpivot, DP-3 Wave 2); 'long' = canonical one-row-per-(sku, date)."""
    date_like = sum(1 for h in headers if _DATE_HEADER_RE.match(str(h).strip()))
    return "wide" if date_like >= 3 else "long"


def _confidence(fmt: str, enc: "str | None", headers: "list[str]") -> str:
    if not headers or not any(h.strip() for h in headers):
        return "low"
    if fmt == "csv" and enc not in ("utf-8", "utf-8-sig"):
        return "medium"     # decoded via a fallback (cp1251/latin-1) — flag it
    return "high"


def sniff_file(path: Path) -> "dict[str, Any]":
    """Produce the structured sniff report (format, encoding, delimiter, decimal,
    layout, headers, ~20 sample rows, confidence). Sample-only — never loads the
    whole file. Runs on the RAW upload inside the sandbox."""
    with path.open("rb") as f:
        raw = f.read(_SNIFF_SAMPLE_BYTES)
    _check_magic(raw)
    if raw.startswith(b"\xd0\xcf\x11\xe0"):
        raise ValueError("legacy .xls format is not accepted; please save as .xlsx")
    if raw.startswith(b"PK\x03\x04"):
        return _sniff_xlsx(path)
    return _sniff_csv(path, raw)


def _sniff_csv(path: Path, raw: bytes) -> "dict[str, Any]":
    enc = detect_encoding(raw)
    text = raw.decode(enc, errors="replace")
    delim = detect_delimiter(text)
    rows = [r for r in csv.reader(io.StringIO(text), delimiter=delim)
            if any(str(c).strip() for c in r)]
    headers = [str(h).strip() for h in rows[0]] if rows else []
    sample = [[str(c) for c in r] for r in rows[1:1 + _SNIFF_SAMPLE_ROWS]]
    flat = [c for r in sample for c in r]
    return {
        "format": "csv",
        "encoding": enc,
        "delimiter": delim,
        "decimal": detect_decimal(flat),
        "row_shape": detect_shape(headers),
        "n_columns": len(headers),
        "headers": headers,
        "sample_rows": sample,
        "confidence": _confidence("csv", enc, headers),
    }


def _sniff_xlsx(path: Path) -> "dict[str, Any]":
    import openpyxl   # already a sandbox dependency (see requirements-sandbox.txt)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb.active
        grid = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i > _SNIFF_SAMPLE_ROWS:
                break
            grid.append(["" if c is None else str(c) for c in row])
    finally:
        wb.close()
    grid = [r for r in grid if any(str(c).strip() for c in r)]
    headers = [h.strip() for h in grid[0]] if grid else []
    sample = grid[1:1 + _SNIFF_SAMPLE_ROWS]
    flat = [c for r in sample for c in r]
    return {
        "format": "xlsx",
        "encoding": None,
        "delimiter": None,
        "decimal": detect_decimal(flat),
        "row_shape": detect_shape(headers),
        "n_columns": len(headers),
        "headers": headers,
        "sample_rows": sample,
        "confidence": _confidence("xlsx", None, headers),
    }


def _read_csv(path: Path, max_rows: int, max_columns: int) -> pd.DataFrame:
    # chunksize = None would load everything — we check size via `nrows` first.
    with path.open("rb") as f:
        sample = f.read(_SNIFF_SAMPLE_BYTES)
    _check_magic(sample[:4096])

    # DP-2 #322: robust reading — detect encoding + delimiter so RU exports
    # (cp1251, ';'-separated) parse into columns instead of garbling/failing.
    enc = detect_encoding(sample)
    delim = detect_delimiter(sample.decode(enc, errors="replace"))

    # Probe: read just one row to count columns (streamed from path, not bytes).
    head = pd.read_csv(path, nrows=0, sep=delim, encoding=enc)
    if len(head.columns) > max_columns:
        raise ValueError(
            f"too many columns: {len(head.columns)} > {max_columns}"
        )

    # Now read up to max_rows+1 to detect overflow.
    df = pd.read_csv(path, nrows=max_rows + 1, sep=delim, encoding=enc,
                     dtype=str, keep_default_na=False)
    if len(df) > max_rows:
        raise ValueError(f"too many rows: > {max_rows}")
    return _strip_formula_prefix(df)


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
    return _strip_formula_prefix(df)


# ── Schema validation ─────────────────────────────────────────────────────────

_REQUIRED = ["date", "sku", "sales"]
_OPTIONAL = ["price", "promo", "stock"]


def _to_numeric(series: pd.Series) -> pd.Series:
    """Numeric coercion tolerant of RU exports: '1 234,50' / '1234,5' →
    1234.50 / 1234.5. Only rewrites when the comma-form parses STRICTLY MORE
    cells than the plain form, so genuine dot-decimal data is left untouched."""
    plain = pd.to_numeric(series, errors="coerce")
    if not plain.isna().any():
        return plain
    ru = pd.to_numeric(
        series.astype(str)
              .str.replace(r"[\s ]", "", regex=True)   # thousands spaces / NBSP
              .str.replace(",", ".", regex=False),
        errors="coerce",
    )
    return ru if ru.notna().sum() > plain.notna().sum() else plain


def _to_datetime(series: pd.Series) -> pd.Series:
    """Date coercion that handles day-first RU formats (31.01.2024). The dotted
    European form is inherently ambiguous ("01.02" = 1 Feb, not 2 Jan), so we
    pick dayfirst from the DATA: if most non-blank cells look European (DD.MM.YYYY
    with dots/slashes) we parse day-first. ISO rows (2024-02-01) parse the same
    either way, so a mixed column still resolves. A retry with the opposite
    setting catches the stragglers. The single-format inference warning is
    suppressed — the ambiguity is handled explicitly, not left to pandas."""
    s = series.astype(str).str.strip()
    nonblank = s.ne("")
    # pandas infers ONE format for the whole column from the first cell, which
    # cross-contaminates a mixed column — so parse the two families separately:
    # European dotted/slashed cells day-first, everything else (ISO, …) default.
    eu_mask = nonblank & s.str.match(_EU_DATE_RE.pattern)
    rest_mask = nonblank & ~eu_mask
    result = pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        if eu_mask.any():
            result[eu_mask] = pd.to_datetime(series[eu_mask], errors="coerce", dayfirst=True)
        if rest_mask.any():
            result[rest_mask] = pd.to_datetime(series[rest_mask], errors="coerce")
    return result


def _apply_mapping(df: pd.DataFrame, mapping: dict) -> pd.DataFrame:
    """Apply a CONFIRMED column mapping (DP-3 #323): {canonical_field:
    source_header} → rename the user's columns to canonical names, then keep
    only the canonical schema (date/sku/sales + optional price/promo/stock).

    Runs in-sandbox so the untrusted rename happens inside isolation. Unknown
    canonical keys and sources absent from the file are ignored; a required
    field left unmapped simply fails the downstream _validate (fail-closed)."""
    # AUD-9 (#361): strip header padding BEFORE matching. sniff_file strips
    # headers, so the auto-map's source names are stripped — but pandas keeps
    # `" Дата "` verbatim, `source in df.columns` matched nothing, every
    # column was dropped, and _validate failed "missing required columns" on
    # a file the system had just said it mapped. Mirrors _validate's strip.
    df = df.rename(columns={c: str(c).strip() for c in df.columns})
    df = df.loc[:, ~df.columns.duplicated()]
    schema = _REQUIRED + _OPTIONAL
    rename: "dict[str, str]" = {}
    for canonical, source in mapping.items():
        if canonical in schema and isinstance(source, str) and source in df.columns:
            rename[source] = canonical
    df = df.rename(columns=rename)
    # de-dup: if a rename collided with an existing canonical column, keep first
    df = df.loc[:, ~df.columns.duplicated()]
    keep = [c for c in schema if c in df.columns]
    return df[keep]


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
    # date (ISO first, day-first RU retry for the stragglers)
    parsed = _to_datetime(df[date_col])
    bad_dates = parsed.isna() & df[date_col].astype(str).str.strip().ne("")
    if bad_dates.any():
        raise ValueError(
            f"{bad_dates.sum()} rows have unparseable {date_col!r} values "
            f"(e.g. {df.loc[bad_dates, date_col].iloc[0]!r})"
        )
    df[date_col] = parsed

    # sales → float, non-negative (RU decimal-comma tolerant)
    sales = _to_numeric(df[target_col])
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
            df[opt] = _to_numeric(df[opt])

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
    p.add_argument("--sniff", action="store_true",
                   help="emit a format sniff report to --manifest and exit (no parse, no parquet)")
    p.add_argument("--mapping", default=None,
                   help='JSON {canonical_field: source_header} applied before validation (DP-3)')
    args = p.parse_args(argv)

    input_path: Path = args.input
    if not input_path.is_file():
        print(f"ERROR: input file not found: {input_path}", file=sys.stderr)
        return 2

    mapping: "dict | None" = None
    if args.mapping:
        try:
            mapping = json.loads(args.mapping)
            if not isinstance(mapping, dict):
                raise ValueError("mapping must be a JSON object")
        except Exception as e:
            print(f"ERROR: invalid --mapping JSON: {e}", file=sys.stderr)
            return 2

    # DP-2 #322: sniff-only pass — detect format/encoding/delimiter/layout and
    # return a sample-based report. Never writes a parquet; feeds «Подготовка
    # данных» (DP-4 preview) and the mapping engine (DP-3).
    if args.sniff:
        try:
            report = sniff_file(input_path)
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 3
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"SNIFF format={report['format']} cols={report['n_columns']} "
              f"shape={report['row_shape']} confidence={report['confidence']}")
        return 0

    suffix = input_path.suffix.lower()
    try:
        if suffix == ".csv":
            df_raw = _read_csv(input_path, args.max_rows, args.max_columns)
        elif suffix == ".xlsx":
            df_raw = _read_excel(input_path, args.max_rows, args.max_columns)
        else:
            raise ValueError(f"unsupported extension: {suffix!r}")

        if mapping:
            df_raw = _apply_mapping(df_raw, mapping)

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
    # JSON-safe canonical preview sample via pandas' own serializer: NaN → null,
    # numpy scalars → native numbers, datetimes → ISO strings. Round-tripped so it
    # embeds as real JSON (list-of-rows) rather than stringified numpy.
    sample_rows = json.loads(
        df_clean.head(_PREVIEW_SAMPLE_ROWS).to_json(orient="values", date_format="iso")
    )
    manifest = {
        "input_filename": input_path.name,
        "row_count": int(len(df_clean)),
        "column_count": int(len(df_clean.columns)),
        "columns": list(df_clean.columns),
        "sku_count": int(df_clean[args.sku_col].nunique()),
        "date_min": str(df_clean[args.date_col].min()),
        "date_max": str(df_clean[args.date_col].max()),
        "output_sha256": sha,
        "sample_rows": sample_rows,
    }
    args.manifest.write_text(json.dumps(manifest, indent=2, default=str))

    print(
        f"OK rows={len(df_clean)} cols={len(df_clean.columns)} "
        f"skus={manifest['sku_count']} sha256={sha[:12]}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
