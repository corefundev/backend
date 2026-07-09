"""
#324 security: client-facing upload error_message must NEVER carry raw parser
internals (Python tracebacks, file paths, sandbox/parser jargon). run_process
sanitizes to curated, path-free RU messages; the raw detail is logged only.
"""
from src.storage import upload_pipeline as up

_LEAK_TOKENS = (
    "Traceback", "/sandbox", "/opt", "/usr", ".py", "Errno",
    "usage:", "secure_parser", "PermissionError",
)


def _no_leak(msg: str) -> bool:
    return not any(tok.lower() in msg.lower() for tok in _LEAK_TOKENS)


def test_traceback_never_surfaces():
    tb = (
        "Traceback (most recent call last):\n"
        '  File "/opt/secure_parser.py", line 224, in main\n'
        "    df_clean.to_parquet(args.output)\n"
        "PermissionError: [Errno 13] Permission denied: '/sandbox/out/data.parquet'"
    )
    out = up._client_parse_error(tb)
    assert out == up._GENERIC_PREP_ERROR
    assert _no_leak(out)


def test_path_bearing_error_is_generic():
    out = up._client_parse_error("ERROR: input file not found: /sandbox/in/original.csv")
    assert out == up._GENERIC_PREP_ERROR
    assert _no_leak(out)


def test_usage_dump_is_generic():
    out = up._client_parse_error(
        "usage: secure_parser.py [-h] --input INPUT\n"
        "secure_parser.py: error: unrecognized arguments: python /opt/secure_parser.py"
    )
    assert out == up._GENERIC_PREP_ERROR
    assert _no_leak(out)


def test_curated_validation_reasons_surface_in_russian():
    assert "продаж" in up._client_parse_error("ERROR: 'sales' contains no numeric values")
    assert "отрицательны" in up._client_parse_error("ERROR: 'sales' contains negative values")
    assert ".xlsx" in up._client_parse_error(
        "ERROR: legacy .xls format is not accepted; please save as .xlsx")
    # all still leak-free
    for s in ("ERROR: 'sales' contains no numeric values",
              "ERROR: 'sku' contains empty values",
              "ERROR: too many rows: > 100"):
        assert _no_leak(up._client_parse_error(s))


def test_missing_columns_reason_is_generic_not_english():
    # run_prepare already gives a precise RU hint pre-parse; the parser's English
    # "missing required columns" must not reach the client verbatim.
    out = up._client_parse_error("ERROR: missing required columns: ['sales', 'sku']")
    assert out == up._GENERIC_PREP_ERROR
    assert "['sales'" not in out


def test_empty_stderr_is_generic():
    assert up._client_parse_error("") == up._GENERIC_PREP_ERROR
    assert up._client_parse_error("\n  \n") == up._GENERIC_PREP_ERROR
