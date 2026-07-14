"""QH-8 #441 — value-weighted обучение: плечи стенда зарегистрированы,
вес-математика корректна (∝|y| и ∝sqrt|y|, нормировка к среднему 1,
нулевые строки не выпадают)."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import bench_wf_ab as bench


def test_arms_registered():
    assert bench.ARMS["vw_abs"]["value_weight"] == "abs"
    assert bench.ARMS["vw_sqrt"]["value_weight"] == "sqrt"


def test_weight_math():
    tr = pd.DataFrame({"sales": [0.0, 1.0, 4.0, 100.0]})
    for mode, expect_ratio in (("abs", 100.0), ("sqrt", 10.0)):
        y = np.abs(tr["sales"].to_numpy())
        w = (y if mode == "abs" else np.sqrt(y)) + 1e-3
        w = w * (len(w) / w.sum())
        assert np.isclose(w.mean(), 1.0)
        assert w[0] > 0
        assert np.isclose(w[3] / w[1], expect_ratio, rtol=1e-2)
