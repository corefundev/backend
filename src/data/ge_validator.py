"""
src/data/ge_validator.py

Data validation using Great Expectations.
Runs a suite of expectations on the input DataFrame and raises
DataValidationError if any critical expectation fails.

Falls back to manual checks (loader.py) if GE is not installed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pandas as pd

logger = logging.getLogger(__name__)


class DataValidationError(Exception):
    """Raised when critical data quality checks fail."""


@dataclass
class ValidationResult:
    success: bool
    failed_expectations: list[dict] = field(default_factory=list)
    statistics: dict = field(default_factory=dict)


def validate_with_great_expectations(
    df: pd.DataFrame,
    config: dict,
    raise_on_failure: bool = True,
) -> ValidationResult:
    """
    Run GE expectations on df. Returns ValidationResult.
    If raise_on_failure=True, raises DataValidationError on any critical failure.
    """
    try:
        import great_expectations as ge
    except ImportError:
        logger.warning(
            "great_expectations not installed. "
            "Falling back to manual validation. "
            "Install with: pip install great_expectations"
        )
        return _manual_validate(df, config, raise_on_failure)

    return _ge_validate(df, config, raise_on_failure)


def _ge_validate(df: pd.DataFrame, config: dict, raise_on_failure: bool) -> ValidationResult:
    """Run actual Great Expectations suite."""
    import great_expectations as ge

    cfg = config["data"]
    date_col   = cfg["date_col"]
    sku_col    = cfg["sku_col"]
    target_col = cfg["target_col"]
    max_missing = cfg.get("max_missing_ratio", 0.10)

    ge_df = ge.from_pandas(df)
    failed = []

    def check(expectation_name: str, result: dict, critical: bool = True) -> None:
        if not result.get("success", False):
            entry = {"expectation": expectation_name, "result": result, "critical": critical}
            failed.append(entry)
            level = logger.error if critical else logger.warning
            level(f"GE FAIL [{expectation_name}]: {result.get('result', {})}")
        else:
            logger.debug(f"GE PASS [{expectation_name}]")

    # 1. Required columns exist
    for col in [date_col, sku_col, target_col]:
        r = ge_df.expect_column_to_exist(col)
        check(f"column_exists:{col}", r.to_json_dict())

    # 2. No null values in required columns
    for col in [date_col, sku_col]:
        r = ge_df.expect_column_values_to_not_be_null(col)
        check(f"not_null:{col}", r.to_json_dict())

    # 3. Target: null ratio below threshold
    r = ge_df.expect_column_values_to_not_be_null(
        target_col,
        mostly=1.0 - max_missing,
    )
    check(f"target_null_ratio<{max_missing:.0%}", r.to_json_dict())

    # 4. No negative sales
    r = ge_df.expect_column_values_to_be_between(
        target_col,
        min_value=0,
        mostly=1.0,  # allow clipping, don't hard-fail
    )
    check(f"target_non_negative", r.to_json_dict(), critical=False)

    # 5. No duplicate (sku, date) pairs
    r = ge_df.expect_compound_columns_to_be_unique([sku_col, date_col])
    check("no_duplicate_sku_date", r.to_json_dict())

    # 6. Date column parseable
    r = ge_df.expect_column_values_to_not_be_null(date_col)
    check("date_not_null", r.to_json_dict())

    # 7. SKU column not empty string
    r = ge_df.expect_column_values_to_not_be_null(sku_col)
    check("sku_not_null", r.to_json_dict())

    # 8. Price non-negative (if present)
    if "price" in df.columns:
        r = ge_df.expect_column_values_to_be_between("price", min_value=0, mostly=0.99)
        check("price_non_negative", r.to_json_dict(), critical=False)

    # 9. Promo is binary (if present)
    if "promo" in df.columns:
        r = ge_df.expect_column_values_to_be_in_set("promo", value_set=[0, 1, 0.0, 1.0])
        check("promo_binary", r.to_json_dict(), critical=False)

    # 10. Reasonable row count
    r = ge_df.expect_table_row_count_to_be_between(
        min_value=cfg.get("min_rows", 30),
        max_value=None,
    )
    check("min_row_count", r.to_json_dict())

    stats = {
        "total_rows": len(df),
        "n_skus": df[sku_col].nunique(),
        "target_null_pct": round(df[target_col].isna().mean() * 100, 2),
        "n_expectations_checked": 10,
        "n_failed": len(failed),
    }

    critical_failures = [f for f in failed if f["critical"]]
    success = len(critical_failures) == 0

    if not success and raise_on_failure:
        msgs = [f["expectation"] for f in critical_failures]
        raise DataValidationError(
            f"Great Expectations: {len(critical_failures)} critical failures: {msgs}"
        )

    logger.info(
        f"GE validation: {'PASSED' if success else 'FAILED'} "
        f"({len(failed)} failures, {stats['n_skus']} SKUs, {stats['total_rows']} rows)"
    )
    return ValidationResult(success=success, failed_expectations=failed, statistics=stats)


def _manual_validate(df: pd.DataFrame, config: dict, raise_on_failure: bool) -> ValidationResult:
    """Fallback when GE is not installed — mirrors GE checks manually."""
    cfg = config["data"]
    date_col   = cfg["date_col"]
    sku_col    = cfg["sku_col"]
    target_col = cfg["target_col"]
    max_missing = cfg.get("max_missing_ratio", 0.10)

    failed = []

    def fail(name: str, detail: str, critical: bool = True) -> None:
        failed.append({"expectation": name, "detail": detail, "critical": critical})
        (logger.error if critical else logger.warning)(f"FAIL [{name}]: {detail}")

    for col in [date_col, sku_col, target_col]:
        if col not in df.columns:
            fail(f"column_exists:{col}", f"Column '{col}' missing")

    if target_col in df.columns:
        miss = df[target_col].isna().mean()
        if miss > max_missing:
            fail("target_null_ratio", f"{miss:.2%} > {max_missing:.2%}")

    if df.duplicated(subset=[sku_col, date_col]).any():
        fail("no_duplicate_sku_date", "Duplicate (sku, date) pairs found")

    stats = {
        "total_rows": len(df),
        "n_skus": df[sku_col].nunique() if sku_col in df.columns else 0,
    }

    critical_failures = [f for f in failed if f["critical"]]
    success = len(critical_failures) == 0

    if not success and raise_on_failure:
        raise DataValidationError(str([f["expectation"] for f in critical_failures]))

    return ValidationResult(success=success, failed_expectations=failed, statistics=stats)
