"""
tests/unit/test_r11_75_dooobuchenie.py

R11-#75 (Вариант A) — incremental retrain merges the UNION of ALL the
client's processed uploads, not a single picked prior. The old two-dataset
merge silently lost history across multiple client returns (a 3rd retrain
extending from the 2nd upload only saw that one batch).

Pure pandas — load_data / load_config are monkeypatched, so no parquet/
config/libomp coupling; these run on macOS + CI.
"""
from __future__ import annotations

import os

import pandas as pd
import pytest


def _patch_loader(monkeypatch, frames: dict):
    import src.data.loader as loader
    monkeypatch.setattr(
        loader, "load_config",
        lambda p: {"data": {"sku_col": "sku", "date_col": "date"}},
    )
    monkeypatch.setattr(loader, "load_data", lambda path, cfg: frames[path].copy())


def test_merge_accumulates_full_history(monkeypatch):
    """3 uploads with disjoint dates → the merged set contains EVERY date
    (the accumulation bug lost the earliest uploads)."""
    from src.pipeline.task_queue import _merge_datasets
    frames = {
        "p_2yr": pd.DataFrame({"sku": ["A", "A"], "date": ["2024-01-01", "2024-01-02"], "sales": [10, 11]}),
        "p_m1":  pd.DataFrame({"sku": ["A"], "date": ["2024-02-01"], "sales": [20]}),
        "p_new": pd.DataFrame({"sku": ["A"], "date": ["2024-03-01"], "sales": [30]}),
    }
    _patch_loader(monkeypatch, frames)
    out = _merge_datasets(base_paths=["p_2yr", "p_m1"], new_path="p_new", config_path="x")
    try:
        m = pd.read_parquet(out)
        assert set(m["date"].astype(str).str[:10]) == {
            "2024-01-01", "2024-01-02", "2024-02-01", "2024-03-01",
        }
    finally:
        os.unlink(out)


def test_merge_newest_source_wins_on_collision(monkeypatch):
    """On a (sku, date) collision the value from the NEWEST source wins:
    current upload > newer prior > older prior."""
    from src.pipeline.task_queue import _merge_datasets
    frames = {
        "p_old": pd.DataFrame({"sku": ["A"], "date": ["2024-01-02"], "sales": [10]}),
        "p_mid": pd.DataFrame({"sku": ["A"], "date": ["2024-01-02"], "sales": [22]}),  # newer prior
        "p_new": pd.DataFrame({"sku": ["B"], "date": ["2024-01-02"], "sales": [33]}),  # diff sku
    }
    _patch_loader(monkeypatch, frames)
    out = _merge_datasets(base_paths=["p_old", "p_mid"], new_path="p_new", config_path="x")
    try:
        m = pd.read_parquet(out)
        a = m[(m["sku"] == "A")]
        assert len(a) == 1 and a["sales"].iloc[0] == 22, "newer prior must win on (A,2024-01-02)"
        assert set(m["sku"]) == {"A", "B"}
    finally:
        os.unlink(out)


def test_merge_current_upload_wins_over_priors(monkeypatch):
    """The current upload (new_path) is the newest source — it overrides a
    prior on the same (sku, date)."""
    from src.pipeline.task_queue import _merge_datasets
    frames = {
        "p_old": pd.DataFrame({"sku": ["A"], "date": ["2024-01-01"], "sales": [1]}),
        "p_new": pd.DataFrame({"sku": ["A"], "date": ["2024-01-01"], "sales": [99]}),
    }
    _patch_loader(monkeypatch, frames)
    out = _merge_datasets(base_paths=["p_old"], new_path="p_new", config_path="x")
    try:
        m = pd.read_parquet(out)
        assert len(m) == 1 and m["sales"].iloc[0] == 99
    finally:
        os.unlink(out)


def test_merge_missing_column_raises(monkeypatch):
    from src.pipeline.task_queue import _merge_datasets
    frames = {
        "p_bad": pd.DataFrame({"sku": ["A"], "sales": [1]}),  # no date column
        "p_new": pd.DataFrame({"sku": ["A"], "date": ["2024-01-01"], "sales": [1]}),
    }
    _patch_loader(monkeypatch, frames)
    with pytest.raises(ValueError):
        _merge_datasets(base_paths=["p_bad"], new_path="p_new", config_path="x")


def test_resolve_data_path_no_extend_is_passthrough():
    """Empty / None extend_from_paths → train on the single upload, no merge
    temp (first-training case + the non-extend path)."""
    from src.pipeline.task_queue import _resolve_data_path
    for paths in (None, []):
        eff, cleanup = _resolve_data_path(
            data_path="data.parquet", extend_from_paths=paths,
            config_path="cfg", runs=None, run_id=None,
        )
        assert eff == "data.parquet"
        assert cleanup is None
